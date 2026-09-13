from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from time import time
from typing import Any, Callable

from .dev_logger import dev_log, summarize_action
from .event_bus import EventBus
from .models import Device, IoTEvent
from .devices.discovery import DeviceDiscoveryMixin
from .printing.barcode import BarcodeMixin
from .printing.escpos import EscposEncodingMixin
from .printing.image_renderer import ImageRendererMixin
from .printing.job_queue import reject_queued_jobs
from .printing.network_printer import NetworkPrinterMixin
from .printing.normalization import ReceiptNormalizationMixin
from .printing.product_parser import ProductParserMixin
from .printing.receipt_metadata import ReceiptMetadataMixin
from .printing.section_consumers import ReceiptSectionConsumerMixin
from .printing.text_layout import TextLayoutMixin
from .printing.windows_printer import WindowsPrinterMixin
from .receipts.processing import ReceiptProcessingMixin
from .receipts.structured import StructuredReceiptMixin

_logger = logging.getLogger(__name__)

class DeviceManager(
    DeviceDiscoveryMixin,
    ReceiptProcessingMixin,
    StructuredReceiptMixin,
    NetworkPrinterMixin,
    WindowsPrinterMixin,
    EscposEncodingMixin,
    ReceiptNormalizationMixin,
    ReceiptSectionConsumerMixin,
    ProductParserMixin,
    ReceiptMetadataMixin,
    ImageRendererMixin,
    BarcodeMixin,
    TextLayoutMixin,
):
    def __init__(
        self,
        event_bus: EventBus,
        spool_dir: Path,
        local_config_getter: Callable[[], dict[str, Any]] | None = None,
        local_config_updater: Callable[..., dict[str, Any]] | None = None,
        customer_display_handler: Callable[[dict[str, Any]], None] | None = None,
        iot_identifier: str = "",
    ) -> None:
        self.event_bus = event_bus
        self.spool_dir = spool_dir
        self.local_config_getter = local_config_getter or (lambda: {})
        self.local_config_updater = local_config_updater
        self.customer_display_handler = customer_display_handler
        self.iot_identifier = str(iot_identifier or "").strip()
        self.devices: dict[str, Device] = {}
        # Last raw-TCP printer a discovery pass actually found, so one failed
        # probe does not drop a printer that is still there.
        self._last_known_network_printer: Device | None = None
        self._workspace_root = self._detect_workspace_root()
        self.resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", str(self._workspace_root)))
        self._fallback_site_packages_added = False
        self._devices_cached_at = 0.0
        self._device_refresh_interval_seconds = max(
            0.5, float(os.getenv("IOT_DEVICE_REFRESH_INTERVAL_SECONDS", "30"))
        )
        self._printer_action_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._printer_action_tasks: dict[str, asyncio.Task[None]] = {}
        self._printer_action_queue_max_size = max(
            1, int(os.getenv("IOT_PRINTER_ACTION_QUEUE_MAX_SIZE", "200"))
        )
        self._image_fetch_cache: dict[str, bytes] = {}
        self._escpos_raster_cache: dict[str, bytes] = {}
        # Detected printer command languages, keyed by "tcp:<host>:<port>" or
        # "windows:<queue>".  Entries expire after
        # IOT_PRINTER_LANGUAGE_TTL_SECONDS so a replaced printer is re-detected.
        self._printer_language_cache: dict[str, tuple[str, float]] = {}
        self._powershell_queues_cache: list[str] | None = self._load_printer_cache()
        self._runtime_logo_cache_buster = str(int(time()))

    def _trigger_printer_buzzer(self, device: Device) -> bool:
        """Send one short ESC/POS buzzer command without printing or cutting."""
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"buzzer_{int(time() * 1000)}.bin"
        # BEL is the common ESC/POS command for a printer's built-in buzzer.
        # It does not actuate the cash-drawer port or advance paper.
        target.write_bytes(b"\x07")
        try:
            sounded = self._send_raw_to_printer(device, target)
        except Exception:
            _logger.exception("ESC/POS buzzer command failed printer=%s", self._printer_name(device))
            return False
        _logger.info(
            "ESC/POS buzzer command sent printer=%s result=%s file=%s",
            self._printer_name(device) or "<unknown>",
            sounded,
            target,
        )
        return sounded
        # Ensure spool directory exists for cache files
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_devices()

    async def startup(self) -> None:
        self._clear_runtime_image_caches()
        self._runtime_logo_cache_buster = str(int(time()))

    async def shutdown(self) -> None:
        printer_tasks = list(self._printer_action_tasks.values())
        for task in printer_tasks:
            task.cancel()
        if printer_tasks:
            await asyncio.gather(*printer_tasks, return_exceptions=True)
        reject_queued_jobs(self._printer_action_queues)
        self._printer_action_tasks.clear()
        self._printer_action_queues.clear()

    def refresh_local_hardware(self) -> None:
        self._refresh_devices(force=True)

    def device_list(self) -> list[dict[str, Any]]:
        self._refresh_devices()
        # ``self.devices`` doubles as a lookup map, so one physical device can be
        # reachable under more than one key: ``_discover_printer_devices``
        # registers a discovered network printer under its own identifier *and*
        # under the ``printer_main`` alias, both pointing at the same Device.
        # Advertising both made clients -- the POS included -- see a single
        # printer twice and send every kitchen ticket twice, so report each
        # identifier once.  The aliases stay in ``self.devices`` for lookup.
        seen: set[str] = set()
        entries: list[dict[str, Any]] = []
        for d in self.devices.values():
            if d.identifier in seen:
                continue
            seen.add(d.identifier)
            entries.append({
                # Standard Odoo IoT Box field names (with device_ prefix)
                "device_identifier": d.identifier,
                "device_name": d.name,
                "device_type": d.type,
                "device_connection": d.connection,
                "device_subtype": d.subtype,
                "device_manufacturer": d.manufacturer,
                # Legacy/compat field names (without prefix)
                "identifier": d.identifier,
                "name": d.name,
                "type": d.type,
                "connection": d.connection,
                "subtype": d.subtype,
                "manufacturer": d.manufacturer,
                "status": d.status,
                "metadata": d.metadata,
            })
        return entries

    def external_device_identifier(self, local_identifier: str) -> str:
        local = str(local_identifier or "").strip()
        if not self.iot_identifier or not local:
            return local
        return f"{self.iot_identifier}__{local}"

    def local_device_identifier(self, external_identifier: str) -> str:
        external = str(external_identifier or "").strip()
        prefix = f"{self.iot_identifier}__" if self.iot_identifier else ""
        if prefix and external.startswith(prefix):
            return external[len(prefix):]
        # Backward compatibility for device records created by older builds.
        return external

    def as_odoo_devices_payload(self) -> dict[str, dict[str, Any]]:
        self._refresh_devices()
        payload: dict[str, dict[str, Any]] = {}
        for d in self.devices.values():
            entry: dict[str, Any] = {
                "name": d.name,
                "type": d.type,
                "connection": d.connection,
                "manufacturer": d.manufacturer,
                "subtype": d.subtype,
                "device_identifier": self.external_device_identifier(d.identifier),
                "device_name": d.name,
                "device_type": d.type,
                "device_connection": d.connection,
                "device_subtype": d.subtype,
            }
            metadata = d.metadata if isinstance(d.metadata, dict) else {}
            protocol = str(metadata.get("printer_protocol") or "").strip()
            if d.type == "printer" and protocol:
                # Lets the POS pick ZPL templates for label printers.
                entry["printer_protocol"] = protocol
            payload[self.external_device_identifier(d.identifier)] = entry
        return payload

    async def _queue_printer_action(
        self,
        owner: str,
        device: Device,
        data: dict[str, Any],
        handler_name: str,
    ) -> bool:
        queue = self._ensure_printer_action_queue(device.identifier)
        queued_at = time()
        queue_size = queue.qsize()
        action = str(data.get("action") or handler_name.lstrip("_"))
        if queue.full():
            _logger.error(
                "Printer action queue full owner=%s device=%s action=%s queue_size=%s max_size=%s",
                owner,
                device.identifier,
                action,
                queue_size,
                self._printer_action_queue_max_size,
            )
            dev_log(
                "printer_queue_full",
                owner=owner,
                device_identifier=device.identifier,
                printer=self._printer_name(device),
                action=action,
                queue_size=queue_size,
                max_size=self._printer_action_queue_max_size,
                action_summary=summarize_action(data),
            )
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message="ERROR_QUEUE_FULL",
                    result={"printer": self._printer_name(device), "mode": action},
                )
            )
            return False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        await queue.put(
            {
                "owner": owner,
                "device": device,
                "data": dict(data),
                "handler_name": handler_name,
                "future": future,
                "queued_at": queued_at,
            }
        )
        _logger.info(
            "Printer action enqueued owner=%s device=%s action=%s queue_before=%s queue_after=%s",
            owner,
            device.identifier,
            action,
            queue_size,
            queue.qsize(),
        )
        dev_log(
            "printer_action_enqueued",
            owner=owner,
            device_identifier=device.identifier,
            printer=self._printer_name(device),
            action=action,
            queue_before=queue_size,
            queue_after=queue.qsize(),
            action_summary=summarize_action(data),
        )
        return await future

    def _ensure_printer_action_queue(self, device_identifier: str) -> asyncio.Queue[dict[str, Any]]:
        queue = self._printer_action_queues.get(device_identifier)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._printer_action_queue_max_size)
            self._printer_action_queues[device_identifier] = queue
        task = self._printer_action_tasks.get(device_identifier)
        if task is None or task.done():
            task = asyncio.create_task(self._printer_action_worker_loop(device_identifier, queue))
            self._printer_action_tasks[device_identifier] = task
            _logger.info(
                "Printer action worker started device=%s max_queue_size=%s",
                device_identifier,
                self._printer_action_queue_max_size,
            )
        return queue

    async def _printer_action_worker(
        self,
        device_identifier: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        """Printer action worker 循环体。异常退出后由 _printer_action_worker_loop 自动重建。"""
        while True:
            job = await queue.get()
            owner = str(job.get("owner") or "")
            device = job.get("device")
            data = job.get("data") if isinstance(job.get("data"), dict) else {}
            handler_name = str(job.get("handler_name") or "")
            future = job.get("future")
            queued_at = float(job.get("queued_at") or time())
            started_at = time()
            action = str(data.get("action") or handler_name.lstrip("_"))
            try:
                _logger.info(
                    "Printer action start owner=%s device=%s action=%s queue_wait_ms=%.1f remaining_queue=%s",
                    owner,
                    device_identifier,
                    action,
                    (started_at - queued_at) * 1000,
                    queue.qsize(),
                )
                dev_log(
                    "printer_action_start",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    queue_wait_ms=round((started_at - queued_at) * 1000, 1),
                    remaining_queue=queue.qsize(),
                    action_summary=summarize_action(data),
                )
                if not isinstance(device, Device):
                    raise RuntimeError("queued printer device is invalid")
                handler = getattr(self, handler_name)
                result = await handler(owner, device, data)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_result(bool(result))
                _logger.info(
                    "Printer action done owner=%s device=%s action=%s result=%s duration_ms=%.1f remaining_queue=%s",
                    owner,
                    device_identifier,
                    action,
                    bool(result),
                    (time() - started_at) * 1000,
                    queue.qsize(),
                )
                dev_log(
                    "printer_action_done",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    result=bool(result),
                    duration_ms=round((time() - started_at) * 1000, 1),
                    remaining_queue=queue.qsize(),
                )
            except asyncio.CancelledError:
                if isinstance(future, asyncio.Future) and not future.done():
                    future.cancel()
                raise
            except Exception:
                dev_log(
                    "printer_action_exception",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    duration_ms=round((time() - started_at) * 1000, 1),
                    action_summary=summarize_action(data),
                )
                _logger.exception(
                    "Printer action failed owner=%s device=%s action=%s duration_ms=%.1f",
                    owner,
                    device_identifier,
                    action,
                    (time() - started_at) * 1000,
                )
                if isinstance(device, Device):
                    await self.event_bus.publish(
                        IoTEvent(
                            device_identifier=device.identifier,
                            owner=owner,
                            status="error",
                            message="ERROR_FAILED",
                            result={"printer": self._printer_name(device), "mode": action},
                        )
                    )
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_result(False)
            finally:
                queue.task_done()

    async def _printer_action_worker_loop(
        self,
        device_identifier: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        """Printer action worker 外层循环：worker 异常退出时自动重建。
        防止 queue.get() 抛出非 CancelledError 异常导致 worker 静默死亡。
        """
        while True:
            try:
                await self._printer_action_worker(device_identifier, queue)
            except asyncio.CancelledError:
                _logger.info(
                    "Printer action worker cancelled device=%s remaining_queue=%s",
                    device_identifier,
                    queue.qsize(),
                )
                break
            except Exception:
                _logger.exception(
                    "Printer action worker died unexpectedly device=%s remaining_queue=%s. "
                    "Restarting in 2 seconds...",
                    device_identifier,
                    queue.qsize(),
                )
                dev_log(
                    "printer_action_worker_died",
                    device_identifier=device_identifier,
                    remaining_queue=queue.qsize(),
                )
                await asyncio.sleep(2)

    async def execute(self, owner: str, device_identifier: str, data: dict[str, Any]) -> bool:
        requested_device_identifier = device_identifier
        device_identifier = self.local_device_identifier(device_identifier)
        generic_device_request = device_identifier == "printer"
        if generic_device_request:
            self._refresh_devices_if_needed()
            device_identifier = self._first_device_by_type(device_identifier) or device_identifier

        device = self.devices.get(device_identifier)
        if not device:
            # First try a gentler refresh (keeps PowerShell cache if it's fresh)
            self._refresh_devices_if_needed()
            if generic_device_request:
                device_identifier = self._first_device_by_type(requested_device_identifier) or requested_device_identifier
            device = self.devices.get(device_identifier)

        # Odoo's HWPrinter endpoint does not include a device identifier, so
        # those requests arrive as ``printer_main``.  A cache created before a
        # network printer was discovered can still contain the placeholder
        # printer_main device, which has no usable backend.  Refresh once and
        # route the alias to a real Windows or raw-TCP printer before queuing
        # the job; otherwise Odoo reports ERROR_PRINTER on the first print
        # after the IoT service starts.
        if (
            device
            and requested_device_identifier in {"printer", "printer_main"}
            and device.type == "printer"
            and not self._printer_has_usable_backend(device)
        ):
            self._refresh_devices(force=True)
            usable_identifier = self._first_usable_printer_identifier()
            if usable_identifier:
                device_identifier = usable_identifier
                device = self.devices.get(device_identifier)
        if not device:
            refresh_started_at = time()
            self._refresh_devices(force=True)
            _logger.info(
                "Device execute refreshed devices owner=%s requested=%s action=%s refresh_ms=%.1f",
                owner,
                requested_device_identifier,
                data.get("action", ""),
                (time() - refresh_started_at) * 1000,
            )
            if generic_device_request:
                device_identifier = self._first_device_by_type(requested_device_identifier) or requested_device_identifier
            device = self.devices.get(device_identifier)
        if not device:
            _logger.warning(
                "Device execute failed because device was not found owner=%s requested=%s resolved=%s action=%s",
                owner,
                requested_device_identifier,
                device_identifier,
                data.get("action", ""),
            )
            dev_log(
                "device_execute_missing_device",
                owner=owner,
                requested_device_identifier=requested_device_identifier,
                resolved_device_identifier=device_identifier,
                action=data.get("action", ""),
                available_devices=list(self.devices.keys()),
                action_summary=summarize_action(data),
            )
            return False

        _logger.info(
            "Device execute owner=%s requested=%s resolved=%s type=%s action=%s",
            owner,
            requested_device_identifier,
            device_identifier,
            device.type,
            data.get("action", ""),
        )
        _logger.debug(
            "Device execute detail owner=%s device=%s printer_name=%s action=%s "
            "has_receipt=%s backend=%s connection=%s metadata=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<none>",
            data.get("action", ""),
            "yes" if data.get("receipt") else "no",
            device.metadata.get("backend") if isinstance(device.metadata, dict) else "<none>",
            device.connection,
            {k: v for k, v in device.metadata.items() if k in ("windows_printer", "backend", "raw_tcp_host")}
            if isinstance(device.metadata, dict) else {},
        )
        dev_log(
            "device_execute",
            owner=owner,
            requested_device_identifier=requested_device_identifier,
            resolved_device_identifier=device_identifier,
            device_type=device.type,
            device_connection=device.connection,
            printer=self._printer_name(device),
            action=data.get("action", ""),
            device_metadata=device.metadata,
            action_summary=summarize_action(data),
        )

        action = data.get("action", "")
        data = self._normalize_label_action(owner, device, data)
        if data is None:
            return False
        action = data.get("action", action)
        if device.type == "display":
            if self.customer_display_handler is not None:
                try:
                    self.customer_display_handler(data)
                except Exception:
                    _logger.exception("Customer display update failed action=%s", action)
                    return False
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="success",
                    result={"action": action, "ok": True},
                )
            )
            return True
        if action == "print_receipt" and self._is_native_receipt_image_action(data):
            if self._can_submit_printer_action_directly(device):
                return await self._submit_printer_action_directly(owner, device, data, "_print_receipt_native_image")
            return await self._queue_printer_action(owner, device, data, "_print_receipt_native_image")
        if action in {"print_receipt", "print_receipt_escpos"}:
            handler_name = self._printer_receipt_handler(device, data)
            if self._can_submit_printer_action_directly(device):
                return await self._submit_printer_action_directly(owner, device, data, handler_name)
            return await self._queue_printer_action(owner, device, data, handler_name)
        if action == "cashbox":
            opened = self._open_cashbox(device)
            status = "success" if opened else "error"
            message = None if opened else "ERROR_PRINTER"
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status=status,
                    message=message,
                    result={"cashbox": "opened" if opened else "failed"},
                )
            )
            return True
        if action in {"beep", "buzzer", "alarm"}:
            sounded = await asyncio.to_thread(self._trigger_printer_buzzer, device)
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="success" if sounded else "error",
                    message=None if sounded else "ERROR_PRINTER",
                    result={"buzzer": "triggered" if sounded else "failed"},
                )
            )
            return sounded

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"action": action, "ok": True},
            )
        )
        _logger.info(
            "Device execute default-success owner=%s device_identifier=%s action=%s",
            owner,
            device.identifier,
            action,
        )
        dev_log("device_execute_default_success", owner=owner, device_identifier=device.identifier, action=action)
        return True

    def _first_device_by_type(self, device_type: str) -> str | None:
        if device_type == "printer":
            return self._first_direct_printer_identifier()
        for d in self.devices.values():
            if d.type == device_type:
                return d.identifier
        return None

    def _first_direct_printer_identifier(self) -> str | None:
        configured_identifier = self._configured_printer_identifier()
        if configured_identifier and configured_identifier in self.devices:
            device = self.devices.get(configured_identifier)
            if device and device.type == "printer" and device.connection == "direct":
                return configured_identifier
        for d in self.devices.values():
            if d.type == "printer" and d.connection == "direct":
                return d.identifier
        for d in self.devices.values():
            if d.type == "printer":
                return d.identifier
        return None

    def _printer_has_usable_backend(self, device: Device) -> bool:
        if device.type != "printer":
            return False
        if self._raw_tcp_endpoint(device):
            return True
        return str(device.metadata.get("backend") or "").strip().lower() == "windows"

    def _first_usable_printer_identifier(self) -> str | None:
        configured_identifier = self._configured_printer_identifier()
        if configured_identifier:
            configured = self.devices.get(configured_identifier)
            if configured and self._printer_has_usable_backend(configured):
                return configured_identifier
        for identifier, device in self.devices.items():
            if self._printer_has_usable_backend(device):
                return identifier
        return None

    def _can_submit_printer_action_directly(self, device: Device) -> bool:
        if self._raw_tcp_endpoint(device):
            return False
        return str(device.metadata.get("backend") or "").strip().lower() == "windows"

    async def _submit_printer_action_directly(
        self,
        owner: str,
        device: Device,
        data: dict[str, Any],
        handler_name: str,
    ) -> bool:
        started_at = time()
        action = str(data.get("action") or handler_name.lstrip("_"))
        _logger.info(
            "Printer action direct start owner=%s device=%s action=%s backend=%s",
            owner,
            device.identifier,
            action,
            str(device.metadata.get("backend") or ""),
        )
        dev_log(
            "printer_action_direct_start",
            owner=owner,
            device_identifier=device.identifier,
            action=action,
            action_summary=summarize_action(data),
        )
        try:
            handler = getattr(self, handler_name)
            result = await handler(owner, device, data)
        except Exception:
            dev_log(
                "printer_action_direct_exception",
                owner=owner,
                device_identifier=device.identifier,
                action=action,
                duration_ms=round((time() - started_at) * 1000, 1),
                action_summary=summarize_action(data),
            )
            _logger.exception(
                "Printer action direct failed owner=%s device=%s action=%s duration_ms=%.1f",
                owner,
                device.identifier,
                action,
                (time() - started_at) * 1000,
            )
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message="ERROR_FAILED",
                    result={"printer": self._printer_name(device), "mode": action},
                )
            )
            return True
        _logger.info(
            "Printer action direct done owner=%s device=%s action=%s result=%s duration_ms=%.1f",
            owner,
            device.identifier,
            action,
            bool(result),
            (time() - started_at) * 1000,
        )
        dev_log(
            "printer_action_direct_done",
            owner=owner,
            device_identifier=device.identifier,
            action=action,
            result=bool(result),
            duration_ms=round((time() - started_at) * 1000, 1),
        )
        return bool(result)
