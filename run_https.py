from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

# HTTPS has its own runtime configuration and must not inherit the HTTP 8399
# configuration when both services are available.
BASE_DIR = Path(__file__).resolve().parent
INSTALL_DIR = Path(sys.executable).resolve().parent.parent if getattr(sys, "frozen", False) else BASE_DIR
os.environ.setdefault("IOT_CONFIG_PATH", str(INSTALL_DIR / "runtime_config.json"))
os.environ.setdefault("IOT_CERTS_DIR", str(INSTALL_DIR / "certs"))
os.environ.setdefault("IOT_SSL_VERIFY", "0")
os.environ.setdefault("IOT_PORT", "8398")

LOG_DIR = INSTALL_DIR / "logs"


def _configure_logging() -> None:
    """Install the root logging handler before the app is imported.

    uvicorn is started with ``log_config=None``, so nothing configures logging
    and every INFO line -- the ``dev_log`` diagnostics included -- is dropped,
    which leaves a runtime that prints nothing impossible to diagnose from the
    machine.  Write a rotating file next to the runtime as well as the console.
    """
    if logging.getLogger().handlers:
        return
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / "https-runtime.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError:
        pass
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    # IOT_LOG_LEVEL=DEBUG surfaces the diagnostics that are otherwise dropped,
    # such as a cloud message addressed to a different IoT identifier.
    level_name = (os.getenv("IOT_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, handlers=handlers)


_configure_logging()

from app.certificate_manager import ensure_runtime_tls_assets  # noqa: E402
from app.main import IOT_IP, app, certificate_manager, config_store  # noqa: E402


def _resolve_host_port() -> tuple[str, int]:
    host = os.getenv("IOT_HOST", "0.0.0.0")
    port = int(os.getenv("IOT_PORT", "8398"))
    local_url = str(config_store.get_local_config().get("local_url") or "")
    if local_url.startswith("https://") and not os.getenv("IOT_PORT_OVERRIDE"):
        parsed = urlparse(local_url)
        # The published address may be a DHCP lease that has since changed.
        # Keep listening on every interface instead of binding to that stale
        # address; only retain its configured port.
        port = parsed.port or port
    if host in {"127.0.0.1", "localhost"}:
        host = "0.0.0.0"
    return host, port


def _advertised_https_url(port: int) -> str:
    """Return the LAN URL that Odoo and POS clients can actually reach.

    ``IOT_IP`` is detected while importing ``app.main`` and deliberately uses
    the current DHCP address.  Do not publish 127.0.0.1 here: that address is
    only valid from the IOTBOX machine itself.
    """
    host = IOT_IP.rsplit(":", 1)[0].strip()
    if not host or host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"https://{host}:{port}"


def _trust_the_certificate_in_the_background(certs) -> None:
    """Install trust automatically on Windows only.

    macOS trust changes can trigger an authorization dialog and must remain an
    explicit operator action. Certificate generation and HTTPS serving are
    intentionally independent from trust-store installation.
    """
    if sys.platform == "darwin":
        return
    try:
        certs.install_for_current_user()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Could not trust the IoT HTTPS certificate for this user: %s", exc
        )


def main() -> None:
    host, port = _resolve_host_port()
    advertised_url = _advertised_https_url(port)
    config_store.update_local_config(
        ssl_engine="secure_https",
        local_url=advertised_url,
        service_protocol="https",
    )
    certs = ensure_runtime_tls_assets(
        certificate_manager.certs_dir,
        # Include the detected LAN address in Subject Alternative Names so
        # POS clients can reach this box without a hostname-mismatch warning.
        iot_ip=IOT_IP,
        p12_password=os.getenv("IOT_P12_PASSWORD", ""),
    )
    # Windows can install into the current user's trust store unattended.
    # macOS installation is deliberately manual: never show an authorization
    # prompt merely because the IoT Box service started.
    if sys.platform != "darwin":
        threading.Thread(
            target=_trust_the_certificate_in_the_background,
            args=(certs,),
            name="iot-certificate-trust",
            daemon=True,
        ).start()
    logging.getLogger(__name__).info(
        "Starting IoT HTTPS runtime host=%s port=%s advertised_url=%s",
        host, port, advertised_url,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_keyfile=os.fspath(certs.key_path),
        ssl_certfile=os.fspath(certs.crt_path),
        log_level="info",
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
