from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .certificate_manager import CertificateManager
from .cloud_bridge import OdooCloudBridge
from .config_store import ConfigStore
from .dev_logger import dev_log, summarize_action
from .device_manager import DeviceManager
from .event_bus import EventBus
from .models import IoTEvent
from .odoo_sync import OdooSyncService
from .printing.printer_language import normalize_language
from .receipt_builder import build_receipt_lines
from .receipt_builder import build_kitchen_ticket_lines
from .kitchen_template_store import (
    LINE_CLASSES,
    load_kitchen_template,
    reset_kitchen_template,
    save_kitchen_template,
    validate_kitchen_template,
)
from .receipt_template_store import load_template, reset_template, save_template, validate_template
from .printer_profile import (
    DEFAULT_PROFILE,
    active_columns,
    calibration_lines,
    line_diagnostics,
    validate_printer_profile,
)
from .version import APP_VERSION
from .vfd_writer import write_serial as write_vfd_serial


BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(os.getenv("IOT_RESOURCE_DIR", str(BASE_DIR)))
STATIC_DIR = RESOURCE_DIR / "web"
SPOOL_DIR = Path(os.getenv("IOT_SPOOL_DIR", str(BASE_DIR / "spool")))
CONFIG_PATH = Path(os.getenv("IOT_CONFIG_PATH", str(BASE_DIR / "runtime_config.json")))
CERTS_DIR = Path(os.getenv("IOT_CERTS_DIR", str(BASE_DIR / "certs")))
LOGO_DIR = RESOURCE_DIR / "assets"


def _detect_local_ip() -> str:
    """Auto-detect the LAN IP address of this machine.

    Falls back to ``127.0.0.1:8398`` when no suitable LAN interface is found.
    """
    env_ip = (os.getenv("IOT_IP") or "").strip()
    if env_ip:
        host = env_ip.split(":", 1)[0].strip()
        if host not in ("", "127.0.0.1", "localhost", "0.0.0.0"):
            return env_ip
    # Try to find a non-loopback IPv4 address via UDP trick (does not send
    # any data — just asks the kernel which interface would be used).
    port = os.getenv("IOT_PORT", "8398")
    for target in ("192.168.1.1", "10.0.0.1", "172.16.0.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.1)
                s.connect((target, 1))
                ip = s.getsockname()[0]
                if ip and ip != "127.0.0.1":
                    return f"{ip}:{port}"
        except (OSError, socket.error):
            continue
    return f"127.0.0.1:{port}"


IOT_IP = _detect_local_ip()
IOT_VERSION = os.getenv("IOT_VERSION", APP_VERSION)
SPOOL_CLEAN_INTERVAL_SECONDS = int(os.getenv("IOT_SPOOL_CLEAN_INTERVAL_SECONDS", "1800"))
SPOOL_RETENTION_SECONDS = int(os.getenv("IOT_SPOOL_RETENTION_SECONDS", "1800"))
IOT_ENABLE_CLOUD_BRIDGE = os.getenv("IOT_ENABLE_CLOUD_BRIDGE", "1").strip().lower() in {"1", "true", "yes", "on"}
IOT_SSL_VERIFY = os.getenv("IOT_SSL_VERIFY", "1").strip().lower() in {"1", "true", "yes", "on"}
IOT_P12_PASSWORD = os.getenv("IOT_P12_PASSWORD", "")

_logger = logging.getLogger(__name__)
_PRINT_ACTIONS = {"print_receipt", "print_receipt_escpos"}
_iot_action_tasks: set[asyncio.Task[None]] = set()
# The customer-facing window is a local webview.  Keep the latest POS payload
# in the IoT Box so the screen can reconnect without losing its current order.
_customer_display_state: dict[str, Any] = {"revision": 0, "payload": {}}


def _set_customer_display_state(action_data: dict[str, Any]) -> None:
    """Store the native POS ``iot_action`` dispatched to a display device."""
    action = str(action_data.get("action") or "set")
    if action == "set":
        payload = action_data.get("data", {})
    elif action == "close":
        payload = {"text": "Thank you"}
    else:
        payload = action_data.get("data", action_data)
    _customer_display_state["revision"] = int(_customer_display_state["revision"]) + 1
    _customer_display_state["payload"] = payload if isinstance(payload, dict) else {"text": str(payload)}


def _clear_customer_display_state() -> None:
    """Drop the last order so it cannot outlive the pairing that sent it.

    The display is a local webview that reconnects to this state, and an empty
    payload makes it fall back to its welcome screen.  The revision is bumped
    rather than zeroed so every connected screen notices the change.
    """
    _customer_display_state["revision"] = int(_customer_display_state["revision"]) + 1
    _customer_display_state["payload"] = {}


# The Movi app has been observed POSTing a byte-identical kitchen print twice
# roughly 100 ms apart, and only *after* the first request had already been
# acknowledged, so both tickets were printed.  Remember what was just accepted
# and drop an identical repeat.  Set the window to 0 to switch the guard off.
_KITCHEN_PRINT_DEDUPE_SECONDS = max(0.0, float(os.getenv("IOT_KITCHEN_PRINT_DEDUPE_SECONDS", "3")))
_recent_kitchen_prints: dict[str, float] = {}


def _is_duplicate_kitchen_print(body: dict[str, Any]) -> bool:
    """Record this kitchen print and report whether it repeats a recent one.

    A receipt explicitly flagged as a reprint is never suppressed: that flag is
    how the caller asks for a deliberate second copy.
    """
    if _KITCHEN_PRINT_DEDUPE_SECONDS <= 0:
        return False
    receipts = body.get("receipts") or []
    if any(isinstance(receipt, dict) and receipt.get("reprint") for receipt in receipts):
        return False
    try:
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    now = time.monotonic()
    for seen_digest, seen_at in list(_recent_kitchen_prints.items()):
        if now - seen_at > _KITCHEN_PRINT_DEDUPE_SECONDS:
            del _recent_kitchen_prints[seen_digest]
    if digest in _recent_kitchen_prints:
        return True
    _recent_kitchen_prints[digest] = now
    return False


def _suppress_windows_connection_reset(loop: asyncio.AbstractEventLoop) -> None:
    default_handler = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        handle = str(context.get("handle") or "")
        if (
            os.name == "nt"
            and isinstance(exception, ConnectionResetError)
            and getattr(exception, "winerror", None) == 10054
            and "_ProactorBasePipeTransport._call_connection_lost" in handle
        ):
            return
        if default_handler:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def _perf_log(message: str) -> None:
    if os.getenv("IOT_VERBOSE_PERF_LOGS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    print(message, flush=True)


def _sync_receipt_logo() -> dict[str, Any]:
    """Fetch the current company logo from the paired Odoo server and cache it
    to ``assets/logo.png`` (used by the receipt builder to render the logo).

    The logo endpoint is db-scoped (Odoo 19), so the request is sent with an
    ``X-Odoo-Database`` header.  A small list of candidate database names is
    tried (the live Movi database first, then the configured one) so the logo
    is resolved regardless of which database hosts movi_pos_api.
    """
    server_url = str(config_store.get_connection().get("url") or "").strip().rstrip("/")
    if not server_url:
        return {"ok": False, "error": "no_server_url"}
    configured_db = str(config_store.get_connection().get("db_name") or "").strip()
    candidate_dbs = list(dict.fromkeys(filter(None, ["odoo_spain", configured_db, "odoo_restaurant"])))
    logo_url = f"{server_url}/movi-api/v1/logo"
    last_error = ""
    for db_name in candidate_dbs:
        try:
            headers = {"X-Odoo-Database": db_name} if db_name else {}
            req = urllib.request.Request(logo_url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    last_error = f"http_{resp.status}"
                    continue
                data = resp.read()
                if not data:
                    last_error = "empty"
                    continue
                LOGO_DIR.mkdir(parents=True, exist_ok=True)
                target = LOGO_DIR / "logo.png"
                target.write_bytes(data)
                return {"ok": True, "bytes": len(data), "path": str(target), "db": db_name}
        except Exception as exc:
            last_error = str(exc)
            continue
    return {"ok": False, "error": last_error or "no_matching_db"}


async def _sync_receipt_logo_async() -> dict[str, Any]:
    return await asyncio.to_thread(_sync_receipt_logo)


async def _background_logo_sync() -> None:
    try:
        await asyncio.sleep(1)
        result = await _sync_receipt_logo_async()
        _logger.info("Receipt logo sync at startup: %s", result)
    except Exception:
        _logger.exception("Receipt logo sync failed")

app = FastAPI(title="Restaurant Native Print IoT Box Runtime", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _is_local_machine_request(request: Request) -> bool:
    """Accept the machine's advertised LAN address as local administration.

    Browsers opened on the IoT Box commonly use its certificate/LAN address
    instead of 127.0.0.1.  In that case the kernel reports the client as the
    same LAN IP; treating it as remote made the dashboard load `/api/status`
    but reject every editor request with 403.
    """
    if _is_loopback_request(request):
        return True
    client_host = request.client.host if request.client else ""
    advertised_host = urlsplit(f"//{IOT_IP}").hostname or ""
    try:
        return ipaddress.ip_address(client_host) == ipaddress.ip_address(advertised_host)
    except ValueError:
        return bool(client_host) and client_host.lower() == advertised_host.lower()


def _is_same_origin(request: Request) -> bool:
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return True
    return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()


def _is_paired_odoo_origin(request: Request) -> bool:
    origin = request.headers.get("origin", "").strip()
    paired_url = str(config_store.get_connection().get("url") or "").strip()
    if not origin or not paired_url:
        return False
    supplied = urlsplit(origin)
    expected = urlsplit(paired_url)
    return (supplied.scheme.lower(), supplied.netloc.lower()) == (
        expected.scheme.lower(), expected.netloc.lower(),
    )


def _is_private_network_preflight(request: Request) -> bool:
    return (
        request.method == "OPTIONS"
        and request.headers.get("access-control-request-private-network", "").lower() == "true"
    )


@app.middleware("http")
async def protect_admin_api(request: Request, call_next):
    """Keep the administration API local unless a shared token is supplied."""
    configured_token = os.getenv("IOT_ADMIN_TOKEN", "").strip()
    supplied_token = request.headers.get("x-iot-admin-token", "").strip()
    token_ok = bool(configured_token) and hmac.compare_digest(configured_token, supplied_token)
    local_ok = _is_local_machine_request(request) and _is_same_origin(request)
    if _is_private_network_preflight(request) and not (
        _is_paired_odoo_origin(request) or local_ok or token_ok
    ):
        return JSONResponse(
            status_code=403,
            content={"status": "error", "detail": "Untrusted private network origin"},
        )
    if request.url.path.startswith("/api/"):
        # The public certificate contains no private key and must be
        # downloadable by LAN clients so they can trust direct HTTPS calls to
        # this IoT Box. The PKCS#12 endpoint remains protected below.
        public_certificate_download = (
            request.method == "GET" and request.url.path == "/api/cert/crt"
        )
        # The dashboard needs this read-only, credential-free payload when it
        # is opened from another device on the trusted LAN.  All state-changing
        # administration endpoints remain local/token protected.
        public_runtime_status = (
            request.method == "GET" and request.url.path == "/api/status"
        )
        # POS runs on the paired Odoo origin and must be able to push VFD
        # customer-display data to the local IOTBOX. Keep all administration
        # APIs local, but allow this one device route from the already-bound
        # Odoo origin.
        paired_vfd = request.url.path == "/api/vfd/display" and _is_paired_odoo_origin(request)
        if not (
            local_ok
            or token_ok
            or paired_vfd
            or public_certificate_download
            or public_runtime_status
        ):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "detail": "Administration API is local-only"},
            )
    protected_device_paths = {
        "/hw_proxy/print_xml_receipt",
        "/hw_proxy/print_receipt",
        "/hw_proxy/print_receipt_escpos",
        "/hw_proxy/open_cashbox",
        "/hw_proxy/open_cashbox_direct",
    }
    if request.url.path in protected_device_paths:
        if not (local_ok or token_ok or _is_paired_odoo_origin(request)):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "detail": "Untrusted print origin"},
            )
    return await call_next(request)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/customer-display")
