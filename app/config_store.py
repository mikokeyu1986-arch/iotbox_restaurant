from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import threading
from time import monotonic
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse
import uuid


_logger = logging.getLogger(__name__)

# These identifiers were accidentally embedded in historical installers.
# Treating them as uninitialized prevents multiple computers from registering
# as the same IOTBOX in Odoo after upgrading.
_LEGACY_PACKAGED_IOT_IDENTIFIERS = {
    "custom-iot-box-001",
    "custom-iot-box-a9f549a11170",
    "custom-iot-box-81e830c8e082",
}

# Unbinding hands the box to a new deployment, so the previous customer's
# machine configuration has to go -- but the box's own identity must survive.
# A re-pairing to the same Odoo has to stay the same IOTBOX; regenerating the
# identifier would register a duplicate device there instead.
_UNBIND_PROTECTED_KEYS: tuple[str, ...] = ("iot_identifier",)


class ConfigStore:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._lock = threading.RLock()
        self._last_message_id_save_at = 0.0
        self._data = self._load()

    @staticmethod
    def _default_local_config() -> dict[str, Any]:
        return {
            "ssl_engine": "plain_http",
            "local_url": "",
            "iot_identifier": "",
            "printer_identifier": "",
            "primary_printer_queue": "",
            "enabled_printer_queues": [],
            "printer_aliases": {},
            "epson_discovery_enabled": True,
            "epson_printer_hosts": [],
            "epson_discovery_subnets": [],
            "epson_discovery_port": 9100,
            "epson_discovery_timeout": 0.12,
            "epson_discovery_workers": 64,
            "zpl_detection_enabled": True,
            "zpl_probe_timeout": 0.6,
            "zpl_encoding": "utf-8",
            "printer_language_overrides": {},
            "scale_port": "",
            "scale_baudrate": 9600,
            "scale_timeout": 1.2,
            "scale_inter_command_delay": 0.05,
            "scale_brand": "zfoc",
            "vfd_enabled": False,
            "vfd_port": "",
            "vfd_baudrate": 9600,
            "vfd_width": 20,
            "vfd_rows": 2,
            "vfd_protocol": "cd5220",
            "vfd_encoding": "ascii",
            "vfd_clear_hex": "0C",
            "vfd_line2_hex": "",
        }

    def _load(self) -> dict[str, Any]:
        defaults = self._default_local_config()
        if not self.config_path.exists():
            return {
                "server_connection": self._empty_connection(),
                "local_config": dict(defaults),
            }
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.config_path.with_suffix(self.config_path.suffix + ".invalid")
            try:
                shutil.copy2(self.config_path, backup)
            except OSError:
                pass
            _logger.exception("Invalid runtime configuration at %s; preserved as %s", self.config_path, backup)
            data = {"server_connection": {"connected": False}}

        data.setdefault("server_connection", {"connected": False})
        local_config = data.setdefault("local_config", {})
        for deprecated_key in ("scale_sse_enabled", "redsys_enabled"):
            local_config.pop(deprecated_key, None)
        for key, value in defaults.items():
            local_config.setdefault(key, value)
        return data

    def _empty_connection(self) -> dict[str, Any]:
        return {
            "connected": False,
            "url": "",
            "token": "",
            "db_uuid": "",
            "enterprise_code": "",
            "db_name": "",
            "last_sync_ok": False,
            "last_sync_message": "",
            "iot_channel": "",
        }

    def _save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.config_path.with_name(f"{self.config_path.name}.tmp")
        payload = json.dumps(self._data, ensure_ascii=True, indent=2)
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
        temp_path.replace(self.config_path)

    def get_connection(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("server_connection", {}))

    def get_public_connection(self) -> dict[str, Any]:
        """Return connection status without pairing credentials."""
        connection = self.get_connection()
        connection.pop("token", None)
        return connection

    def get_local_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("local_config", {}))

    def connect_from_token_url(self, token_url: str) -> dict[str, Any]:
        parsed = urlparse(token_url)
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        db_uuid = (qs.get("db_uuid") or [""])[0]
        enterprise_code = (qs.get("enterprise_code") or [""])[0]
        db_name = (qs.get("db_name") or [""])[0]

        if not token or not db_uuid:
            raise ValueError("Token URL missing required token or db_uuid")

        server_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        with self._lock:
            previous = dict(self._data.get("server_connection", {}))
            same_server = (
                str(previous.get("url") or "").rstrip("/") == server_url
                and str(previous.get("db_uuid") or "") == db_uuid
            )
            self._data["server_connection"] = {
                "connected": True,
                "url": server_url,
                "token": token,
                "db_uuid": db_uuid,
                "enterprise_code": enterprise_code,
                "db_name": db_name,
                "last_sync_ok": False,
                "last_sync_message": "",
            }
            if same_server:
                for key in ("iot_channel", "last_websocket_message_id"):
                    value = previous.get(key)
                    if value:
                        self._data["server_connection"][key] = value
            self._save()
            return dict(self._data["server_connection"])

    def reset_connection(self, *, message: str = "") -> dict[str, Any]:
        with self._lock:
            connection = self._empty_connection()
            if message:
                connection["last_sync_message"] = message
            self._data["server_connection"] = connection
            self._save()
            return dict(connection)

    def reset_local_config(self, *, keep: Iterable[str] = _UNBIND_PROTECTED_KEYS) -> dict[str, Any]:
        """Reset every local setting to its default.

        Only the explicit unbind path may call this.  It discards the printers,
        scale and VFD the previous deployment was configured for, so calling it
        from a failed *pairing* would destroy a working installation -- see
        ``reset_for_unbind`` for the full unbind and ``reset_connection`` for the
        connection-only reset that the failure paths use.
        """
        with self._lock:
            current = self._data.setdefault("local_config", {})
            preserved = {key: current[key] for key in keep if key in current}
            reset = self._default_local_config()
            reset.update(preserved)
            # Never reset onto the plain_http default: the shipped runtime is
            # HTTPS-only, and the certificates clients pin are issued for it.
            # run_https.py re-asserts both on every start; doing it here too
            # keeps the store sane for anything that reads it in between.
            reset["ssl_engine"] = "secure_https"
            reset["service_protocol"] = "https"
            self._data["local_config"] = reset
            self._save()
            return dict(reset)

    def reset_for_unbind(self, *, message: str = "") -> dict[str, Any]:
        """Unbind: drop the pairing *and* the machine configuration it served.

        Deliberately separate from ``reset_connection``, which the failed-connect
        paths also call and which must leave the local configuration alone.
        """
        self.reset_local_config()
        return self.reset_connection(message=message)

    def set_sync_status(self, ok: bool, message: str) -> None:
        with self._lock:
            conn = self._data.setdefault("server_connection", {})
            conn["last_sync_ok"] = bool(ok)
            conn["last_sync_message"] = message
            self._save()

    def update_connection(self, **fields: Any) -> None:
        with self._lock:
            conn = self._data.setdefault("server_connection", {})
            changed = False
            for key, value in fields.items():
                if conn.get(key) != value:
                    conn[key] = value
                    changed = True
            if changed:
                self._save()

    def update_last_websocket_message_id(self, message_id: int, *, flush_interval_seconds: float = 2.0, force: bool = False) -> None:
        if message_id <= 0:
            return
        with self._lock:
            conn = self._data.setdefault("server_connection", {})
            current = int(conn.get("last_websocket_message_id") or 0)
            if message_id < current:
                return
            conn["last_websocket_message_id"] = message_id
            now = monotonic()
            if not force and message_id == current:
                return
            if not force and (now - self._last_message_id_save_at) < flush_interval_seconds:
                return
            self._save()
            self._last_message_id_save_at = now

    def update_local_config(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            config = self._data.setdefault("local_config", {})
            changed = False
            for key, value in fields.items():
                if config.get(key) != value:
                    config[key] = value
                    changed = True
            if changed:
                self._save()
            return dict(config)

    def ensure_iot_identifier(self, preferred: str = "") -> str:
        with self._lock:
            config = self._data.setdefault("local_config", {})
            current = str(config.get("iot_identifier") or "").strip()
            candidate = str(preferred or "").strip()
            if candidate and candidate not in _LEGACY_PACKAGED_IOT_IDENTIFIERS:
                if current != candidate:
                    config["iot_identifier"] = candidate
                    self._save()
                return candidate
            if current and current not in _LEGACY_PACKAGED_IOT_IDENTIFIERS:
                return current
            generated = f"custom-iot-box-{uuid.uuid4().hex[:12]}"
            config["iot_identifier"] = generated
            self._save()
            return generated
