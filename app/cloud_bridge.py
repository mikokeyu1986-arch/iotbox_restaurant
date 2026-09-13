from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import ssl
import time
from http.cookies import SimpleCookie
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

# ============================================================
# 专用线程池：隔离 Cloud Bridge 的阻塞 IO 操作，避免耗尽
# asyncio 默认线程池导致整台 Box HTTP 服务假死。
# 场景：Odoo 8070 变慢时 _fetch_session_id(10s) +
# _send_operation_confirmation(8s) 同时抢占默认池 8 线程 → 池满 → 所有 to_thread 排队。
# ============================================================
_CLOUD_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_CONFIRM_SEMAPHORE = asyncio.Semaphore(2)  # 最多 2 个确认请求并发

# ------------------------------------------------------------
# 自定义 opener：禁止 HTTP 重定向跟随。
# Odoo /web/login 返回 302 时，默认 urlopen 会跟随重定向，
# 可能触发无限循环。我们需要从 302 响应的 Set-Cookie 中提取 session_id。
# ------------------------------------------------------------
class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不跟随任何重定向

    def http_error_302(self, req, fp, code, msg, headers):
        # http_error_* 回调只接收 6 个参数，重定向目标需要先从
        # Location/URI 头中提取，再交给 7 参数的 redirect_request。
        if "location" in headers:
            newurl = headers["location"]
        elif "uri" in headers:
            newurl = headers["uri"]
        else:
            return None
        return self.redirect_request(req, fp, code, msg, headers, newurl)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())

from .config_store import ConfigStore
from .dev_logger import dev_log
from .device_manager import DeviceManager
from .odoo_sync import OdooSyncService


_logger = logging.getLogger(__name__)


class ReconnectRequested(Exception):
    pass