async def customer_display_page() -> FileResponse:
    """The local page shown by the automatically detected second monitor."""
    return FileResponse(STATIC_DIR / "customer_display.html")


@app.get("/customer-display/logo")
async def customer_display_logo() -> FileResponse:
    """Serve the locally cached company logo to the customer display."""
    logo_path = LOGO_DIR / "logo.png"
    if not logo_path.is_file():
        raise HTTPException(status_code=404, detail="Company logo is not available")
    return FileResponse(logo_path, media_type="image/png")


@app.get("/customer-display/state")
async def customer_display_state(request: Request) -> dict[str, Any]:
    # Order details must never be exposed to another device on the LAN.
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Customer display is local-only")
    return dict(_customer_display_state)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    """Lightweight readiness probe used by the desktop launcher."""
    return {
        "status": "ready",
        "protocol": request.url.scheme,
        "version": APP_VERSION,
        "iot_ip": IOT_IP,
    }

event_bus = EventBus()
config_store = ConfigStore(CONFIG_PATH)
IOT_IDENTIFIER = config_store.ensure_iot_identifier(os.getenv("IOT_IDENTIFIER", ""))
device_manager = DeviceManager(
    event_bus,
    spool_dir=SPOOL_DIR,
    local_config_getter=config_store.get_local_config,
    local_config_updater=config_store.update_local_config,
    customer_display_handler=_set_customer_display_state,
    iot_identifier=IOT_IDENTIFIER,
)
odoo_sync = OdooSyncService(verify_ssl=IOT_SSL_VERIFY)
cloud_bridge = OdooCloudBridge(
    config_store,
    device_manager,
    IOT_IDENTIFIER,
    IOT_IP,
    IOT_VERSION,
    verify_ssl=IOT_SSL_VERIFY,
)
certificate_manager = CertificateManager(CERTS_DIR, iot_ip=IOT_IP, p12_password=IOT_P12_PASSWORD)

try:
    from .drivers.scale import ScaleService
    scale_service = ScaleService(config_store.get_local_config, event_bus=event_bus)
except ImportError:
    scale_service = None


async def _spool_cleanup_loop() -> None:
    device_manager.cleanup_spool(SPOOL_RETENTION_SECONDS)
    while True:
        await asyncio.sleep(max(1, SPOOL_CLEAN_INTERVAL_SECONDS))
        device_manager.cleanup_spool(SPOOL_RETENTION_SECONDS)


async def _local_ip_watchdog() -> None:
    """Follow a DHCP address change and republish the new address.

    ``IOT_IP`` is resolved once at import, so without this the box keeps
    advertising the address it happened to start with: Odoo stores the stale
    one, POS clients dial it, and only a restart puts it right.
    ``_sync_registered_iot_box`` already knows how to push the current address
    to Odoo -- it was simply only ever called at startup.
    """
    global IOT_IP
    if (os.getenv("IOT_IP") or "").strip():
        # An explicit IOT_IP is pinned on purpose; do not fight it.
        return
    interval = max(15, int(os.getenv("IOT_LOCAL_IP_CHECK_SECONDS", "60")))
    while True:
        await asyncio.sleep(interval)
        try:
            detected = _detect_local_ip()
        except Exception:
            _logger.exception("Local address check failed")
            continue
        if detected == IOT_IP or detected.startswith("127."):
            # Loopback means the interface is down; keeping the last known
            # address beats advertising one nothing can reach.
            continue
        await _republish_local_address(IOT_IP, detected)


async def _republish_local_address(previous: str, detected: str) -> None:
    """Move everything over to the box's new LAN address."""
    global IOT_IP
    IOT_IP = detected
    _logger.warning("Local address changed %s -> %s; republishing", previous, detected)
    dev_log("local_ip_changed", previous=previous, current=detected)
    cloud_bridge.iot_ip = detected
    certificate_manager.iot_ip = detected
    # The address is part of the certificate SAN, so re-issue the files for it.
    # A restart is what actually serves the new one; clients that pin the CA
    # (the kiosk) survive the change either way.
    try:
        await asyncio.to_thread(certificate_manager.ensure)
    except Exception:
        _logger.exception("Could not re-issue TLS assets for the new address")
    # The advertised address itself, so the control page and anything reading
    # the runtime config stop pointing at the address the box just left.  Keep
    # whichever scheme this runtime was started with.
    current_url = str(config_store.get_local_config().get("local_url") or "")
    scheme = current_url.split("://", 1)[0] if "://" in current_url else "https"
    config_store.update_local_config(local_url=f"{scheme}://{detected}")
    await _sync_registered_iot_box()
    await cloud_bridge.request_reconnect(message="Local address changed")


async def _sync_registered_iot_box() -> None:
    """Refresh Odoo's stored LAN address after a DHCP address change."""
    connection = config_store.get_connection()
    if not (connection.get("connected") and connection.get("url") and connection.get("token")):
        return
    sync = await asyncio.to_thread(
        odoo_sync.sync_setup,
        server_url=str(connection["url"]),
        token=str(connection["token"]),
        db_name=str(connection.get("db_name") or ""),
        identifier=IOT_IDENTIFIER,
        ip=IOT_IP,
        version=IOT_VERSION,
        devices=device_manager.as_odoo_devices_payload(),
    )
    config_store.set_sync_status(sync.ok, sync.message)
    if sync.ok and sync.iot_channel:
        config_store.update_connection(iot_channel=sync.iot_channel)
    if sync.ok:
        _logger.info("Refreshed IOTBOX registration at startup ip=%s", IOT_IP)
    else:
        _logger.warning("Could not refresh IOTBOX registration at startup: %s", sync.message)


