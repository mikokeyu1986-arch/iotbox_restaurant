from __future__ import annotations

import logging
import os
import sys
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
    try:
        certs.install_for_current_windows_user()
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not install IoT HTTPS certificate in the Windows trust store: %s", exc)
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