class OdooCloudBridge:
    """Outbound websocket bridge for cloud Odoo setups.

    The runtime keeps an active websocket connection to Odoo and receives
    `iot_action` messages from the IoT channel, then executes local device
    actions and sends operation confirmations back to `/iot/box/send_websocket`.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        device_manager: DeviceManager,
        iot_identifier: str,
        iot_ip: str,
        iot_version: str,
        verify_ssl: bool = True,
    ) -> None:
        self.config_store = config_store
        self.device_manager = device_manager
        self.iot_identifier = iot_identifier
        self.iot_ip = iot_ip
        self.iot_version = iot_version
        self.verify_ssl = verify_ssl
        self.odoo_sync = OdooSyncService(verify_ssl=verify_ssl)
        self._stop_event = asyncio.Event()
        self.connected = False
        self.last_error = ""
        self.last_server_url = ""
        self.last_channel = ""
        self._active_ws: Any | None = None
        self._action_tasks: set[asyncio.Task[None]] = set()
        self._confirmation_log_path = self.config_store.config_path.parent / "logs" / "operation_confirmations.jsonl"
        self._reconnect_delay_seconds = 1.0
        self._ws_ping_interval = max(5.0, float(os.getenv("IOT_WS_PING_INTERVAL", "30")))
        self._ws_ping_timeout = max(5.0, float(os.getenv("IOT_WS_PING_TIMEOUT", "60")))
        self._ws_open_timeout = max(5.0, float(os.getenv("IOT_WS_OPEN_TIMEOUT", "20")))
        self._ws_close_timeout = max(1.0, float(os.getenv("IOT_WS_CLOSE_TIMEOUT", "5")))
        self._ws_max_size = max(1024 * 1024, int(os.getenv("IOT_WS_MAX_SIZE", str(16 * 1024 * 1024))))
        self._ws_max_queue = max(16, int(os.getenv("IOT_WS_MAX_QUEUE", "256")))
        # ``websockets`` already verifies a live connection with ping/pong.
        # Business messages can legitimately be absent for long periods, so
        # this watchdog is opt-in rather than treating an idle kitchen as a
        # broken connection.
        self._ws_last_message_at = 0.0
        try:
            self._ws_receive_timeout = max(0.0, float(os.getenv("IOT_WS_RECEIVE_TIMEOUT", "0")))
        except ValueError:
            self._ws_receive_timeout = 0.0

    # ------------------------------------------------------------------
    # Dedicated thread pool helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_cloud_executor() -> concurrent.futures.ThreadPoolExecutor:
        global _CLOUD_EXECUTOR
        if _CLOUD_EXECUTOR is None:
            _CLOUD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="odoo-cloud"
            )
        return _CLOUD_EXECUTOR

    @staticmethod
    async def _cloud_to_thread(fn, *args):
        """Run blocking IO in the dedicated cloud executor, NOT the default pool."""
        executor = OdooCloudBridge._ensure_cloud_executor()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, fn, *args)

    @staticmethod
    def shutdown_cloud_executor() -> None:
        global _CLOUD_EXECUTOR
        if _CLOUD_EXECUTOR is not None:
            _CLOUD_EXECUTOR.shutdown(wait=True)
            _CLOUD_EXECUTOR = None

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            connection = self.config_store.get_connection()
            server_url = str(connection.get("url") or "").rstrip("/")
            iot_channel = str(connection.get("iot_channel") or "").strip()
            db_name = str(connection.get("db_name") or "").strip()
            last_message_id = int(connection.get("last_websocket_message_id") or 0)
            self.last_server_url = server_url
            self.last_channel = iot_channel

            if not server_url or not iot_channel:
                self.connected = False
                self.last_error = "Waiting for Odoo pairing"
                dev_log(
                    "cloud_bridge_waiting",
                    server_url=server_url,
                    has_iot_channel=bool(iot_channel),
                    db_name=db_name,
                    last_message_id=last_message_id,
                )
                await asyncio.sleep(3)
                continue

            try:
                # 使用专用线程池 + 超时保护，防止 Odoo 8070 慢时
                # _fetch_session_id 阻塞默认 asyncio 线程池导致整台 Box 假死
                session_id = await asyncio.wait_for(
                    self._cloud_to_thread(self._fetch_session_id, server_url, db_name),
                    timeout=15.0,
                )
                await self._ws_loop(server_url, iot_channel, last_message_id, session_id)
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                self.connected = False
                self.last_error = str(exc)
                dev_log("cloud_bridge_invalid_status", server_url=server_url, error=str(exc))
                _logger.warning(
                    "Cloud bridge websocket rejected by server server_url=%s ws_url=%s error=%s",
                    server_url,
                    self._to_websocket_url(server_url),
                    exc,
                )
                await self._sleep_before_reconnect("invalid_status", 5.0)
            except ReconnectRequested:
                self.connected = False
                self.last_error = "IoT registration refreshed; reconnecting"
                await self._sleep_before_reconnect("registration_refresh", 1.0)
            except ConnectionClosed as exc:
                self.connected = False
                code = getattr(exc, "code", None)
                reason = getattr(exc, "reason", "") or "no close reason"
                self.last_error = f"Websocket closed code={code} reason={reason}"
                dev_log("cloud_bridge_closed", server_url=server_url, code=code, reason=reason)
                _logger.warning(
                    "Cloud bridge websocket closed code=%s reason=%s pending_tasks=%s",
                    code,
                    reason,
                    len(self._action_tasks),
                )
                await self._sleep_before_reconnect("connection_closed", 1.0)
            except WebSocketException as exc:
                self.connected = False
                self.last_error = str(exc) or exc.__class__.__name__
                dev_log("cloud_bridge_websocket_error", server_url=server_url, error_type=exc.__class__.__name__, error=str(exc))
                _logger.warning(
                    "Cloud bridge websocket error type=%s error=%s pending_tasks=%s",
                    exc.__class__.__name__,
                    exc,
                    len(self._action_tasks),
                )
                await self._sleep_before_reconnect("websocket_error", 2.0)
            except Exception:
                self.connected = False
                self.last_error = "Unexpected websocket failure"
                dev_log("cloud_bridge_unexpected_error", server_url=server_url)
                _logger.exception("Cloud bridge disconnected unexpectedly")
                await self._sleep_before_reconnect("unexpected_error", 5.0)

    async def _sleep_before_reconnect(self, reason: str, base_delay: float | None = None) -> None:
        if self._stop_event.is_set():
            return
        delay = self._reconnect_delay_seconds if base_delay is None else base_delay
        delay = min(max(delay, 1.0), 30.0)
        _logger.info(
            "Cloud bridge reconnect scheduled reason=%s delay_seconds=%.1f pending_tasks=%s",
            reason,
            delay,
            len(self._action_tasks),
        )
        dev_log(
            "cloud_bridge_reconnect_scheduled",
            reason=reason,
            delay_seconds=round(delay, 1),
            pending_tasks=len(self._action_tasks),
        )
        await asyncio.sleep(delay)
        self._reconnect_delay_seconds = min(delay * 1.5, 30.0)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._active_ws is not None:
            await self._active_ws.close()
        for task in list(self._action_tasks):
            task.cancel()
        if self._action_tasks:
            await asyncio.gather(*list(self._action_tasks), return_exceptions=True)

    async def request_reconnect(self, *, message: str = "Connection settings changed; reconnecting") -> None:
        self.last_error = message
        self.connected = False
        dev_log(
            "cloud_bridge_reconnect_requested",
            message=message,
            server_url=self.last_server_url,
            iot_channel=self.last_channel,
            pending_tasks=len(self._action_tasks),
        )
        if self._active_ws is not None:
            await self._active_ws.close()

    async def disconnect(self, *, message: str = "Disconnected from Odoo") -> dict[str, Any]:
        self.last_error = message
        self.last_server_url = ""
        self.last_channel = ""
        # An unbind clears the machine configuration too, not just the pairing:
        # the box is being handed to a new deployment.  The failed-connect paths
        # in main.py deliberately call reset_connection() instead, which leaves
        # the local configuration intact.
        connection = self.config_store.reset_for_unbind(message=message)
        if self._active_ws is not None:
            await self._active_ws.close()
        self.connected = False
        return connection

    async def _ws_loop(self, server_url: str, iot_channel: str, last_message_id: int, session_id: str) -> None:
        ws_url = self._to_websocket_url(server_url)
        headers = [("User-Agent", "CustomIoTBoxRuntime/1.0")]
        headers.append(("Origin", server_url))
        if session_id:
            headers.append(("Cookie", f"session_id={session_id}"))
        latest_message_id = last_message_id

        ssl_context = None
        if not self.verify_ssl and ws_url.startswith("wss://"):
            ssl_context = ssl._create_unverified_context()
            _logger.debug(
                "Cloud bridge using unverified SSL context ws_url=%s verify_ssl=%s",
                ws_url,
                self.verify_ssl,
            )

        _logger.info(
            "Cloud bridge connecting ws_url=%s verify_ssl=%s ping_interval=%s open_timeout=%s",
            ws_url,
            ssl_context is not None or self.verify_ssl,
            self._ws_ping_interval,
            self._ws_open_timeout,
        )
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=headers,
                ping_interval=self._ws_ping_interval,
                ping_timeout=self._ws_ping_timeout,
                open_timeout=self._ws_open_timeout,
                close_timeout=self._ws_close_timeout,
                max_queue=self._ws_max_queue,
                max_size=self._ws_max_size,
                ssl=ssl_context,
            ) as ws:
                self._active_ws = ws
                self.connected = True
                self.last_error = ""
                self._reconnect_delay_seconds = 1.0
                self._ws_last_message_at = asyncio.get_running_loop().time()
                await ws.send(
                    json.dumps(
                        {
                            "event_name": "subscribe",
                            "data": {
                                "channels": [iot_channel],
                                "last": last_message_id,
                                "identifier": self.iot_identifier,
                            },
                        }
                    )
                )
                _logger.info("Cloud bridge connected to %s on channel %s", server_url, iot_channel)
                dev_log(
                    "cloud_bridge_connected",
                    server_url=server_url,
                    iot_channel=iot_channel,
                    last_message_id=last_message_id,
                    session_id=session_id,
                )

                # 保活监控协程：如果超过 receive_timeout 无消息，主动关闭连接触发重连
                keepalive_task = asyncio.create_task(self._ws_keepalive_watchdog(ws))
                try:
                    async for raw in ws:
                        self._ws_last_message_at = asyncio.get_running_loop().time()
                        raw_started_at = self._ws_last_message_at
                        messages = self._parse_messages(raw)
                        raw_size = len(raw) if isinstance(raw, (str, bytes, bytearray)) else 0
                        _logger.info(
                            "Cloud bridge websocket message received bytes=%s messages=%s pending_tasks=%s",
                            raw_size,
                            len(messages),
                            len(self._action_tasks),
                        )
                        dev_log(
                            "cloud_bridge_message",
                            raw_bytes=raw_size,
                            message_count=len(messages),
                            pending_tasks=len(self._action_tasks),
                        )
                        for message in messages:
                            msg_id = int(message.get("id") or 0)
                            message_type = str(message.get("message", {}).get("type") or "unknown") if isinstance(message.get("message"), dict) else "unknown"
                            _logger.debug(
                                "Cloud bridge processing message id=%s type=%s raw_bytes=%s",
                                msg_id,
                                message_type,
                                raw_size,
                            )
                            should_reconnect = await self._handle_and_commit_message(server_url, message)
                            if msg_id:
                                latest_message_id = msg_id
                            if should_reconnect:
                                raise ReconnectRequested()
                        duration_ms = (asyncio.get_running_loop().time() - raw_started_at) * 1000
                        if duration_ms > 1000:
                            _logger.warning(
                                "Cloud bridge websocket message handling slow duration_ms=%.1f messages=%s pending_tasks=%s",
                                duration_ms,
                                len(messages),
                                len(self._action_tasks),
                            )
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass
        finally:
            self._active_ws = None
            self.connected = False
            self.config_store.update_last_websocket_message_id(latest_message_id, force=True)

    async def _handle_and_commit_message(self, server_url: str, message: dict[str, Any]) -> bool:
        """Handle one message completely before advancing its replay cursor."""
        should_reconnect = await self._handle_message(server_url, message)
        message_id = int(message.get("id") or 0)
        if message_id:
            self.config_store.update_last_websocket_message_id(message_id)
        return should_reconnect

    async def _ws_keepalive_watchdog(self, ws: Any) -> None:
        """保活监控：定期检查最后一次收到消息的时间。
        如果超过 _ws_receive_timeout 秒无消息，认为连接已假死，强制关闭 WebSocket 触发重连。
        """
        if self._ws_receive_timeout <= 0:
            return
        check_interval = max(10.0, self._ws_ping_interval)
        try:
            while True:
                await asyncio.sleep(check_interval)
                now = asyncio.get_running_loop().time()
                elapsed = now - self._ws_last_message_at
                if elapsed > self._ws_receive_timeout:
                    _logger.error(
                        "Cloud bridge keepalive timeout: no message received for %.1fs (limit %.1fs). "
                        "Force closing websocket to trigger reconnect.",
                        elapsed,
                        self._ws_receive_timeout,
                    )
                    dev_log(
                        "cloud_bridge_keepalive_timeout",
                        idle_seconds=round(elapsed, 1),
                        limit_seconds=self._ws_receive_timeout,
                    )
                    # 强制关闭连接，使 async for 循环退出，触发外层重连逻辑
                    await ws.close(1001, "keepalive timeout")
                    return
                _logger.debug(
                    "Cloud bridge keepalive check: idle=%.1fs limit=%.1fs ok",
                    elapsed,
                    self._ws_receive_timeout,
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            _logger.exception("Cloud bridge keepalive watchdog unexpected error")
            # 发生意外错误时也关闭连接
            try:
                await ws.close(1011, "keepalive watchdog error")
            except Exception:
                pass

    async def _handle_message(self, server_url: str, message: dict[str, Any]) -> bool:
        started_at = asyncio.get_running_loop().time()
        message_data = message.get("message") or {}
        message_type = str(message_data.get("type") or "")
        payload = message_data.get("payload") or {}
        if not isinstance(payload, dict):
            return False

        iot_identifiers = payload.get("iot_identifiers") or []
        if self.iot_identifier not in iot_identifiers:
            _logger.debug(
                "Cloud bridge message skipped (iot_identifier mismatch) message_type=%s "
                "expected_identifier=%s message_identifiers=%s",
                message_type,
                self.iot_identifier,
                iot_identifiers,
            )
            return False

        if message_type == "server_clear":
            await self._cloud_to_thread(self._re_register_after_server_clear, server_url)
            return True
        if message_type != "iot_action":
            return False

        session_id = str(payload.get("session_id") or "0")
        device_identifiers = payload.get("device_identifiers") or []
        if not device_identifiers and payload.get("device_identifier"):
            device_identifiers = [payload.get("device_identifier")]

        action_data = self._extract_action_data(payload)
        receipt_summary = self._summarize_action_data(action_data)
        _logger.info(
            "Cloud bridge iot_action received session_id=%s devices=%s action=%s action_unique_id=%s "
            "receipt_type=%s receipt_ref=%s product_count=%s products=%s fingerprint=%s payload_keys=%s",
            session_id,
            [str(device_identifier) for device_identifier in device_identifiers],
            str(action_data.get("action") or ""),
            str(action_data.get("action_unique_id") or ""),
            receipt_summary["receipt_type"],
            receipt_summary["receipt_ref"],
            receipt_summary["product_count"],
            receipt_summary["products"],
            receipt_summary["fingerprint"],
            sorted(action_data.keys()),
        )
        _logger.debug(
            "Cloud bridge iot_action FULL payload server_url=%s session_id=%s devices=%s action=%s "
            "receipt_has_structured=%s receipt_has_lines=%s receipt_keys=%s "
            "payload_has_receipt=%s receipt_type=%s",
            server_url,
            session_id,
            [str(d) for d in device_identifiers],
            str(action_data.get("action") or ""),
            "yes" if isinstance(action_data.get("receipt"), dict) and action_data["receipt"].get("structured") else "no",
            "yes" if isinstance(action_data.get("receipt"), dict) and action_data["receipt"].get("lines") else "no",
            sorted(action_data.get("receipt", {}).keys()) if isinstance(action_data.get("receipt"), dict) else [],
            "yes" if action_data.get("receipt") else "no",
            receipt_summary["receipt_type"],
        )
        if action_data.get("receipt"):
            receipt = action_data["receipt"]
            if isinstance(receipt, dict):
                _logger.debug(
                    "Cloud bridge receipt detail structured=%s lines_count=%s items_count=%s "
                    "total_line=%s customer=%s",
                    "yes" if receipt.get("structured") else "no",
                    len(receipt.get("lines") or []) if isinstance(receipt.get("lines"), list) else 0,
                    len(receipt.get("structured", {}).get("items") or []) if isinstance(receipt.get("structured"), dict) else 0,
                    bool(receipt.get("structured", {}).get("total_line")) if isinstance(receipt.get("structured"), dict) else False,
                    "yes" if receipt.get("structured", {}).get("customer") else "no",
                )
        dev_log(
            "cloud_bridge_iot_action_received",
            server_url=server_url,
            session_id=session_id,
            device_identifiers=[str(device_identifier) for device_identifier in device_identifiers],
            action=str(action_data.get("action") or ""),
            action_unique_id=str(action_data.get("action_unique_id") or ""),
            receipt_summary=receipt_summary,
            payload_keys=sorted(action_data.keys()),
        )
        action_tasks: list[asyncio.Task[None]] = []
        for device_identifier in device_identifiers:
            resolved_device = str(device_identifier)
            task = asyncio.create_task(
                self._execute_and_confirm(
                    server_url,
                    session_id,
                    resolved_device,
                    action_data,
                )
            )
            self._track_action_task(task)
            action_tasks.append(task)
        _logger.info(
            "Cloud bridge iot_action scheduled session_id=%s devices=%s action=%s schedule_ms=%.1f pending_tasks=%s",
            session_id,
            len(device_identifiers),
            str(action_data.get("action") or ""),
            (asyncio.get_running_loop().time() - started_at) * 1000,
            len(self._action_tasks),
        )
        if action_tasks:
            await asyncio.gather(*action_tasks)
        return False

    def _track_action_task(self, task: asyncio.Task[None]) -> None:
        self._action_tasks.add(task)
        task.add_done_callback(self._finish_action_task)
        _logger.info("Cloud bridge action task tracked pending_tasks=%s", len(self._action_tasks))

    def _finish_action_task(self, task: asyncio.Task[None]) -> None:
        self._action_tasks.discard(task)
        if task.cancelled():
            _logger.info("Cloud bridge action task cancelled pending_tasks=%s", len(self._action_tasks))
            return
        exc = task.exception()
        if exc is not None:
            _logger.error(
                "Cloud bridge action task failed pending_tasks=%s",
                len(self._action_tasks),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        _logger.info("Cloud bridge action task finished pending_tasks=%s", len(self._action_tasks))

    async def _execute_and_confirm(
        self,
        server_url: str,
        session_id: str,
        device_identifier: str,
        action_data: dict[str, Any],
    ) -> None:
        started_at = asyncio.get_running_loop().time()
        action = str(action_data.get("action") or "")
        status = "disconnected"
        _logger.info(
            "Cloud bridge execute start session_id=%s device_identifier=%s action=%s pending_tasks=%s",
            session_id,
            device_identifier,
            action or "<none>",
            len(self._action_tasks),
        )
        _logger.debug(
            "Cloud bridge execute action_data_detail action=%s action_unique_id=%s "
            "receipt_type=%s device_identifier=%s data_keys=%s",
            action,
            str(action_data.get("action_unique_id") or ""),
            str(action_data.get("action") or ""),
            device_identifier,
            sorted(action_data.keys()),
        )
        dev_log(
            "cloud_bridge_execute_start",
            server_url=server_url,
            session_id=session_id,
            device_identifier=device_identifier,
            action=action,
            pending_tasks=len(self._action_tasks),
        )
        try:
            success = await self.device_manager.execute(session_id, device_identifier, action_data)
            status = "success" if success else "disconnected"
            _logger.info(
                "Cloud bridge execute result session_id=%s device_identifier=%s action=%s success=%s duration_ms=%.1f",
                session_id,
                device_identifier,
                action or "<none>",
                success,
                (asyncio.get_running_loop().time() - started_at) * 1000,
            )
            _logger.debug(
                "Cloud bridge execute completed session_id=%s device_identifier=%s action=%s "
                "success=%s status=%s duration_ms=%.1f",
                session_id,
                device_identifier,
                action,
                success,
                status,
                (asyncio.get_running_loop().time() - started_at) * 1000,
            )
            dev_log(
                "cloud_bridge_execute_result",
                server_url=server_url,
                session_id=session_id,
                device_identifier=device_identifier,
                action=action,
                success=success,
                status=status,
                duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000, 1),
            )
        except Exception as exc:
            dev_log(
                "cloud_bridge_execute_exception",
                server_url=server_url,
                session_id=session_id,
                device_identifier=device_identifier,
                action=action,
            )
            _logger.exception(
                "Cloud bridge execute failed session_id=%s device_identifier=%s action=%s "
                "error_type=%s error=%s",
                session_id,
                device_identifier,
                action or "<none>",
                type(exc).__name__,
                str(exc),
            )
        try:
            # Semaphore 限流：最多 2 个确认请求同时进行，
            # 防止 Odoo 推送大量 action 时确认请求耗尽线程池
            async with _CONFIRM_SEMAPHORE:
                confirm_started_at = asyncio.get_running_loop().time()
                await self._cloud_to_thread(
                    self._send_operation_confirmation,
                    server_url,
                    session_id,
                    device_identifier,
                    status,
                    action_data,
                )
            _logger.info(
                "Cloud bridge confirmation sent session_id=%s device_identifier=%s action=%s status=%s duration_ms=%.1f",
                session_id,
                device_identifier,
                action or "<none>",
                status,
                (asyncio.get_running_loop().time() - confirm_started_at) * 1000,
            )
            dev_log(
                "cloud_bridge_confirmation_sent",
                server_url=server_url,
                session_id=session_id,
                device_identifier=device_identifier,
                action=action,
                status=status,
                duration_ms=round((asyncio.get_running_loop().time() - confirm_started_at) * 1000, 1),
            )
        except Exception:
            dev_log(
                "cloud_bridge_confirmation_failed",
                server_url=server_url,
                session_id=session_id,
                device_identifier=device_identifier,
                status=status,
            )
            _logger.exception(
                "Cloud bridge confirmation failed session_id=%s device_identifier=%s status=%s",
                session_id,
                device_identifier,
                status,
            )
            raise
        _logger.info(
            "Cloud bridge action done session_id=%s device_identifier=%s action=%s status=%s total_ms=%.1f pending_tasks=%s",
            session_id,
            device_identifier,
            action or "<none>",
            status,
            (asyncio.get_running_loop().time() - started_at) * 1000,
            len(self._action_tasks),
        )

    def _extract_action_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        nested = payload.get("data")
        if isinstance(nested, dict):
            return dict(nested)
        action_data = dict(payload)
        for key in ("iot_identifiers", "iot_identifier", "session_id", "device_identifiers", "device_identifier"):
            action_data.pop(key, None)
        return action_data

    def _summarize_action_data(self, action_data: dict[str, Any]) -> dict[str, Any]:
        receipt = action_data.get("receipt")
        if not isinstance(receipt, dict):
            return {
                "receipt_type": "none",
                "receipt_ref": "",
                "product_count": 0,
                "products": "",
                "fingerprint": self._short_fingerprint(action_data),
            }

        structured = receipt.get("structured") if isinstance(receipt.get("structured"), dict) else {}
        raw_lines = receipt.get("lines") if isinstance(receipt.get("lines"), list) else []
        raw_items = structured.get("items") if isinstance(structured.get("items"), list) else []
        products = self._extract_product_preview(raw_lines, raw_items)
        receipt_ref = self._extract_receipt_reference(raw_lines, structured)
        has_kitchen_product = any(
            isinstance(line, dict)
            and (
                str(line.get("type") or "") == "product_line"
                or (
                    isinstance(line.get("classes"), list)
                    and "kitchen-product-line" in [str(cls) for cls in line.get("classes") or []]
                )
            )
            for line in raw_lines
        )
        has_total = bool(structured.get("total_line")) or any(
            isinstance(line, dict)
            and "total" in str(line.get("text") or line.get("left_text") or "").strip().lower()
            for line in raw_lines
        )
        if has_kitchen_product:
            receipt_type = "kitchen"
        elif products and has_total:
            receipt_type = "pos_receipt"
        elif products:
            receipt_type = "product_receipt"
        elif has_total:
            receipt_type = "total_receipt"
        else:
            receipt_type = "unknown"
        return {
            "receipt_type": receipt_type,
            "receipt_ref": receipt_ref,
            "product_count": len(products),
            "products": " | ".join(products[:8]),
            "fingerprint": self._short_fingerprint(receipt),
        }

    def _extract_product_preview(self, lines: list[Any], items: list[Any]) -> list[str]:
        products: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = str(item.get("qty") or "").strip()
            name = str(item.get("name") or "").strip()
            if name:
                products.append(f"{qty} {name}".strip())
        for line in lines:
            if not isinstance(line, dict) or str(line.get("type") or "") != "product_line":
                continue
            qty = str(line.get("qty") or "").strip()
            name = str(line.get("name") or "").strip()
            if name:
                products.append(f"{qty} {name}".strip())
        return products

    def _extract_receipt_reference(self, lines: list[Any], structured: dict[str, Any]) -> str:
        for key in ("name", "order_name", "pos_reference", "reference", "tracking_number", "table"):
            value = str(structured.get(key) or "").strip()
            if value:
                return value[:80]
        candidates: list[str] = []
        for line in lines[:10]:
            if not isinstance(line, dict):
                continue
            if str(line.get("type") or "") == "header_meta_line":
                left = str(line.get("left_text") or "").strip()
                right = str(line.get("right_text") or "").strip()
                text = " ".join(part for part in (left, right) if part)
            else:
                text = str(line.get("text") or "").strip()
            if text and (text.startswith("#") or "MESA" in text.upper() or "TABLE" in text.upper()):
                candidates.append(text)
        return " | ".join(candidates[:3])[:120]

    def _short_fingerprint(self, value: Any) -> str:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:
            raw = str(value)
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]

    def _send_operation_confirmation(
        self,
        server_url: str,
        session_id: str,
        device_identifier: str,
        status: str,
        action_data: dict[str, Any],
    ) -> None:
        """Acknowledge an IoT action and reject silent Odoo-side failures.

        The POS correlates this message with ``session_id``.  Sending an empty
        result made a successful local kitchen print indistinguishable from an
        incomplete operation to some Odoo channel implementations.
        """
        endpoint = f"{server_url}/iot/box/send_websocket"
        action = str(action_data.get("action") or "")
        action_unique_id = str(action_data.get("action_unique_id") or "")
        params = {
            "session_id": session_id,
            "iot_box_identifier": self.iot_identifier,
            "device_identifier": device_identifier,
            "status": status,
            "result": {"success": status == "success", "status": status},
            "action_args": {
                "action": action,
                "action_unique_id": action_unique_id,
            },
        }
        body = json.dumps({"params": params}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        db_name = str(self.config_store.get_connection().get("db_name") or "").strip()
        if db_name:
            headers["X-Odoo-Database"] = db_name
        req = Request(endpoint, data=body, headers=headers, method="POST")
        ssl_context = None if self.verify_ssl else ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=8, context=ssl_context) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
            response_payload: Any = None
            if resp_body.strip():
                try:
                    response_payload = json.loads(resp_body)
                except json.JSONDecodeError:
                    response_payload = {"raw": resp_body[:500]}
            if isinstance(response_payload, dict) and response_payload.get("error"):
                raise RuntimeError(f"Odoo rejected operation confirmation: {response_payload['error']}")
            self._record_confirmation(
                session_id=session_id,
                device_identifier=device_identifier,
                status=status,
                action=action,
                action_unique_id=action_unique_id,
                http_status=resp.status,
                accepted=True,
                response=response_payload,
            )
            _logger.debug(
                "Cloud bridge confirmation sent server_url=%s session_id=%s device=%s status=%s "
                "http_status=%s response_size=%s verify_ssl=%s",
                server_url,
                session_id,
                device_identifier,
                status,
                resp.status,
                len(resp_body),
                self.verify_ssl,
            )
        except Exception as exc:
            self._record_confirmation(
                session_id=session_id,
                device_identifier=device_identifier,
                status=status,
                action=action,
                action_unique_id=action_unique_id,
                accepted=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            _logger.error(
                "Cloud bridge confirmation FAILED server_url=%s session_id=%s device=%s status=%s "
                "error_type=%s error=%s verify_ssl=%s",
                server_url,
                session_id,
                device_identifier,
                status,
                type(exc).__name__,
                str(exc),
                self.verify_ssl,
            )
            raise

    def _record_confirmation(self, **entry: Any) -> None:
        """Persist confirmation diagnostics independently from console logs."""
        payload = {"timestamp": round(time.time(), 3), **entry}
        try:
            self._confirmation_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._confirmation_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            _logger.exception("Could not write operation confirmation diagnostic log")

    @staticmethod
    def _extract_session_id(set_cookies: list[str]) -> str:
        cookie = SimpleCookie()
        for set_cookie in set_cookies:
            cookie.load(set_cookie)
        morsel = cookie.get("session_id")
        return morsel.value if morsel else ""

    def _fetch_session_id(self, server_url: str, db_name: str) -> str:
        login_url = f"{server_url}/web/login"
        if db_name:
            login_url += f"?db={quote_plus(db_name)}"
        req = Request(login_url, method="GET")
        opener = _NO_REDIRECT_OPENER
        if not self.verify_ssl:
            opener = build_opener(
                _NoRedirectHandler(),
                HTTPSHandler(context=ssl._create_unverified_context()),
            )
        try:
            # 使用禁止重定向的 opener：Odoo /web/login 返回 302 时，
            # 默认 urlopen 会跟随 → 可能无限循环。
            # 我们直接从 302 响应的 Set-Cookie 中提取 session_id。
            # OpenerDirector.open() 不支持 context 参数（urlopen 独有），
            # so the unverified context must be installed on its HTTPS handler.
            with opener.open(req, timeout=10) as resp:
                set_cookies = resp.headers.get_all("Set-Cookie", [])
                status = resp.status
        except HTTPError as exc:
            # 禁止重定向后 302 以 HTTPError 抛出；其 headers 仍携带
            # Set-Cookie: session_id=...，据此提取会话。
            headers = exc.headers
            set_cookies = headers.get_all("Set-Cookie", []) if headers else []
            status = exc.code
        except Exception as exc:
            _logger.error(
                "Failed to fetch session_id from %s error_type=%s error=%s verify_ssl=%s",
                server_url,
                type(exc).__name__,
                str(exc),
                self.verify_ssl,
            )
            raise

        _logger.debug(
            "Fetched session_id from %s http_status=%s cookies_count=%s verify_ssl=%s",
            server_url,
            status,
            len(set_cookies),
            self.verify_ssl,
        )
        session_id = self._extract_session_id(set_cookies)
        _logger.debug("Session ID fetched server_url=%s has_session_id=%s", server_url, "yes" if session_id else "no")
        return session_id

    def _fetch_pairing_token_url(self, server_url: str) -> str:
        endpoint = f"{server_url.rstrip('/')}/iot/pairing_token"
        req = Request(endpoint, method="GET")
        ssl_context = None if self.verify_ssl else ssl._create_unverified_context()
        with urlopen(req, timeout=10, context=ssl_context) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return str(payload.get("token_url") or "").strip()

    def _re_register_after_server_clear(self, server_url: str) -> None:
        _logger.info("Received server_clear for %s; requesting a fresh pairing token", self.iot_identifier)
        token_url = self._fetch_pairing_token_url(server_url)
        if not token_url:
            self.config_store.update_connection(iot_channel="", last_websocket_message_id=0)
            self.config_store.set_sync_status(False, "Missing pairing token after server_clear")
            raise RuntimeError("Missing pairing token after server_clear")

        connection = self.config_store.connect_from_token_url(token_url)
        sync = self.odoo_sync.sync_setup(
            server_url=connection.get("url", ""),
            token=connection.get("token", ""),
            db_name=connection.get("db_name", ""),
            identifier=self.iot_identifier,
            ip=self.iot_ip,
            version=self.iot_version,
            devices=self.device_manager.as_odoo_devices_payload(),
        )
        self.config_store.set_sync_status(sync.ok, sync.message)
        if not sync.ok:
            self.config_store.update_connection(iot_channel="", last_websocket_message_id=0)
            raise RuntimeError(sync.message)

        self.config_store.update_connection(
            connected=True,
            iot_channel=sync.iot_channel,
            last_websocket_message_id=0,
        )
        _logger.info("IoT runtime %s re-registered after server_clear on channel %s", self.iot_identifier, sync.iot_channel)

    def _to_websocket_url(self, server_url: str) -> str:
        parsed = urlsplit(server_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((ws_scheme, parsed.netloc, "/websocket", "", ""))

    def _parse_messages(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if not isinstance(raw, str):
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "connected": self.connected,
            "server_url": self.last_server_url,
            "iot_channel": self.last_channel,
            "last_error": self.last_error,
            "ssl_verify": self.verify_ssl,
        }