@app.on_event("startup")
async def startup_tasks() -> None:
    _suppress_windows_connection_reset(asyncio.get_running_loop())
    dev_log(
        "runtime_startup",
        iot_identifier=IOT_IDENTIFIER,
        iot_ip=IOT_IP,
        config_path=str(CONFIG_PATH),
        spool_dir=str(SPOOL_DIR),
        cloud_bridge_enabled=IOT_ENABLE_CLOUD_BRIDGE,
        ssl_verify=IOT_SSL_VERIFY,
    )
    app.state.certificate_error = ""
    if str(config_store.get_local_config().get("ssl_engine") or "").strip() == "secure_https":
        try:
            # HTTPS startup already prepared this exact LAN certificate before
            # Uvicorn loaded it. This is now a cheap consistency check only.
            certificate_manager.ensure()
        except Exception as exc:
            app.state.certificate_error = str(exc)
            _logger.exception("Certificate consistency check failed")
    app.state.spool_cleanup_task = asyncio.create_task(_spool_cleanup_loop())
    app.state.local_ip_watchdog_task = asyncio.create_task(_local_ip_watchdog())
    await device_manager.startup()
    app.state.logo_sync_task = asyncio.create_task(_background_logo_sync())
    if scale_service is not None:
        try:
            # 绑定主事件循环，让 ScaleMonitor 子线程能线程安全地推送 SSE 事件
            # 注意：不在此处启动 ScaleMonitor —— 按需读取模式，等 POS 打开称重界面时才启动
            scale_service.bind_main_loop(asyncio.get_running_loop())
            scale_service.ensure_monitor()
        except Exception:
            _logger.exception("Scale service initialization failed; continuing")
    if IOT_ENABLE_CLOUD_BRIDGE:
        # Re-register before opening the websocket so Odoo has the current
        # DHCP address and the current device list for this HTTPS runtime.
        await _sync_registered_iot_box()
        app.state.cloud_bridge_task = asyncio.create_task(cloud_bridge.run_forever())
        app.state.cloud_bridge_watchdog_task = asyncio.create_task(
            _cloud_bridge_watchdog()
        )


async def _cloud_bridge_watchdog() -> None:
    """Cloud Bridge 存活监控：如果 run_forever 协程意外退出或长时间 disconnected，
    自动重建 task，防止 IoT Box 静默失联。
    """
    CHECK_INTERVAL = 15  # 每 15 秒检查一次
    MAX_DISCONNECTED_SECONDS = 120  # 连续断开超过 2 分钟则重建
    disconnected_since = 0.0

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        task: asyncio.Task | None = getattr(app.state, "cloud_bridge_task", None)

        # 情况 1：task 不存在或已结束
        if task is None or task.done():
            exc = task.exception() if task and task.done() and not task.cancelled() else None
            _logger.error(
                "Cloud bridge task died unexpectedly%s. Restarting...",
                f" exception={exc}" if exc else "",
            )
            dev_log("cloud_bridge_task_died", exception=str(exc) if exc else "unknown")
            cloud_bridge._stop_event.clear()
            app.state.cloud_bridge_task = asyncio.create_task(cloud_bridge.run_forever())
            disconnected_since = 0.0
            continue

        # 情况 2：task 在运行但连接断开时间过长
        if not cloud_bridge.connected:
            # A deliberately unpaired box has no server to reach, so "disconnected
            # for two minutes" is its normal state, not a fault -- rebuilding the
            # bridge for it just logs an error every other minute.  The task is
            # left running; pairing again reconnects it through request_reconnect.
            connection = config_store.get_connection()
            if not (connection.get("connected") and connection.get("url")):
                disconnected_since = 0.0
                continue
            if disconnected_since == 0.0:
                disconnected_since = asyncio.get_running_loop().time()
            else:
                elapsed = asyncio.get_running_loop().time() - disconnected_since
                if elapsed > MAX_DISCONNECTED_SECONDS:
                    _logger.error(
                        "Cloud bridge disconnected for %.1fs (> %ss). Force restarting...",
                        elapsed,
                        MAX_DISCONNECTED_SECONDS,
                    )
                    dev_log(
                        "cloud_bridge_disconnected_too_long",
                        elapsed_seconds=round(elapsed, 1),
                        max_seconds=MAX_DISCONNECTED_SECONDS,
                    )
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    cloud_bridge._stop_event.clear()
                    app.state.cloud_bridge_task = asyncio.create_task(cloud_bridge.run_forever())
                    disconnected_since = 0.0
                    continue
        else:
            disconnected_since = 0.0


