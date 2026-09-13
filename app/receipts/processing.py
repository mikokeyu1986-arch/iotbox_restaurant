from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from io import BytesIO
import re
from time import time
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from PIL import Image

from ..models import Device, IoTEvent
from ..receipt_builder import build_kiosk_pickup_ticket_lines, build_lines
from ..printing.common import perf_log as _perf_log
from ..printing.printer_language import extract_zpl_text

_logger = logging.getLogger(__name__)

class ReceiptProcessingMixin:
    def cleanup_spool(self, retention_seconds: int) -> int:
        if retention_seconds < 0:
            return 0
        if not self.spool_dir.exists():
            return 0
        now_ts = time()
        removed = 0
        for pattern in ("receipt_*.bin", "cashbox_*.bin", "native_receipt_*.png"):
            for path in self.spool_dir.glob(pattern):
                try:
                    if not path.is_file():
                        continue
                    age = now_ts - path.stat().st_mtime
                    if age >= retention_seconds:
                        path.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    async def _print_receipt_escpos(self, owner: str, device: Device, data: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(self._process_receipt_escpos, owner, device, data)
        target = result.get("target")
        printed_ok = bool(result.get("ok"))
        error = str(result.get("error") or "ERROR_FAILED")
        if not printed_ok:
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message=error,
                    result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "escpos"},
                )
            )
            # Report the failure.  Returning True here made a job that never
            # reached the paper indistinguishable from a printed one: the
            # caller got ok=true and had no reason to retry or warn.
            return False

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "escpos"},
            )
        )
        return True

    async def _print_receipt_native_image(self, owner: str, device: Device, data: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(self._process_receipt_native_image, owner, device, data)
        target = result.get("target")
        printed_ok = bool(result.get("ok"))
        error = str(result.get("error") or "ERROR_FAILED")
        if not printed_ok:
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message=error,
                    result={"image_file": str(target), "printer": self._printer_name(device), "mode": "native_image"},
                )
            )
            return False

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"image_file": str(target), "printer": self._printer_name(device), "mode": "native_image"},
            )
        )
        return True

    def _process_receipt_native_image(self, owner: str, device: Device, data: dict[str, Any]) -> dict[str, Any]:
        started_at = time()
        payload = data.get("receipt") or {}
        image_data = self._native_receipt_image_bytes(payload)
        if not image_data:
            _logger.warning(
                "Native receipt image print skipped because no image was found owner=%s device=%s",
                owner,
                device.identifier,
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": None}

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"native_receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.png"
        try:
            with Image.open(BytesIO(image_data)) as img:
                img = img.convert("RGB")
                img.save(target, format="PNG")
                printed_ok = self._send_native_image_to_windows_printer(device, img)
                _logger.info(
                    "Native receipt image send result owner=%s device=%s printer=%s printed_ok=%s image=%sx%s file=%s total_ms=%.1f",
                    owner,
                    device.identifier,
                    self._printer_name(device) or "<unknown>",
                    printed_ok,
                    img.width,
                    img.height,
                    target,
                    (time() - started_at) * 1000,
                )
                return {"ok": printed_ok, "error": None if printed_ok else "ERROR_PRINTER", "target": target}
        except Exception as exc:
            _logger.exception(
                "Native receipt image print failed owner=%s device=%s error_type=%s error=%s",
                owner,
                device.identifier,
                type(exc).__name__,
                str(exc),
            )
            return {"ok": False, "error": "ERROR_PRINTER", "target": target}

    def _normalize_label_action(
        self, owner: str, device: Device, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fold a ``print_zpl`` action into the receipt pipeline.

        The label/wristband client names raw ZPL by action and sends it under
        ``zpl``.  No handler is registered for that name, so left alone the
        request reaches the default branch, which acknowledges success without
        touching the printer -- the label never prints and the client is told
        it did.  Returning ``None`` marks a malformed request that must not be
        acknowledged as printed.
        """
        if str(data.get("action") or "") != "print_zpl":
            return data
        zpl_payload = data.get("zpl") or data.get("receipt")
        if not zpl_payload:
            _logger.warning(
                "print_zpl action carried no zpl payload owner=%s device=%s",
                owner,
                device.identifier,
            )
            return None
        _logger.info(
            "Routing print_zpl through the receipt pipeline owner=%s device=%s",
            owner,
            device.identifier,
        )
        return {**data, "action": "print_receipt", "receipt": zpl_payload}

    async def _print_receipt_zpl(self, owner: str, device: Device, data: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(self._process_receipt_zpl, owner, device, data)
        target = result.get("target")
        printed_ok = bool(result.get("ok"))
        error = str(result.get("error") or "ERROR_FAILED")
        if not printed_ok:
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message=error,
                    result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "zpl"},
                )
            )
            # Report the failure.  Returning True here made a job that never
            # reached the paper indistinguishable from a printed one: the
            # caller got ok=true and had no reason to retry or warn.
            return False

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "zpl"},
            )
        )
        return True

    def _process_receipt_zpl(self, owner: str, device: Device, data: dict[str, Any]) -> dict[str, Any]:
        """Forward Odoo's own ZPL instructions to a label printer untouched.

        The IoT Box never builds label content: whatever Odoo sent is written
        to the spool directory byte for byte and handed to the raw printer
        backend (TCP/9100 or the Windows spooler) so the label printer receives
        exactly the instructions the POS produced.
        """
        started_at = time()
        payload = data.get("receipt")
        zpl_text = extract_zpl_text(payload, allow_plain=True)
        if not zpl_text:
            _logger.warning(
                "ZPL print skipped because no raw ZPL content was found owner=%s device=%s printer=%s",
                owner,
                device.identifier,
                self._printer_name(device) or "<unknown>",
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": None}

        config = self.local_config_getter() or {}
        encoding = str(config.get("zpl_encoding") or "utf-8").strip() or "utf-8"
        try:
            body = zpl_text.encode(encoding, errors="replace")
        except LookupError:
            _logger.warning("Unknown ZPL encoding=%s; using utf-8 instead", encoding)
            encoding = "utf-8"
            body = zpl_text.encode(encoding, errors="replace")
        if not body.endswith(b"\n"):
            body += b"\n"

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"receipt_zpl_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        self._write_zpl_debug_payload(zpl_text)
        try:
            target.write_bytes(body)
        except OSError:
            _logger.exception(
                "ZPL spool write failed owner=%s device=%s target=%s",
                owner,
                device.identifier,
                target,
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": target}
        print_started_at = time()
        printed_ok = self._send_raw_to_printer(device, target)
        print_duration_ms = (time() - print_started_at) * 1000
        _logger.info(
            "ZPL send result owner=%s device=%s printer=%s printed_ok=%s bytes=%s labels=%s "
            "encoding=%s send_ms=%.1f total_ms=%.1f",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            printed_ok,
            len(body),
            zpl_text.upper().count("^XZ"),
            encoding,
            print_duration_ms,
            (time() - started_at) * 1000,
        )
        return {"ok": printed_ok, "error": None if printed_ok else "ERROR_PRINTER", "target": target}

    def _write_zpl_debug_payload(self, zpl_text: str) -> None:
        debug_path = self.spool_dir / "last_zpl_payload.zpl"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(zpl_text, encoding="utf-8", errors="replace")
        except OSError:
            pass

    def _process_receipt_escpos(self, owner: str, device: Device, data: dict[str, Any]) -> dict[str, Any]:
        total_started_at = time()
        payload = data.get("receipt") or {}
        # Ensure payload is a dict; Odoo may send either a JSON object string
        # or a rendered receipt image as a data URL for print_receipt.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = self._receipt_string_payload(payload)
        self._write_debug_request_payload(payload)
        structured = payload.get("structured") if isinstance(payload, dict) else None
        lines = payload.get("lines") if isinstance(payload, dict) else None
        raw_order = payload.get("order") if isinstance(payload, dict) else None
        raw_order_data = payload.get("order_data") if isinstance(payload, dict) else None
        kiosk_pickup_ticket = payload.get("kiosk_pickup_ticket") if isinstance(payload, dict) else None
        if isinstance(lines, list):
            lines = self._coerce_embedded_receipt_image_lines(lines)
        _logger.debug(
            "ESCPOS process start owner=%s device=%s printer_name=%s "
            "has_structured=%s has_lines=%s has_raw_order=%s has_order_data=%s payload_type=%s action=%s action_unique_id=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            "yes" if structured else "no",
            "yes" if isinstance(lines, list) else "no",
            "yes" if isinstance(raw_order, dict) else "no",
            "yes" if isinstance(raw_order_data, dict) else "no",
            type(payload).__name__,
            str(data.get("action") or ""),
            str(data.get("action_unique_id") or ""),
        )
        if isinstance(kiosk_pickup_ticket, dict):
            lines = build_kiosk_pickup_ticket_lines(kiosk_pickup_ticket)
            _logger.info("ESCPOS built KIOSK pickup ticket lines=%s owner=%s device=%s", len(lines), owner, device.identifier)
        elif isinstance(structured, dict):
            lines = self._build_structured_receipt_lines(structured)
            _logger.debug(
                "ESCPOS built structured receipt lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        elif isinstance(raw_order_data, dict):
            # Native Odoo kitchen ticket data format from getOrderData()
            # Used by kitchen_print_patch.js which sends the native
            # OrderChangeReceipt data directly to the IoT Box REST.
            lines = self._build_kitchen_from_order_data(raw_order_data)
            _logger.info(
                "ESCPOS built kitchen ticket from order_data lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        elif isinstance(raw_order, dict):
            # Build receipt lines from raw Odoo POS order data.
            # Detect kitchen tickets by checking for kitchen-specific flags
            # in the order or fall back to standard receipt builder.
            kitchen = bool(raw_order.get("kitchen") or raw_order.get("is_kitchen") or False)
            lines = build_lines(raw_order, kitchen=kitchen)
            _logger.info(
                "ESCPOS built receipt lines from raw order data lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        elif isinstance(lines, list) and any(
            isinstance(line, dict)
            and (
                line.get("type") == "product_line"
                or "kitchen-product-line" in {str(value) for value in line.get("classes") or []}
                or "kitchen-note" in {str(value) for value in line.get("classes") or []}
            )
            for line in lines
        ) or (
            isinstance(lines, list)
            and any("order-ref-prefix" in {str(value) for value in line.get("classes") or []} for line in lines if isinstance(line, dict))
            and any("product-name" in {str(value) for value in line.get("classes") or []} for line in lines if isinstance(line, dict))
        ):
            if self._is_kitchen_ticket_lines(lines):
                lines = self._apply_visual_template_to_kitchen_lines(lines)
            else:
                lines = self._apply_visual_template_to_structured_lines(lines)
        if not isinstance(lines, list) or not lines:
            raw_lines_type = type(payload).__name__ if not isinstance(payload, dict) else (
                type(payload.get("lines")).__name__ if payload.get("lines") is not None else "none"
            )
            _logger.warning(
                "ESCPOS no receipt lines found owner=%s device=%s structured=%s raw_lines=%s",
                owner,
                device.identifier,
                "yes" if structured else "no",
                raw_lines_type,
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": None}
        # Lines from raw_order or structured are already fully built and do
        # not need the fragile normalisation pass that was required for the
        # old JS-rendered receipt line format.
        is_kitchen_ticket = self._is_kitchen_ticket_lines(lines)
        is_cash_closure_report = self._is_cash_closure_report_lines(lines)
        skip_normalize = (
            isinstance(raw_order, dict)
            or isinstance(structured, dict)
            or is_cash_closure_report
        )
        if is_kitchen_ticket:
            lines = self._drop_kitchen_image_lines(lines)
            lines = [self._normalize_receipt_line_text(line) for line in lines if isinstance(line, dict)]
        elif not skip_normalize:
            lines = self._normalize_receipt_lines(lines)
            lines = self._ensure_receipt_qr_line(lines)
        elif is_cash_closure_report:
            _logger.info("Preserving native cash-closure report lines=%s", len(lines))
        receipt_summary = self._summarize_escpos_receipt(lines, payload, is_kitchen_ticket)
        _logger.info(
            "ESC/POS receipt prepared owner=%s device=%s printer=%s action_unique_id=%s receipt_type=%s "
            "receipt_ref=%s line_count=%s product_count=%s products=%s fingerprint=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            str(data.get("action_unique_id") or ""),
            receipt_summary["receipt_type"],
            receipt_summary["receipt_ref"],
            len(lines),
            receipt_summary["product_count"],
            receipt_summary["products"],
            receipt_summary["fingerprint"],
        )

        debug_started_at = time()
        self._write_debug_payload(lines)
        debug_duration_ms = (time() - debug_started_at) * 1000
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        try:
            build_started_at = time()
            profile = self.local_config_getter().get("printer_profile", {})
            profile_cut = (
                profile.get("cut", True)
                if isinstance(profile, dict) and profile.get("columns_font_a") is not None
                else True
            )
            cut_enabled = bool(payload.get("cut", profile_cut))
            if is_kitchen_ticket:
                # Kitchen tickets use plain-text ESC/POS (no formatting)
                escpos_bytes = self._build_kitchen_escpos_bytes(
                    lines,
                    cut=cut_enabled,
                    payload=payload,
                )
            else:
                escpos_bytes = self._build_escpos_bytes(
                    lines,
                    cut=cut_enabled,
                    payload=payload,
                    # Raw Odoo orders and structured receipts have already
                    # passed through the visual template. Normalizing them a
                    # second time collapses the significant spaces used by
                    # the configured 48-column product layout.
                    normalize_lines=not skip_normalize,
                )
            build_duration_ms = (time() - build_started_at) * 1000
            write_started_at = time()
            target.write_bytes(escpos_bytes)
            write_duration_ms = (time() - write_started_at) * 1000
            _logger.debug(
                "ESCPOS bytes built and written target=%s bytes=%s build_ms=%.1f write_ms=%.1f",
                target,
                len(escpos_bytes),
                build_duration_ms,
                write_duration_ms,
            )
        except Exception as exc:
            _logger.error(
                "ESCPOS build or write failed owner=%s device=%s error_type=%s error=%s",
                owner,
                device.identifier,
                type(exc).__name__,
                str(exc),
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": target}
        print_started_at = time()
        printed_ok = self._send_raw_to_printer(device, target)
        print_duration_ms = (time() - print_started_at) * 1000
        _logger.info(
            "ESCPOS send result owner=%s device=%s printer=%s printed_ok=%s "
            "bytes=%s build_ms=%.1f send_ms=%.1f total_ms=%.1f",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            printed_ok,
            len(escpos_bytes),
            build_duration_ms,
            print_duration_ms,
            (time() - total_started_at) * 1000,
        )
        _perf_log(
            "ESCPOS profile "
            f"lines={len(lines)} "
            f"bytes={len(escpos_bytes)} "
            f"debug_ms={debug_duration_ms:.1f} "
            f"build_ms={build_duration_ms:.1f} "
            f"write_ms={write_duration_ms:.1f} "
            f"send_ms={print_duration_ms:.1f} "
            f"total_ms={(time() - total_started_at) * 1000:.1f} "
            f"printer={self._printer_name(device) or '<unknown>'} "
            f"ok={printed_ok}"
        )
        return {"ok": printed_ok, "error": "ERROR_PRINTER" if not printed_ok else None, "target": target}

    def _coerce_embedded_receipt_image_lines(self, lines: list[Any]) -> list[Any]:
        coerced: list[Any] = []
        changed = False
        for line in lines:
            if not isinstance(line, dict):
                coerced.append(line)
                continue
            text = str(line.get("text") or "").strip()
            image_src = self._base64_receipt_image_src(text)
            if not image_src:
                coerced.append(line)
                continue
            next_line = dict(line)
            next_line.pop("text", None)
            next_line["type"] = "image"
            next_line["src"] = image_src
            next_line["align"] = next_line.get("align") or "center"
            next_line["image_kind"] = next_line.get("image_kind") or "receipt"
            classes = next_line.get("classes")
            if not isinstance(classes, list):
                classes = []
            next_line["classes"] = [*classes, "embedded-receipt-image"]
            coerced.append(next_line)
            changed = True
        if changed:
            _logger.info("ESCPOS converted embedded base64 receipt image lines=%s", len(lines))
        return coerced

    @staticmethod
    def _is_cash_closure_report_lines(lines: list[Any]) -> bool:
        """Return whether raw lines are an Odoo POS cash-closing report."""
        text = " ".join(
            str(line.get("text") or "")
            for line in lines
            if isinstance(line, dict)
        )
        if re.search(r"\bCIERRE\s+DE\s+CAJA\b", text, flags=re.IGNORECASE):
            return True
        return bool(
            re.search(r"\b(pagos|payments)\s*:", text, flags=re.IGNORECASE)
            and re.search(r"\b(impuestos|taxes)\s*:", text, flags=re.IGNORECASE)
            and re.search(r"\btotal\s*:", text, flags=re.IGNORECASE)
        )

    def _base64_receipt_image_src(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or ""))
        if len(compact) < 200:
            return ""
        if compact.startswith("data:image/"):
            return compact
        image_type = ""
        if compact.startswith("/9j/"):
            image_type = "jpeg"
        elif compact.startswith("iVBOR"):
            image_type = "png"
        elif compact.startswith("R0lGOD"):
            image_type = "gif"
        elif compact.startswith("UklGR"):
            image_type = "webp"
        if not image_type:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            return ""
        return f"data:image/{image_type};base64,{compact}"

    def _receipt_string_payload(self, raw_receipt: str) -> dict[str, Any]:
        text = str(raw_receipt or "").strip()
        if not text:
            return {}
        image_src = self._base64_receipt_image_src(text)
        if image_src:
            text = image_src
        if self._looks_like_receipt_image_source(text):
            return {
                "lines": [
                    {
                        "type": "image",
                        "src": text,
                        "align": "center",
                        "classes": ["rendered-receipt-image"],
                        "image_kind": "receipt",
                    }
                ]
            }
        return {"lines": self._plain_receipt_text_lines(text)}

    def _is_native_receipt_image_action(self, data: dict[str, Any]) -> bool:
        if str(data.get("action") or "") != "print_receipt":
            return False
        return bool(self._native_receipt_image_bytes(data.get("receipt") or {}, probe_only=True))

    def _native_receipt_image_bytes(self, payload: Any, probe_only: bool = False) -> bytes:
        if isinstance(payload, str):
            stripped = payload.strip()
            try:
                payload = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                if self._looks_like_receipt_image_source(stripped) or self._base64_receipt_image_src(stripped):
                    if probe_only:
                        return b"image"
                    src = self._base64_receipt_image_src(stripped) or stripped
                    return self._fetch_receipt_image(src)
                return b""

        if not isinstance(payload, dict):
            return b""
        lines = payload.get("lines")
        if not isinstance(lines, list):
            return b""
        for line in lines:
            if not isinstance(line, dict):
                continue
            line_type = str(line.get("type") or "")
            image_kind = str(line.get("image_kind") or "")
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            src = str(line.get("src") or "").strip()
            if line_type != "image" or not src:
                continue
            if image_kind == "receipt" or "rendered-receipt-image" in classes or "embedded-receipt-image" in classes:
                if probe_only:
                    return b"image"
                return self._fetch_receipt_image(src)
        return b""

    def _looks_like_receipt_image_source(self, text: str) -> bool:
        lowered = text[:128].lower()
        return (
            lowered.startswith("data:image/")
            or lowered.startswith("http://")
            or lowered.startswith("https://")
        )

    def _plain_receipt_text_lines(self, raw_receipt: str) -> list[dict[str, Any]]:
        text = str(raw_receipt or "").strip()
        if not text:
            return []
        if text.startswith("<"):
            try:
                root = ET.fromstring(text)
                lines: list[dict[str, Any]] = []
                self._append_plain_receipt_xml_lines(root, lines)
                if lines:
                    return lines
            except ET.ParseError:
                pass
        return [
            {"text": line.strip(), "align": "left", "bold": False, "classes": []}
            for line in text.splitlines()
            if line.strip()
        ]

    def _append_plain_receipt_xml_lines(
        self,
        node: ET.Element,
        lines: list[dict[str, Any]],
        inherited: dict[str, Any] | None = None,
    ) -> None:
        inherited = dict(inherited or {})
        tag = node.tag.rsplit("}", 1)[-1].lower()
        style = dict(inherited)
        class_attr = str(node.attrib.get("class") or "")
        classes = [part for part in class_attr.split() if part]

        if tag == "center" or str(node.attrib.get("align") or "").lower() == "center":
            style["align"] = "center"
        elif tag == "right" or "pos-receipt-right-align" in classes:
            style["align"] = "right"
        if tag in {"b", "strong", "bold"}:
            style["bold"] = True
        if tag in {"h1", "h2"}:
            style["bold"] = True
            style["double_width"] = True
            style["double_height"] = True

        src = str(node.attrib.get("src") or "").strip()
        if tag == "img" and src:
            lines.append(
                {
                    "type": "image",
                    "src": src,
                    "align": style.get("align", "center"),
                    "classes": classes,
                    "image_kind": "receipt",
                }
            )

        pieces: list[str] = []
        if node.text and node.text.strip():
            pieces.append(node.text.strip())
        for child in list(node):
            self._append_plain_receipt_xml_lines(child, lines, style)
            if child.tail and child.tail.strip():
                pieces.append(child.tail.strip())
        if pieces and tag not in {"receipt", "root", "table", "tbody", "thead", "tr", "img"}:
            lines.append(
                {
                    "text": " ".join(pieces),
                    "align": style.get("align", "left"),
                    "bold": bool(style.get("bold")),
                    "double_width": bool(style.get("double_width")),
                    "double_height": bool(style.get("double_height")),
                    "classes": classes,
                }
            )

    def _is_kitchen_ticket_lines(self, lines: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(line, dict)
            and isinstance(line.get("classes"), list)
            and (
                "kitchen-product-line" in [str(cls) for cls in line.get("classes") or []]
                or "kitchen-table-number" in [str(cls) for cls in line.get("classes") or []]
                or "kitchen-tracking-number" in [str(cls) for cls in line.get("classes") or []]
            )
            for line in lines
        )

    def _is_rendered_receipt_image_lines(self, lines: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(line, dict)
            and str(line.get("type") or "") == "image"
            and (
                str(line.get("image_kind") or "") == "receipt"
                or (
                    isinstance(line.get("classes"), list)
                    and "rendered-receipt-image" in [str(cls) for cls in line.get("classes") or []]
                )
            )
            for line in lines
        )

    def _summarize_escpos_receipt(
        self,
        lines: list[dict[str, Any]],
        payload: dict[str, Any],
        is_kitchen_ticket: bool,
    ) -> dict[str, Any]:
        products: list[str] = []
        references: list[str] = []
        has_total = False
        for line in lines:
            if not isinstance(line, dict):
                continue
            line_type = str(line.get("type") or "")
            if line_type == "product_line":
                qty = str(line.get("qty") or "").strip()
                name = str(line.get("name") or "").strip()
                if name:
                    products.append(f"{qty} {name}".strip())
            if line_type == "header_meta_line":
                left = str(line.get("left_text") or "").strip()
                right = str(line.get("right_text") or "").strip()
                text = " ".join(part for part in (left, right) if part)
            else:
                text = str(line.get("text") or "").strip()
            upper_text = text.upper()
            if text and (text.startswith("#") or "MESA" in upper_text or "TABLE" in upper_text):
                references.append(text)
            if "total" in text.strip().lower():
                has_total = True

        structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else {}
        if structured:
            for key in ("name", "order_name", "pos_reference", "reference", "tracking_number", "table"):
                value = str(structured.get(key) or "").strip()
                if value:
                    references.insert(0, value)
                    break
            if structured.get("total_line"):
                has_total = True

        if is_kitchen_ticket:
            receipt_type = "kitchen"
        elif products and has_total:
            receipt_type = "pos_receipt"
        elif products:
            receipt_type = "product_receipt"
        elif has_total:
            receipt_type = "total_receipt"
        else:
            receipt_type = "unknown"

        fingerprint_source = {
            "receipt_type": receipt_type,
            "refs": references[:4],
            "products": products,
            "total_line": structured.get("total_line") if isinstance(structured, dict) else "",
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8",
                errors="replace",
            )
        ).hexdigest()[:12]
        return {
            "receipt_type": receipt_type,
            "receipt_ref": " | ".join(dict.fromkeys(references[:4]))[:120],
            "product_count": len(products),
            "products": " | ".join(products[:8]),
            "fingerprint": fingerprint,
        }

    def _drop_kitchen_image_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            if isinstance(line, dict) and str(line.get("type") or "") == "image":
                skipped += 1
                continue
            filtered.append(line)
        if skipped:
            _logger.info(
                "Kitchen ESC/POS images skipped skipped_images=%s lines_before=%s lines_after=%s",
                skipped,
                len(lines),
                len(filtered),
            )
        return filtered

    def _write_debug_payload(self, lines: list[dict[str, Any]]) -> None:
        debug_path = self.spool_dir / "last_escpos_payload.json"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                json.dumps(lines, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _write_debug_request_payload(self, payload: dict[str, Any]) -> None:
        debug_path = self.spool_dir / "last_escpos_request.json"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
