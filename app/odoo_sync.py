from __future__ import annotations

import json
import os
import ssl
import time
from urllib.parse import quote
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(slots=True)
class SyncResult:
    ok: bool
    message: str
    iot_channel: str = ""


class OdooSyncService:
    def __init__(self, verify_ssl: bool = True) -> None:
        self.verify_ssl = verify_ssl

    @staticmethod
    def _extract_channel(parsed: dict[str, Any]) -> str:
        result = parsed.get("result")
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            for key in ("iot_channel", "channel", "name"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        params = parsed.get("params")
        if isinstance(params, dict):
            for key in ("iot_channel", "channel", "name"):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def sync_setup(
        self,
        *,
        server_url: str,
        token: str,
        db_name: str = "",
        identifier: str,
        ip: str,
        version: str,
        devices: dict[str, dict[str, Any]],
    ) -> SyncResult:
        if not server_url or not token:
            return SyncResult(False, "Missing server_url or token")

        endpoint = f"{server_url.rstrip('/')}/iot/setup"
        if db_name and "?" not in endpoint:
            endpoint += f"?db={quote(str(db_name).strip())}"
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "iot_box": {
                    "identifier": identifier,
                    "ip": ip,
                    "version": version,
                    "token": token,
                    "mac": identifier,
                    "admin_token": os.getenv("IOT_ADMIN_TOKEN", "").strip(),
                },
                "devices": devices,
            },
            "id": int(time.time()),
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if db_name:
            headers["X-Odoo-Database"] = str(db_name).strip()
        req = Request(endpoint, data=body, headers=headers, method="POST")
        ssl_context = None if self.verify_ssl else ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=8, context=ssl_context) as resp:
                raw = resp.read().decode("utf-8")
        except URLError as exc:
            return SyncResult(False, f"Failed calling /iot/setup: {exc}")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return SyncResult(False, f"Invalid JSON response from /iot/setup: {raw[:180]}")

        if parsed.get("error"):
            return SyncResult(False, f"/iot/setup error: {parsed['error']}")
        channel = self._extract_channel(parsed)
        if channel:
            return SyncResult(True, "Synced with Odoo /iot/setup", iot_channel=channel)
        return SyncResult(True, "Synced with Odoo /iot/setup (no channel update returned)")