@app.on_event("shutdown")
async def shutdown_tasks() -> None:
    dev_log("runtime_shutdown", pending_iot_tasks=len(_iot_action_tasks))
    for task in list(_iot_action_tasks):
        task.cancel()
    if _iot_action_tasks:
        await asyncio.gather(*list(_iot_action_tasks), return_exceptions=True)
    await cloud_bridge.stop()
    await device_manager.shutdown()
    if scale_service is not None:
        scale_service._stop_monitor()
    task = getattr(app.state, "spool_cleanup_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # 先取消 watchdog，避免它在我们停止 cloud_bridge 时又重建 task
    watchdog_task = getattr(app.state, "cloud_bridge_watchdog_task", None)
    if watchdog_task:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
    cloud_task = getattr(app.state, "cloud_bridge_task", None)
    if cloud_task:
        cloud_task.cancel()
        try:
            await cloud_task
        except asyncio.CancelledError:
            pass
    # 释放 Cloud Bridge 专用线程池
    OdooCloudBridge.shutdown_cloud_executor()


class JsonRpcRequest(BaseModel):
    jsonrpc: str | None = "2.0"
    method: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    id: str | int | None = None


def rpc_ok(result: Any, rpc_id: str | int | None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(message: str, rpc_id: str | int | None, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _track_iot_action_task(task: asyncio.Task[None]) -> None:
    _iot_action_tasks.add(task)
    task.add_done_callback(_iot_action_tasks.discard)
    _logger.info("IOT background task tracked pending_tasks=%s", len(_iot_action_tasks))


async def _execute_iot_action_background(session_id: str, device_identifier: str, data: dict[str, Any]) -> None:
    started_at = asyncio.get_running_loop().time()
    action = str(data.get("action") or "")
    has_receipt = "yes" if data.get("receipt") else "no"
    _logger.info(
        "IOT background action start session_id=%s device_identifier=%s action=%s has_receipt=%s pending_tasks=%s",
        session_id,
        device_identifier,
        action or "<none>",
        has_receipt,
        len(_iot_action_tasks),
    )
    _logger.debug(
        "IOT background action data session_id=%s device_identifier=%s action=%s data_keys=%s",
        session_id,
        device_identifier,
        action,
        sorted(data.keys()),
    )
    try:
        success = await device_manager.execute(session_id, device_identifier, data)
    except Exception as exc:
        success = False
        dev_log(
            "iot_background_exception",
            session_id=session_id,
            device_identifier=device_identifier,
            action=action,
        )
        _logger.exception(
            "IOT background action failed session_id=%s device_identifier=%s action=%s error_type=%s error=%s",
            session_id,
            device_identifier,
            action or "<none>",
            type(exc).__name__,
            str(exc),
        )
    if not success:
        _logger.warning(
            "IOT background action returned failure session_id=%s device_identifier=%s action=%s duration_ms=%.1f",
            session_id,
            device_identifier,
            action or "<none>",
            (asyncio.get_running_loop().time() - started_at) * 1000,
        )
        await event_bus.publish(
            IoTEvent(
                device_identifier=device_identifier,
                owner=session_id,
                status="error",
                message="ERROR_PRINTER",
                result={"mode": action or "unknown"},
            )
        )
    else:
        # The POS learns that a print finished from /iot_drivers/event: it
        # registers a listener for the device and waits for the box to report
        # the action status.  Publishing on failure only left every successful
        # kitchen print unanswered until the 50 s poll expired, which the POS
        # shows as an order stuck in "not sent".
        await event_bus.publish(
            IoTEvent(
                device_identifier=device_identifier,
                owner=session_id,
                status="success",
                result={"mode": action or "unknown"},
            )
        )
    _logger.info(
        "IOT background action done session_id=%s device_identifier=%s action=%s success=%s duration_ms=%.1f pending_tasks=%s",
        session_id,
        device_identifier,
        action or "<none>",
        success,
        (asyncio.get_running_loop().time() - started_at) * 1000,
        len(_iot_action_tasks),
    )
    dev_log(
        "iot_background_done",
        session_id=session_id,
        device_identifier=device_identifier,
        action=action,
        success=success,
        duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000, 1),
        pending_tasks=len(_iot_action_tasks),
    )



@app.get("/", response_class=FileResponse)
async def homepage() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )



@app.post("/iot_drivers/action")
async def iot_action(request: JsonRpcRequest) -> dict[str, Any]:
    params = request.params
    session_id = str(params.get("session_id", ""))
    device_identifier = str(params.get("device_identifier", ""))
    data = params.get("data") or {}
    if not isinstance(data, dict):
        return rpc_error("Invalid action data", request.id, code=-32602)
    action = str(data.get("action") or "")
    _perf_log(
        "IOT action "
        f"session_id={session_id or '<missing>'} "
        f"device_identifier={device_identifier or '<missing>'} "
        f"action={action or '<none>'} "
        f"keys={sorted(data.keys()) if isinstance(data, dict) else '<non-dict>'} "
        f"data={data if isinstance(data, dict) else '<non-dict>'}"
    )

    _logger.info(
        "IOT action request session_id=%s device_identifier=%s action=%s keys=%s",
        session_id or "<missing>",
        device_identifier or "<missing>",
        action or "<none>",
        sorted(data.keys()) if isinstance(data, dict) else "<non-dict>",
    )
    dev_log(
        "iot_action_request",
        rpc_id=request.id,
        session_id=session_id,
        device_identifier=device_identifier,
        action=action,
        action_summary=summarize_action(data),
    )

    if not session_id or not device_identifier:
        dev_log(
            "iot_action_rejected",
            reason="missing identifiers",
            rpc_id=request.id,
            session_id=session_id,
            device_identifier=device_identifier,
            action=action,
        )
        _logger.warning(
            "IOT action rejected due to missing identifiers session_id=%s device_identifier=%s",
            session_id or "<missing>",
            device_identifier or "<missing>",
        )
        return rpc_error("Missing session_id or device_identifier", request.id, code=-32602)

    if action in _PRINT_ACTIONS:
        task = asyncio.create_task(_execute_iot_action_background(session_id, device_identifier, dict(data)))
        _track_iot_action_task(task)
        _logger.info(
            "IOT action queued session_id=%s device_identifier=%s action=%s pending_tasks=%s",
            session_id,
            device_identifier,
            action or "<none>",
            len(_iot_action_tasks),
        )
        dev_log(
            "iot_action_queued",
            rpc_id=request.id,
            session_id=session_id,
            device_identifier=device_identifier,
            action=action,
            pending_tasks=len(_iot_action_tasks),
        )
        return rpc_ok(True, request.id)

    # Odoo 19 POS IoT 协议：电子秤 action 通过 /iot_drivers/action 调用
    # action ∈ {read_once, start_reading, stop_reading}，action 只返回 True，
    # 重量通过 EventBus 事件推送（/iot_drivers/event 长轮询获取）。
    # 事件 result 是直接数字（非 {"value": x} 对象），遵循 serial_scale_driver.py 协议。
    # 关键：Odoo 19 controller 把 session_id 加到 data 中，Driver.action 设置
    # self.data["owner"] = session_id，后续事件中 owner = session_id，
    # POS 的 onMessage 检查 message.owner === requestId 才会处理事件。
    if scale_service is not None and action in {"read_once", "start_reading", "stop_reading", "read"}:
        target = device_manager.devices.get(device_identifier)
        is_scale = (
            (target is not None and getattr(target, "type", "") == "scale")
            or "scale" in device_identifier.lower()
        )
        if is_scale:
            try:
                # 设置 owner = session_id，后续 ScaleMonitor 发布事件时使用此 owner
                # 对应 Odoo 19 driver.py: self.data["owner"] = session_id
                scale_service.set_owner(session_id)
                # 先启动 ScaleMonitor（打开 COM8 一次），再读缓存。
                # 之前先 read_hw_proxy_scale 再 touch_action 会导致：
                # 1. _direct_read 打开 COM8 读一次关闭
                # 2. ScaleMonitor 再打开 COM8
                # 这个开-关-开窗口在 Windows 上可能导致串口驱动不稳定
                # （DTR/RTS 抖动、驱动重初始化延迟），直接导致后续无法读取。
                scale_service.touch_action()
                if action in {"read_once", "read"}:
                    # ScaleMonitor 刚启动，仅从缓存读取（不调用 _direct_read 以避免串口冲突）
                    weight = scale_service.get_cached_weight(max_age=2.0)
                    if weight is not None:
                        scale_service.publish_weight_event(weight)
                _logger.info(
                    "IOT scale action session_id=%s device_identifier=%s action=%s owner=%s",
                    session_id, device_identifier, action, session_id,
                )
                dev_log(
                    "iot_action_scale",
                    rpc_id=request.id,
                    session_id=session_id,
                    device_identifier=device_identifier,
                    action=action,
                )
                # Odoo 19 action 只返回 True，重量数据通过事件推送
                return rpc_ok(True, request.id)
            except Exception as exc:
                _logger.exception("IOT scale action failed: %s", exc)
                return rpc_error(f"Scale action failed: {exc}", request.id)

    success = await device_manager.execute(session_id, device_identifier, data)
    _logger.info(
        "IOT action result session_id=%s device_identifier=%s action=%s success=%s",
        session_id,
        device_identifier,
        action or "<none>",
        success,
    )
    dev_log(
        "iot_action_done",
        rpc_id=request.id,
        session_id=session_id,
        device_identifier=device_identifier,
        action=action,
        success=success,
    )
    return rpc_ok(success, request.id)


@app.post("/iot_drivers/event")
async def iot_event(request: JsonRpcRequest) -> dict[str, Any]:
    listener = request.params.get("listener")
    if not isinstance(listener, dict):
        _logger.warning("IOT event poll rejected because listener parameter is missing or invalid")
        return rpc_error("Missing listener parameter", request.id, code=-32602)

    _perf_log(f"IOT event poll listener={listener}")
    event = await event_bus.poll(listener, timeout_seconds=50)
    if event:
        _perf_log(f"IOT event result event={event}")
        _logger.info(
            "IOT event delivered session_id=%s device_identifier=%s owner=%s status=%s",
            listener.get("session_id", ""),
            event.get("device_identifier", ""),
            event.get("owner", ""),
            event.get("status", ""),
        )
    else:
        _perf_log("IOT event result event=<timeout>")
        _logger.info("IOT event poll timeout session_id=%s", listener.get("session_id", ""))
    dev_log(
        "iot_event_poll_result",
        rpc_id=request.id,
        session_id=listener.get("session_id", ""),
        listener_devices=list((listener.get("devices") or {}).keys()) if isinstance(listener.get("devices"), dict) else listener.get("devices"),
        event_status=event.get("status") if isinstance(event, dict) else None,
        event_device=event.get("device_identifier") if isinstance(event, dict) else None,
        event_owner=event.get("owner") if isinstance(event, dict) else None,
    )
    return rpc_ok(event, request.id)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    server_connection = config_store.get_public_connection()
    certificates = certificate_manager.status()
    certificates["startup_error"] = getattr(app.state, "certificate_error", "")
    devices = device_manager.device_list()
    cloud_status = cloud_bridge.status()
    dev_log(
        "api_status",
        iot_identifier=IOT_IDENTIFIER,
        server_url=server_connection.get("url", ""),
        connected=server_connection.get("connected", False),
        iot_channel=server_connection.get("iot_channel", ""),
        cloud_bridge=cloud_status if IOT_ENABLE_CLOUD_BRIDGE else {"enabled": False},
        device_count=len(devices),
        device_identifiers=[device.get("identifier") for device in devices],
    )
    return {
        "status": "success",
        "mode": "cloud",
        "iot": {
            "identifier": IOT_IDENTIFIER,
            "ip": IOT_IP,
            "version": IOT_VERSION,
        },
        "server_connection": server_connection,
        "local_config": config_store.get_local_config(),
        "cloud_bridge": cloud_status
        if IOT_ENABLE_CLOUD_BRIDGE
        else {
            "enabled": False,
            "connected": False,
            "server_url": server_connection.get("url", ""),
            "iot_channel": server_connection.get("iot_channel", ""),
            "last_error": "Disabled",
            "ssl_verify": IOT_SSL_VERIFY,
        },
        "certificates": certificates,
        "devices": devices,
    }


@app.get("/api/logo")
async def api_logo() -> dict[str, Any]:
    """Report the IoT Box's locally cached receipt logo (if any)."""
    logo_path = LOGO_DIR / "logo.png"
    if not logo_path.is_file():
        return {"status": "success", "logo": None}
    stat = logo_path.stat()
    return {
        "status": "success",
        "logo": {
            "path": str(logo_path),
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
        },
    }


@app.post("/api/logo/sync")
async def api_logo_sync() -> dict[str, Any]:
    """Refresh the locally cached receipt logo from Odoo."""
    result = await _sync_receipt_logo_async()
    return {"status": "success" if result.get("ok") else "error", **result}


def _legacy_iot_status_payload() -> dict[str, Any]:
    local_config = config_store.get_local_config()
    return {
        "status": "connected",
        "iot_box": {
            "identifier": IOT_IDENTIFIER,
            "name": IOT_IDENTIFIER,
            "ip": IOT_IP,
            "version": IOT_VERSION,
            "ssl_engine": local_config.get("ssl_engine", "plain_http"),
            "local_url": local_config.get("local_url", ""),
        },
        "devices": device_manager.device_list(),
        "drivers": _build_drivers_object(),
    }


@app.get("/hw_proxy/hello")
async def hw_proxy_hello_get() -> str:
    return "ping"


@app.post("/hw_proxy/hello")
async def hw_proxy_hello_post(request: JsonRpcRequest | None = None) -> dict[str, Any]:
    return rpc_ok("ping", request.id if request else None)


@app.post("/hw_proxy/scale_read")
async def hw_proxy_scale_read(request: JsonRpcRequest | None = None) -> dict[str, Any]:
    """Odoo POS 标准电子秤读取端点。

    POS 在称重界面按 ~500ms 间隔轮询此端点。ScaleMonitor 后台线程
    持续读取并更新缓存，这里优先返回新鲜缓存（<1s），缓存失效时
    才触发一次直接串口读取，保证 POS 响应快且不阻塞。
    """
    rpc_id = request.id if request else None
    if scale_service is None:
        return rpc_error("Scale service not available", rpc_id)
    try:
        weight = scale_service.read_hw_proxy_scale()
    except Exception as exc:
        _logger.exception("hw_proxy/scale_read failed: %s", exc)
        return rpc_error(f"Scale read failed: {exc}", rpc_id)
    if weight is None:
        return rpc_ok({"weight": 0.0, "unit": "kg", "info": "no_reading"}, rpc_id)
    return rpc_ok({"weight": round(weight, 3), "unit": "kg", "info": "ok"}, rpc_id)


@app.post("/hw_proxy/scale_zero")
async def hw_proxy_scale_zero(request: JsonRpcRequest | None = None) -> dict[str, Any]:
    """Odoo POS 标准电子秤去皮/归零端点（占位实现）。

    ZFOC/Epelsa 协议的去皮命令需根据具体型号适配，此处先返回成功
    占位，避免 POS 调用时 404。
    """
    rpc_id = request.id if request else None
    # TODO: 根据 scale_brand 发送对应去皮命令
    return rpc_ok({"success": True, "info": "scale_zero_not_implemented"}, rpc_id)


@app.get("/hw_proxy/status_json")
@app.get("/iot/box")
@app.get("/iot/box/status")
async def legacy_iot_status_get() -> dict[str, Any]:
    return _legacy_iot_status_payload()


@app.post("/hw_proxy/status_json")
async def legacy_iot_status_post(request: Request) -> dict[str, Any]:
    """
    Odoo POS keepAlive() calls rpc(POST) to this endpoint and expects
    the JSON-RPC result to be a drivers object:
      { printer: {status: "connected"}, scanner: {status: "..."}, scale: {status: "..."} }
    NOT the full status payload.
    """
    drivers = _build_drivers_object()
    try:
        body = await request.json()
        rpc_id = body.get("id") if isinstance(body, dict) else None
    except Exception:
        rpc_id = None
    return rpc_ok(drivers, rpc_id)


@app.post("/iot/box")
@app.post("/iot/box/status")
async def legacy_iot_box_status_post(request: Request) -> dict[str, Any]:
    payload = _legacy_iot_status_payload()
    try:
        body = await request.json()
        rpc_id = body.get("id") if isinstance(body, dict) else None
    except Exception:
        rpc_id = None
    return rpc_ok(payload, rpc_id)


def _build_drivers_object() -> dict[str, dict[str, str]]:
    """Build the drivers object that Odoo POS keepAlive() expects."""
    devices = device_manager.device_list()
    drivers: dict[str, dict[str, str]] = {}
    for device in devices:
        dtype = str(device.get("device_type") or device.get("type") or "").strip()
        if dtype and dtype not in drivers:
            drivers[dtype] = {"status": str(device.get("status") or "disconnected")}
    for expected_type in ("printer", "scanner", "scale"):
        if expected_type not in drivers:
            drivers[expected_type] = {"status": "disconnected"}
    return drivers


async def _read_legacy_request_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        text = body.decode("utf-8", errors="replace").strip()
        payload = {"receipt": text} if text else {}
    return payload if isinstance(payload, dict) else {"payload": payload}


def _legacy_rpc_id(payload: dict[str, Any]) -> str | int | None:
    return payload.get("id")


def _legacy_params(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    if isinstance(params, dict):
        return params
    return payload


def _legacy_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    params = _legacy_params(payload)
    raw_receipt = (
        params.get("receipt")
        or params.get("xml")
        or params.get("data")
        or payload.get("receipt")
        or payload.get("xml")
        or payload.get("data")
    )
    if isinstance(raw_receipt, dict):
        return raw_receipt
    if isinstance(raw_receipt, list):
        return {"lines": raw_receipt}
    if isinstance(raw_receipt, str):
        return {"lines": _legacy_xml_or_text_lines(raw_receipt)}
    return {}


def _legacy_xml_or_text_lines(raw_receipt: str) -> list[dict[str, Any]]:
    text = str(raw_receipt or "").strip()
    if not text:
        return []
    if text.startswith("<"):
        try:
            root = ET.fromstring(text)
            lines: list[dict[str, Any]] = []
            _append_legacy_xml_lines(root, lines)
            if lines:
                return lines
        except ET.ParseError:
            pass
    return [
        {"text": line.strip(), "align": "left", "bold": False, "classes": []}
        for line in text.splitlines()
        if line.strip()
    ]


def _append_legacy_xml_lines(node: ET.Element, lines: list[dict[str, Any]], inherited: dict[str, Any] | None = None) -> None:
    inherited = dict(inherited or {})
    tag = node.tag.rsplit("}", 1)[-1].lower()
    style = dict(inherited)
    if tag in {"center", "div", "p"} and str(node.attrib.get("align") or "").lower() == "center":
        style["align"] = "center"
    if tag == "center":
        style["align"] = "center"
    if tag in {"right"}:
        style["align"] = "right"
    if tag in {"b", "strong", "bold"}:
        style["bold"] = True
    if tag in {"h1", "h2"}:
        style["bold"] = True
        style["double_width"] = True
        style["double_height"] = True

    pieces: list[str] = []
    if node.text and node.text.strip():
        pieces.append(node.text.strip())
    for child in list(node):
        _append_legacy_xml_lines(child, lines, style)
        if child.tail and child.tail.strip():
            pieces.append(child.tail.strip())
    if pieces and tag not in {"receipt", "root", "table", "tbody", "thead", "tr"}:
        lines.append(
            {
                "text": " ".join(pieces),
                "align": style.get("align", "left"),
                "bold": bool(style.get("bold")),
                "double_width": bool(style.get("double_width")),
                "double_height": bool(style.get("double_height")),
                "classes": [],
            }
        )


async def _legacy_print(request: Request, *, action: str) -> dict[str, Any]:
    payload = await _read_legacy_request_payload(request)
    params = _legacy_params(payload)
    device_identifier = str(
        params.get("device_identifier")
        or params.get("printer_identifier")
        or config_store.get_local_config().get("printer_identifier")
        or "printer_main"
    )
    session_id = str(_legacy_rpc_id(payload) or params.get("session_id") or f"legacy-{int(asyncio.get_running_loop().time() * 1000)}")
    if action == "cashbox":
        data = {"action": "cashbox"}
    else:
        data = {"action": "print_receipt_escpos", "receipt": _legacy_receipt_payload(payload)}
    dev_log(
        "legacy_hw_proxy_print_request",
        rpc_id=_legacy_rpc_id(payload),
        session_id=session_id,
        device_identifier=device_identifier,
        action=data.get("action"),
        payload_keys=sorted(payload.keys()),
        params_keys=sorted(params.keys()),
        action_summary=summarize_action(data),
    )
    success = await device_manager.execute(session_id, device_identifier, data)
    dev_log(
        "legacy_hw_proxy_print_result",
        rpc_id=_legacy_rpc_id(payload),
        session_id=session_id,
        device_identifier=device_identifier,
        action=data.get("action"),
        success=success,
    )
    return rpc_ok(bool(success), _legacy_rpc_id(payload))


@app.post("/hw_proxy/default_printer_action")
async def hw_proxy_default_printer_action(request: Request) -> dict[str, Any]:
    """
    Odoo POS HWPrinter calls this endpoint via rpc() with:
      params: { data: { action: "print_receipt"|"cashbox", receipt: ..., ... } }
    Returns JSON-RPC response with the print result.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    params = body.get("params") if isinstance(body, dict) else {}
    data = params.get("data") if isinstance(params, dict) else {}
    if not isinstance(data, dict):
        data = {}
    action = str(data.get("action") or "")
    rpc_id = body.get("id") if isinstance(body, dict) else None

    if action == "cashbox":
        data = {"action": "cashbox"}
    else:
        # Check if the receipt is a native canvas image from HWPrinter.
        # HWPrinter renders the HTML receipt to a canvas and sends the
        # data URL directly as: { action: "print_receipt", receipt: "data:image/..." }.
        # In this case we MUST pass the image through the native print path
        # (_print_receipt_native_image) rather than parsing the data URL as
        # ESC/POS text lines via _legacy_receipt_payload().
        raw_receipt = data.get("receipt") or ""
        if isinstance(raw_receipt, str) and (
            raw_receipt.strip().startswith("data:image/")
            or raw_receipt.strip().startswith("http://")
            or raw_receipt.strip().startswith("https://")
        ):
            # Pass through as native image print_receipt action
            data = {"action": "print_receipt", "receipt": raw_receipt.strip()}
        else:
            data = {"action": "print_receipt_escpos", "receipt": _legacy_receipt_payload(data)}

    session_id = str(
        data.get("session_id")
        or params.get("session_id")
        or config_store.get_local_config().get("printer_identifier")
        or f"hwproxy-{int(asyncio.get_running_loop().time() * 1000)}"
    )
    device_identifier = str(
        data.get("device_identifier")
        or data.get("printer_identifier")
        or config_store.get_local_config().get("printer_identifier")
        or "printer_main"
    )

    dev_log(
        "hw_proxy_default_printer_action",
        rpc_id=rpc_id,
        session_id=session_id,
        device_identifier=device_identifier,
        action=data.get("action"),
        data_keys=sorted(data.keys()),
    )
    success = await device_manager.execute(session_id, device_identifier, data)
    return rpc_ok(bool(success), rpc_id)


@app.post("/hw_proxy/print_xml_receipt")
@app.post("/hw_proxy/print_receipt")
@app.post("/hw_proxy/print_receipt_escpos")
async def legacy_hw_proxy_print_receipt(request: Request) -> dict[str, Any]:
    return await _legacy_print(request, action="print_receipt_escpos")


@app.post("/hw_proxy/open_cashbox")
@app.post("/hw_proxy/open_cashbox_direct")
async def legacy_hw_proxy_open_cashbox(request: Request) -> dict[str, Any]:
    return await _legacy_print(request, action="cashbox")


@app.post("/printer_iot/printer")
async def printer_iot_kitchen_print(request: Request) -> dict[str, Any]:
    """Handle kitchen order print requests from the Movi Flutter app.

    The Flutter app sends kitchen print payloads directly to the IoT Box:
    {
        "device_identifier": "printer_main",
        "receipts": [
            {"pos_reference": "Order 00003", "changes": {"title": "NEW", ...}, ...}
        ]
    }

    Each receipt is wrapped as raw kitchen order_data so that the ESC/POS
    driver renders it as a kitchen ticket via _build_kitchen_from_order_data.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid JSON body"},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "JSON body must be an object"},
        )

    device_identifier = str(body.get("device_identifier", "")).strip()
    receipts = body.get("receipts") or []

    if not device_identifier:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Missing device_identifier"},
        )
    if not isinstance(receipts, list) or not receipts:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Missing or empty receipts array"},
        )

    if _is_duplicate_kitchen_print(body):
        _logger.info(
            "Duplicate kitchen print ignored device=%s receipts=%s window_s=%s",
            device_identifier,
            len(receipts),
            _KITCHEN_PRINT_DEDUPE_SECONDS,
        )
        dev_log(
            "printer_iot_printer_duplicate",
            device_identifier=device_identifier,
            receipt_count=len(receipts),
        )
        # Report success: the ticket is already on its way to the printer, and a
        # failure response would only invite the caller to retry a third time.
        return {
            "status": "ok",
            "results": [{"index": i, "ok": True, "duplicate": True} for i in range(len(receipts))],
        }

    session_id = str(
        config_store.get_local_config().get("printer_identifier")
        or f"printer-iot-{int(asyncio.get_running_loop().time() * 1000)}"
    )

    results: list[dict[str, Any]] = []
    has_error = False
    for idx, receipt_data in enumerate(receipts):
        if not isinstance(receipt_data, dict):
            continue
        try:
            # Wrap in order_data so _process_receipt_escpos recognizes it
            # as raw kitchen order data and calls _build_kitchen_from_order_data
            print_data: dict[str, Any] = {
                "action": "print_receipt_escpos",
                "receipt": {"order_data": receipt_data},
            }
            success = await device_manager.execute(session_id, device_identifier, print_data)
            results.append({"index": idx, "ok": success})
            if not success:
                has_error = True
        except Exception as exc:
            results.append({"index": idx, "ok": False, "error": str(exc)})
            has_error = True
            _logger.exception(
                "printer_iot_printer receipt failed index=%s device=%s error=%s",
                idx,
                device_identifier,
                exc,
            )

    dev_log(
        "printer_iot_printer",
        device_identifier=device_identifier,
        receipt_count=len(receipts),
        results_count=len(results),
        has_error=has_error,
    )

    if has_error:
        return {"status": "partial_error", "results": results}
    return {"status": "ok", "results": results}


