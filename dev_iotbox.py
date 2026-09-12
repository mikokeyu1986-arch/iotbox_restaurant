"""Local IOTBOX receipt development helper.

Usage:
    py dev_iotbox.py --watch
    py dev_iotbox.py --preview

The watcher only restarts the local HTTP runtime. It never starts the GUI or
invokes the online updater.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WATCH_FILES = (
    ROOT / "app" / "receipt_builder.py",
    ROOT / "app" / "device_manager.py",
    ROOT / "run_https.py",
)
RUNTIME = ROOT / "run_https.py"
PREVIEW = ROOT / "spool" / "last_escpos_request.json"


def _start_runtime() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(RUNTIME)],
        cwd=ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _preview() -> None:
    payload = json.loads(PREVIEW.read_text(encoding="utf-8"))
    structured = payload.get("structured", {})
    lines = []
    for key in ("header_lines", "items", "summary_lines", "total_line", "payment_lines", "change_line", "footer_lines"):
        value = structured.get(key)
        if isinstance(value, list):
            lines.extend(value)
        elif isinstance(value, str) and value:
            lines.append(value)
    print("\n".join(_line_text(line) for line in lines if _line_text(line)))


def _line_text(line: object) -> str:
    if isinstance(line, str):
        return line
    if not isinstance(line, dict):
        return ""
    if line.get("type") == "product_line":
        return f"{line.get('qty', '')} {line.get('name', '')} {line.get('total', '')}".strip()
    return str(line.get("text") or line.get("left_text") or line.get("name") or "").strip()


def _watch() -> None:
    process = _start_runtime()
    mtimes = {path: path.stat().st_mtime_ns for path in WATCH_FILES if path.exists()}
    print("IOTBOX 开发监听已启动；保存小票代码后自动重启后台服务。按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1)
            changed = any(path.exists() and path.stat().st_mtime_ns != mtimes.get(path) for path in WATCH_FILES)
            if not changed:
                continue
            mtimes = {path: path.stat().st_mtime_ns for path in WATCH_FILES if path.exists()}
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process = _start_runtime()
            print("已检测到代码变化，IOTBOX 后台服务已重新加载。")
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.preview:
        _preview()
    elif args.watch:
        _watch()
    else:
        parser.error("请使用 --watch 或 --preview")
