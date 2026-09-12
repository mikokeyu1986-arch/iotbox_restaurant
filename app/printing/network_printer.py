from __future__ import annotations

import logging
import os
from pathlib import Path
import socket
from time import time
from typing import Any
from uuid import uuid4

from ..models import Device
from .printer_language import PRINTER_LANGUAGE_ZPL, extract_zpl_text, normalize_language

_logger = logging.getLogger(__name__)


class NetworkPrinterMixin:
    def _send_raw_to_printer(self, device: Device, target_file: Path) -> bool:
        raw_endpoint = self._raw_tcp_endpoint(device)
        _logger.debug(
            "Send raw to printer device=%s printer_name=%s has_raw_endpoint=%s "
            "windows_printing_available=%s target=%s",
            device.identifier,
            self._printer_name(device) or "<none>",
            "yes" if raw_endpoint else "no",
            "yes" if self._is_windows_native_printing_available() else "no",
            target_file,
        )
        if raw_endpoint:
            return self._send_raw_to_tcp_printer(raw_endpoint[0], raw_endpoint[1], target_file)
        printer_name = self._printer_name(device)
        if self._is_windows_native_printing_available():
            return self._send_raw_to_windows_printer(printer_name, target_file)
        _logger.error(
            "No printer backend available device=%s printer_name=%s has_win32print=%s os_name=%s",
            device.identifier,
            printer_name,
            self._is_windows_native_printing_available(),
            os.name,
        )
        return False
    def _raw_tcp_endpoint(self, device: Device) -> tuple[str, int] | None:
        config = self.local_config_getter() or {}
        mappings = config.get("raw_printer_hosts") or {}
        if not isinstance(mappings, dict):
            mappings = {}
        candidates = [
            device.identifier,
            str(device.name or "").strip(),
            str(device.metadata.get("windows_printer") or "").strip(),
            "printer_main" if device.identifier == self._configured_printer_identifier() else "",
        ]
        raw_value = ""
        for candidate in candidates:
            if candidate and candidate in mappings:
                raw_value = str(mappings.get(candidate) or "").strip()
                break
        if not raw_value:
            raw_value = str(device.metadata.get("raw_tcp_host") or "").strip()
        if not raw_value:
            return None
        host = raw_value
        port = int(device.metadata.get("raw_tcp_port") or config.get("raw_printer_port", 9100) or 9100)
        if ":" in raw_value:
            host_part, port_part = raw_value.rsplit(":", 1)
            host = host_part.strip()
            if port_part.strip().isdigit():
                port = int(port_part.strip())
        if not host:
            return None
        return host, port

    def _send_raw_to_tcp_printer(self, host: str, port: int, target_file: Path) -> bool:
        try:
            payload = target_file.read_bytes()
        except OSError:
            _logger.exception("Raw TCP print failed reading target=%s", target_file)
            return False
        timeout = max(0.5, float((self.local_config_getter() or {}).get("raw_printer_timeout", 4.0) or 4.0))
        started_at = time()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(payload)
        except OSError:
            _logger.exception(
                "Raw TCP print failed host=%s port=%s target=%s bytes=%s",
                host,
                port,
                target_file,
                len(payload),
            )
            return False
        _logger.info(
            "Raw TCP print sent host=%s port=%s target=%s bytes=%s duration_ms=%.1f",
            host,
            port,
            target_file,
            len(payload),
            (time() - started_at) * 1000,
        )
        return True

    def _open_cashbox(self, device: Device) -> bool:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"cashbox_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        # ESC p m t1 t2: pulse on pin 2, timings chosen for common Epson-compatible drawers.
        pulse_bytes = b"\x1b@\x1bp\x00\x19\xfa"
        try:
            target.write_bytes(pulse_bytes)
        except OSError:
            return False

        return self._send_raw_to_printer(device, target)
    def _printer_name(self, device: Device) -> str | None:
        printer_name = str(device.metadata.get("windows_printer") or "").strip()
        if printer_name:
            return printer_name
        return None

    def _device_printer_language(self, device: Device) -> str:
        """Return the printer command language of *device* ("zpl" / "escpos")."""
        metadata = device.metadata if isinstance(device.metadata, dict) else {}
        override = self._configured_printer_language(
            device.identifier,
            str(metadata.get("windows_printer") or ""),
            str(metadata.get("raw_tcp_host") or ""),
        )
        if override:
            return override
        return normalize_language(metadata.get("printer_protocol"))

    def _printer_receipt_handler(self, device: Device, data: dict[str, Any]) -> str:
        """Pick the receipt handler matching the printer command language.

        ZPL label printers only accept ZPL: an ESC/POS byte stream sent to them
        is silently discarded.  The IoT Box never renders label content itself,
        so a ZPL printer is only used when Odoo already sent ZPL instructions;
        anything else falls back to the regular ESC/POS path.
        """
        if self._device_printer_language(device) != PRINTER_LANGUAGE_ZPL:
            return "_print_receipt_escpos"
        if not extract_zpl_text(data.get("receipt"), allow_plain=True):
            _logger.warning(
                "ZPL printer received a payload without raw ZPL content; falling back to ESC/POS "
                "device=%s printer=%s action=%s",
                device.identifier,
                self._printer_name(device) or "<unknown>",
                data.get("action", ""),
            )
            return "_print_receipt_escpos"
        return "_print_receipt_zpl"