@app.get("/api/cert/crt", response_class=FileResponse)
async def api_cert_crt() -> FileResponse:
    try:
        certificate_manager.ensure()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Certificate unavailable: {exc}") from exc
    return FileResponse(certificate_manager.crt_path, filename="iotbox.crt", media_type="application/x-x509-ca-cert")


@app.get("/api/cert/p12", response_class=FileResponse)
async def api_cert_p12() -> FileResponse:
    try:
        certificate_manager.ensure()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Certificate unavailable: {exc}") from exc
    return FileResponse(certificate_manager.p12_path, filename="iotbox.p12", media_type="application/x-pkcs12")


@app.post("/iot_drivers/connect_to_server")
async def connect_to_server(request: JsonRpcRequest) -> dict[str, Any]:
    token = str(request.params.get("token", "")).strip()
    if not token:
        return rpc_error("Missing token", request.id, code=-32602)
    try:
        connection = config_store.connect_from_token_url(token)
    except ValueError as exc:
        return rpc_ok({"status": "failure", "message": str(exc)}, request.id)
    sync = odoo_sync.sync_setup(
        server_url=connection.get("url", ""),
        token=connection.get("token", ""),
        db_name=connection.get("db_name", ""),
        identifier=IOT_IDENTIFIER,
        ip=IOT_IP,
        version=IOT_VERSION,
        devices=device_manager.as_odoo_devices_payload(),
    )
    config_store.set_sync_status(sync.ok, sync.message)
    if sync.ok and sync.iot_channel:
        config_store.update_connection(iot_channel=sync.iot_channel)
    if not sync.ok:
        config_store.reset_connection(message=sync.message)
    elif IOT_ENABLE_CLOUD_BRIDGE:
        await cloud_bridge.request_reconnect(message="Odoo connection updated from /iot_drivers/connect_to_server")
    dev_log(
        "iot_connect_to_server",
        rpc_id=request.id,
        server_url=connection.get("url", ""),
        db_name=connection.get("db_name", ""),
        db_uuid=connection.get("db_uuid", ""),
        sync_ok=sync.ok,
        sync_message=sync.message,
        iot_channel=sync.iot_channel or connection.get("iot_channel", ""),
        devices=device_manager.as_odoo_devices_payload(),
    )

    return rpc_ok(
        {
            "status": "success" if sync.ok else "failure",
            "message": sync.message,
        },
        request.id,
    )


