"""Command-line diagnostics for printer and Odoo connection latency."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin


def _measure(label: str, operation) -> float | None:
    started = time.perf_counter()
    try:
        operation()
    except Exception as exc:
        print(f"  {label}: 失败 ({exc})")
        return None
    elapsed = (time.perf_counter() - started) * 1000
    print(f"  {label}: {elapsed:.0f} ms")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--odoo-url", help="Odoo base URL; omit to skip the HTTP request test")
    args = parser.parse_args()

    print(f"Python: {sys.version}")
    if args.odoo_url:
        host = args.odoo_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        ping_count = "-n" if platform.system() == "Windows" else "-c"
        _measure(
            f"Ping {host}",
            lambda: subprocess.run(
                ["ping", ping_count, "1", host], check=True, capture_output=True, timeout=5,
            ),
        )

        body = json.dumps({
            "params": {
                "session_id": "diagnostic",
                "iot_box_identifier": "diagnostic",
                "device_identifier": "diagnostic",
                "status": "success",
                "result": {},
                "action_args": {},
            }
        }).encode("utf-8")
        request = urllib.request.Request(
            urljoin(args.odoo_url.rstrip("/") + "/", "iot/box/send_websocket"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _measure("HTTPS 请求", lambda: urllib.request.urlopen(request, timeout=10).read())

    try:
        import win32print
        _measure(
            "打印机枚举",
            lambda: win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            ),
        )
    except ImportError:
        print("  打印机枚举: 当前系统没有 win32print")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