@app.post("/api/connect")
async def api_connect(payload: dict[str, Any]) -> dict[str, Any]:
    token_url = str(payload.get("token_url", "")).strip()
    if not token_url:
        raise HTTPException(status_code=400, detail="token_url is required")
    try:
        connection = config_store.connect_from_token_url(token_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    previous_channel = str(connection.get("iot_channel") or "").strip()
    # A token is single-use on the Odoo side.  When the Odoo port/server is
    # changed, never keep a channel from the previous registration.
    config_store.update_connection(iot_channel="", last_websocket_message_id=0)
    sync = None
    for attempt in range(3):
        sync = await asyncio.to_thread(
            odoo_sync.sync_setup,
            server_url=connection.get("url", ""),
            token=connection.get("token", ""),
            db_name=connection.get("db_name", ""),
            identifier=IOT_IDENTIFIER,
            ip=IOT_IP,
            version=IOT_VERSION,
            devices=device_manager.as_odoo_devices_payload(),
        )
        if not sync.ok or sync.iot_channel:
            break
        await asyncio.sleep(0.5 * (attempt + 1))
    assert sync is not None
    config_store.set_sync_status(sync.ok, sync.message)
    if sync.ok and sync.iot_channel:
        config_store.update_connection(iot_channel=sync.iot_channel)
    elif sync.ok and previous_channel:
        # Some Odoo versions update the registration but omit iot_channel in
        # an otherwise successful /iot/setup response.  For a re-pairing to
        # the same server/database, retain the known working channel instead
        # of discarding a valid binding and asking for another token.
        config_store.update_connection(iot_channel=previous_channel)
        sync.iot_channel = previous_channel
        sync.message = "Synced with Odoo /iot/setup (existing channel retained)"
    if not sync.ok or not sync.iot_channel:
        message = sync.message
        if sync.ok and not sync.iot_channel:
            message = (
                "Odoo /iot/setup returned no iot_channel. "
                "Please generate a new pairing token and verify the Odoo port."
            )
        config_store.reset_connection(message=message)
        dev_log(
            "api_connect_failed",
            server_url=connection.get("url", ""),
            db_name=connection.get("db_name", ""),
            db_uuid=connection.get("db_uuid", ""),
            sync_message=message,
        )
        raise HTTPException(status_code=502, detail=message)
    dev_log(
        "api_connect_success",
        server_url=connection.get("url", ""),
        db_name=connection.get("db_name", ""),
        db_uuid=connection.get("db_uuid", ""),
        sync_message=sync.message,
        iot_channel=sync.iot_channel or connection.get("iot_channel", ""),
        devices=device_manager.as_odoo_devices_payload(),
    )
    if IOT_ENABLE_CLOUD_BRIDGE:
        await cloud_bridge.request_reconnect(message="Odoo connection updated from /api/connect")
    return {"status": "success", "server_connection": config_store.get_public_connection()}


@app.post("/api/disconnect")
async def api_disconnect() -> dict[str, Any]:
    """Unbind: clear the pairing *and* every configuration record it set up.

    ``cloud_bridge.disconnect`` performs the local-configuration reset itself,
    so the GUI's own unbind path (which calls this endpoint) behaves the same.
    """
    connection = await cloud_bridge.disconnect(message="Disconnected from Odoo. Ready to pair a new server.")
    # The server certificate is bound to this box's address and is re-issued on
    # the next start; the signing CA is kept because clients pin it.
    removed_certificates = certificate_manager.clear_server_certificate()
    _clear_customer_display_state()
    dev_log(
        "api_disconnect",
        message=connection.get("last_sync_message", ""),
        removed_certificates=removed_certificates,
    )
    return {
        "status": "success",
        "server_connection": connection,
        "local_config": config_store.get_local_config(),
    }


@app.post("/api/settings")
async def api_settings(payload: dict[str, Any]) -> dict[str, Any]:
    ssl_engine = str(payload.get("ssl_engine", "secure_https")).strip() or "secure_https"
    local_url = str(payload.get("local_url", "")).strip()
    update_fields: dict[str, Any] = {
        "ssl_engine": ssl_engine,
        "local_url": local_url,
    }
    if "printer_identifier" in payload:
        update_fields["printer_identifier"] = str(payload.get("printer_identifier", "")).strip()
    if "primary_printer_queue" in payload:
        update_fields["primary_printer_queue"] = str(payload.get("primary_printer_queue", "")).strip()
    if "enabled_printer_queues" in payload:
        raw_enabled_printer_queues = payload.get("enabled_printer_queues") or []
        if isinstance(raw_enabled_printer_queues, str):
            enabled_printer_queues = [item.strip() for item in raw_enabled_printer_queues.splitlines() if item.strip()]
        else:
            enabled_printer_queues = [
                str(item).strip() for item in raw_enabled_printer_queues
                if str(item).strip()
            ]
        update_fields["enabled_printer_queues"] = enabled_printer_queues
    if "zpl_detection_enabled" in payload:
        update_fields["zpl_detection_enabled"] = bool(payload.get("zpl_detection_enabled"))
    if "zpl_encoding" in payload:
        update_fields["zpl_encoding"] = str(payload.get("zpl_encoding") or "").strip() or "utf-8"
    if "printer_language_overrides" in payload:
        raw_overrides = payload.get("printer_language_overrides")
        language_overrides: dict[str, str] = {}
        if isinstance(raw_overrides, dict):
            for key, value in raw_overrides.items():
                language = normalize_language(value)
                target_key = str(key or "").strip()
                if target_key and language:
                    language_overrides[target_key] = language
        update_fields["printer_language_overrides"] = language_overrides
    config = config_store.update_local_config(**update_fields)
    # Cached language detections depend on this configuration; drop them so the
    # refresh below probes again.
    device_manager.invalidate_printer_language_cache()
    device_manager.refresh_local_hardware()
    connection = config_store.get_connection()
    sync_message = ""
    if connection.get("connected") and connection.get("url") and connection.get("token"):
        sync = await asyncio.to_thread(
            odoo_sync.sync_setup,
            server_url=connection.get("url", ""),
            token=connection.get("token", ""),
            identifier=IOT_IDENTIFIER,
            ip=IOT_IP,
            version=IOT_VERSION,
            devices=device_manager.as_odoo_devices_payload(),
        )
        config_store.set_sync_status(sync.ok, sync.message)
        if sync.ok and sync.iot_channel:
            config_store.update_connection(iot_channel=sync.iot_channel)
        sync_message = sync.message
        if IOT_ENABLE_CLOUD_BRIDGE and sync.ok:
            await cloud_bridge.request_reconnect(message="Local settings synced; reconnecting cloud bridge")
    dev_log(
        "api_settings_saved",
        update_fields=update_fields,
        sync_message=sync_message,
        devices=device_manager.device_list(),
        connection=config_store.get_public_connection(),
    )
    return {"status": "success", "local_config": config, "sync_message": sync_message}


def _receipt_preview_order() -> dict[str, Any]:
    sample_path = BASE_DIR / "templates" / "escpos_receipt" / "example_order.json"
    if sample_path.exists():
        try:
            return json.loads(sample_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "name": "Order 00042",
        "pos_reference": "Order 00042",
        "tracking_number": "42",
        "date_order": "2026-08-01 20:30:00",
        "finalized": True,
        "currency": {"symbol": "€", "position": "after"},
        "company": {"name": "示例餐厅", "street": "Calle Ejemplo 1", "city": "Las Palmas"},
        "config": {"receipt_language": "zh_CN", "receipt_footer": "谢谢惠顾\n欢迎再次光临"},
        "user_id": {"name": "收银员"},
        "table_id": {"table_number": "A08"},
        "lines": [
            {"qty": 2, "full_product_name": "牛肉汉堡 (加芝士)", "price_subtotal_incl": 17},
            {"qty": 1, "full_product_name": "可乐", "price_subtotal_incl": 3},
        ],
        "amountTaxes": 1.31,
        "tax_names": ["IGIC 7%"],
        "totalDue": 20,
        "amountPaid": 20,
        "payment_lines": [{"name": "现金", "amount": 20}],
    }


@app.get("/api/receipt-template")
async def api_receipt_template() -> dict[str, Any]:
    return {"status": "success", "template": load_template()}


@app.put("/api/receipt-template")
async def api_save_receipt_template(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        template = save_template(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "success", "template": template}


@app.delete("/api/receipt-template")
async def api_reset_receipt_template() -> dict[str, Any]:
    return {"status": "success", "template": reset_template()}


@app.post("/api/receipt-template/preview")
async def api_preview_receipt_template(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        template = validate_template(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    preview_lines = None
    try:
        last_request = json.loads((SPOOL_DIR / "last_escpos_request.json").read_text(encoding="utf-8"))
        structured = last_request.get("structured") if isinstance(last_request, dict) else None
        if isinstance(structured, dict) and structured:
            preview_lines = device_manager._build_structured_receipt_lines(structured, template=template)
    except FileNotFoundError:
        # No print request exists yet after a fresh start.  Use the built-in
        # preview order below instead of reporting a false GUI error.
        _logger.info("No last Odoo print request yet; using sample receipt preview")
    except (OSError, json.JSONDecodeError):
        _logger.exception("Failed to read last Odoo request for receipt preview")
    if preview_lines is None:
        preview_lines = build_receipt_lines(_receipt_preview_order(), template=template, preview_fields=True)
    profile = validate_printer_profile(config_store.get_local_config().get("printer_profile") or DEFAULT_PROFILE)
    return {
        "status": "success",
        "template": template,
        "lines": preview_lines,
        "profile": profile,
        "diagnostics": line_diagnostics(preview_lines, profile),
    }


@app.get("/api/printer-profile")
async def api_printer_profile() -> dict[str, Any]:
    profile = validate_printer_profile(config_store.get_local_config().get("printer_profile") or DEFAULT_PROFILE)
    return {"status": "success", "profile": profile, "active_columns": active_columns(profile)}


@app.put("/api/printer-profile")
async def api_save_printer_profile(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = validate_printer_profile(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    config_store.update_local_config(printer_profile=profile)
    return {"status": "success", "profile": profile, "active_columns": active_columns(profile)}


async def _print_studio_lines(lines: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    device_identifier = str(config_store.get_local_config().get("printer_identifier") or "printer_main")
    success = await device_manager.execute(
        f"receipt-studio-{int(time.time() * 1000)}",
        device_identifier,
        {"action": "print_receipt_escpos", "receipt": {"lines": lines, "cut": profile["cut"]}},
    )
    if not success:
        raise HTTPException(status_code=503, detail="打印机不可用或任务提交失败")
    return {"status": "success", "device_identifier": device_identifier}


@app.post("/api/printer-profile/calibration/preview")
async def api_calibration_preview(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = validate_printer_profile(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    lines = calibration_lines(profile)
    return {"status": "success", "profile": profile, "lines": lines, "diagnostics": line_diagnostics(lines, profile)}


@app.post("/api/printer-profile/calibration/print")
async def api_calibration_print(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = validate_printer_profile(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return await _print_studio_lines(calibration_lines(profile), profile)


@app.post("/api/receipt-template/print-preview")
async def api_print_receipt_preview(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        template = validate_template(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    profile = validate_printer_profile(config_store.get_local_config().get("printer_profile") or DEFAULT_PROFILE)
    lines = build_receipt_lines(_receipt_preview_order(), template=template, preview_fields=True)
    problems = [item for item in line_diagnostics(lines, profile) if item["overflow"]]
    if problems:
        raise HTTPException(status_code=400, detail=f"有 {len(problems)} 行超出可打印宽度，请先修正")
    return await _print_studio_lines(lines, profile)


@app.get("/api/kitchen-template")
async def api_kitchen_template() -> dict[str, Any]:
    # ``line_classes`` lets the editor build one row per line type without
    # duplicating the list in JavaScript, so adding a line type to the builder
    # surfaces a control for it automatically.
    return {
        "status": "success",
        "template": load_kitchen_template(),
        "line_classes": [
            {"id": name, "label": label, "block": block_id}
            for name, label, block_id in LINE_CLASSES
        ],
    }


@app.put("/api/kitchen-template")
async def api_save_kitchen_template(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        template = save_kitchen_template(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "success", "template": template}


@app.delete("/api/kitchen-template")
async def api_reset_kitchen_template() -> dict[str, Any]:
    return {"status": "success", "template": reset_kitchen_template()}


@app.post("/api/kitchen-template/preview")
async def api_preview_kitchen_template(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        template = validate_kitchen_template(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "status": "success",
        "template": template,
        "lines": build_kitchen_ticket_lines(
            _receipt_preview_order(), template=template, preview_fields=True,
        ),
    }



@app.get("/api/devices")
async def api_devices() -> dict[str, Any]:
    return {"devices": device_manager.device_list()}


@app.get("/api/scale/config")
async def api_scale_config() -> dict[str, Any]:
    config = config_store.get_local_config()
    return {
        "port": str(config.get("scale_port") or ""),
        "baudrate": int(config.get("scale_baudrate") or 9600),
        "timeout": float(config.get("scale_timeout") or 1.2),
        "brand": str(config.get("scale_brand") or "zfoc"),
        "inter_command_delay": float(config.get("scale_inter_command_delay") or 0.05),
        "is_monitor_running": scale_service.is_monitor_running if scale_service else False,
    }


@app.get("/api/scale/weight")
async def api_scale_weight() -> dict[str, Any]:
    if scale_service is None:
        return {"status": "error", "message": "Scale service not available"}
    try:
        weight = scale_service.read_weight()
        if weight is None:
            return {"status": "no_weight", "message": "No weight reading available"}
        return {"status": "success", "weight_kg": weight, "unit": "kg"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/scale/weight_cached")
async def api_scale_weight_cached() -> dict[str, Any]:
    if scale_service is None:
        return {"status": "error", "message": "Scale service not available"}
    weight = scale_service.get_cached_weight()
    if weight is None:
        return {"status": "no_weight", "message": "No cached weight"}
    return {"status": "success", "weight_kg": weight, "unit": "kg"}


@app.post("/api/scale/refresh")
async def api_scale_refresh() -> dict[str, Any]:
    if scale_service is None:
        return {"status": "error", "message": "Scale service not available"}
    # 按需读取模式：不自动启动监控，只刷新配置
    # 监控会在 POS 打开称重界面（调用 read_once action）时自动启动
    return {"status": "success", "is_running": scale_service.is_monitor_running, "mode": "on_demand"}


@app.post("/api/scale/prime")
async def api_scale_prime() -> dict[str, Any]:
    if scale_service is None:
        return {"status": "error", "message": "Scale service not available"}
    scale_service.prime_monitor()
    weight = scale_service.get_cached_weight()
    return {
        "status": "success",
        "is_running": scale_service.is_monitor_running,
        "initial_weight": weight,
    }


@app.get("/api/scale/ports")
async def api_scale_ports() -> dict[str, Any]:
    try:
        from .serial_utils import list_serial_ports
        ports = list_serial_ports()
        return {"status": "success", "ports": ports}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.post("/api/scale/save_config")
async def api_scale_save_config(payload: dict[str, Any]) -> dict[str, Any]:
    update_fields: dict[str, Any] = {}
    if "scale_port" in payload:
        update_fields["scale_port"] = str(payload.get("scale_port", "")).strip()
    if "scale_baudrate" in payload:
        update_fields["scale_baudrate"] = int(payload.get("scale_baudrate", 9600))
    if "scale_timeout" in payload:
        update_fields["scale_timeout"] = float(payload.get("scale_timeout", 1.2))
    if "scale_brand" in payload:
        update_fields["scale_brand"] = str(payload.get("scale_brand", "zfoc")).strip().lower()
    if "scale_inter_command_delay" in payload:
        update_fields["scale_inter_command_delay"] = float(payload.get("scale_inter_command_delay", 0.05))
    config = config_store.update_local_config(**update_fields)
    # 按需读取模式：保存配置后不自动启动监控
    # 监控会在 POS 打开称重界面（调用 read_once action）时自动启动
    if scale_service is not None:
        scale_service.ensure_monitor()
    return {
        "status": "success",
        "local_config": config,
        "is_monitor_running": scale_service.is_monitor_running if scale_service else False,
    }


@app.post("/api/vfd/display")
async def api_vfd_display(payload: dict[str, Any]) -> dict[str, Any]:
    """Receive POS customer-display data and write it to the local VFD."""
    config = config_store.get_local_config()
    if not bool(config.get("vfd_enabled", False)):
        raise HTTPException(status_code=503, detail="VFD is disabled")
    port = str(config.get("vfd_port") or "").strip()
    if not port:
        raise HTTPException(status_code=503, detail="VFD serial port is not configured")
    try:
        lines = await asyncio.to_thread(
            write_vfd_serial,
            payload,
            port=port,
            baudrate=int(config.get("vfd_baudrate") or 9600),
            width=int(config.get("vfd_width") or 20),
            rows=int(config.get("vfd_rows") or 2),
            protocol=str(config.get("vfd_protocol") or "cd5220"),
            encoding=str(config.get("vfd_encoding") or "ascii"),
            clear_hex=str(config.get("vfd_clear_hex") or "0C"),
            line2_hex=str(config.get("vfd_line2_hex") or ""),
        )
    except Exception as exc:
        _logger.exception("VFD display write failed port=%s", port)
        raise HTTPException(status_code=502, detail=f"VFD write failed: {exc}") from exc
    return {"ok": True, "lines": lines, "action": payload.get("action", "display")}


@app.post("/api/vfd/save_config")
async def api_vfd_save_config(payload: dict[str, Any]) -> dict[str, Any]:
    update_fields: dict[str, Any] = {
        "vfd_enabled": bool(payload.get("vfd_enabled", False)),
        "vfd_port": str(payload.get("vfd_port", "")).strip(),
        "vfd_baudrate": int(payload.get("vfd_baudrate", 9600) or 9600),
        "vfd_protocol": str(payload.get("vfd_protocol", "cd5220")).strip().lower() or "cd5220",
    }
    if update_fields["vfd_protocol"] not in {"cd5220", "plain"}:
        raise HTTPException(status_code=400, detail="Unsupported VFD protocol")
    config = config_store.update_local_config(**update_fields)
    return {"status": "success", "local_config": config}
