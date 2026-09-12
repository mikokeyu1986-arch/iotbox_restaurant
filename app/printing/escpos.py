from __future__ import annotations

import logging
import os
import re
from time import time
from typing import Any
from uuid import uuid4

from .product_options import format_option

from PIL import Image, ImageDraw, ImageFont

from ..models import Device
from ..receipt_builder import build_lines

_logger = logging.getLogger(__name__)

class EscposEncodingMixin:
    def _build_escpos_bytes(
        self,
        lines: list[dict[str, Any]],
        cut: bool = True,
        payload: dict[str, Any] | None = None,
        normalize_lines: bool = True,
    ) -> bytes:
        width = self._escpos_line_width()
        encoding, codepage = self._escpos_encoding_config(payload=payload, lines=lines)
        product_header_rendered = False
        is_kitchen_ticket = self._is_kitchen_ticket_lines(lines)
        is_rendered_receipt_image = self._is_rendered_receipt_image_lines(lines)
        chunks = [
            b"\x1b@",
            self._escpos_encoding_init(encoding, codepage, payload),
            b"" if is_kitchen_ticket or is_rendered_receipt_image else self._escpos_line_spacing_command(),
        ]
        rendered_lines_source = self._normalize_receipt_lines(lines) if normalize_lines else lines
        for raw_line in rendered_lines_source:
            if not isinstance(raw_line, dict):
                continue
            if raw_line.get("type") == "job_split":
                split_feed_lines = max(0, int(os.getenv("IOT_ESCPOS_SPLIT_FEED_LINES", "4")))
                chunks.append(b"\n" * split_feed_lines)
                chunks.append(b"\x1dV\x00")
                chunks.append(b"\x1b@")
                chunks.append(self._escpos_encoding_init(encoding, codepage, payload))
                chunks.append(b"" if is_kitchen_ticket or is_rendered_receipt_image else self._escpos_line_spacing_command())
                product_header_rendered = False
                continue
            if raw_line.get("type") == "image":
                image_bytes = self._build_escpos_image(raw_line)
                if image_bytes:
                    image_kind = str(raw_line.get("image_kind") or "")
                    image_align = str(raw_line.get("align") or "center")
                    if image_kind == "qr":
                        image_align = "center"
                    chunks.append(self._escpos_align(image_align))
                    chunks.append(image_bytes)
                    chunks.append(b"\n")
                    if image_kind == "barcode":
                        image_classes = (
                            [str(cls) for cls in raw_line.get("classes") or []]
                            if isinstance(raw_line.get("classes"), list)
                            else []
                        )
                        if "no-barcode-separator" not in image_classes:
                            chunks.append(self._escpos_align("left"))
                            chunks.append(self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace"))
                            chunks.append(b"\n")
                continue
            if raw_line.get("type") == "header_meta_line":
                product_header_rendered = False
            if raw_line.get("type") == "product_header":
                # structured receipt already carries the product header line;
                # mark it as rendered so product_line does not print it again.
                product_header_rendered = True
            raw_line_classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )
            if raw_line.get("type") == "spacer" and "product-line-spacer" in raw_line_classes:
                continue
            if raw_line.get("type") == "product_line":
                if not is_kitchen_ticket and not product_header_rendered:
                    chunks.append(self._escpos_align("left"))
                    chunks.append(self._escpos_emphasis(True))
                    chunks.append(self._escpos_safe_text(self._build_product_header_text(width), encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_text_newline())
                    chunks.append(self._escpos_emphasis(False))
                    chunks.append(self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_text_newline())
                    product_header_rendered = True
                chunks.extend(self._build_product_line_chunks(raw_line, width, encoding))
                continue
            if raw_line.get("type") == "service_info_block":
                chunks.extend(self._build_service_info_chunks(raw_line, width, encoding))
                continue
            if raw_line.get("type") == "product_header":
                chunks.append(self._escpos_align("left"))
                chunks.append(self._escpos_emphasis(bool(raw_line.get("bold", True))))
                chunks.append(self._escpos_safe_text(str(raw_line.get("text") or ""), encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
                chunks.append(self._escpos_emphasis(False))
                chunks.append(self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
                continue
            rendered_lines = self._render_escpos_lines(raw_line, width)
            if not rendered_lines:
                continue
            chunks.append(self._escpos_align(str(raw_line.get("align") or "left")))
            chunks.append(self._escpos_emphasis(bool(raw_line.get("bold"))))
            width_multiplier, height_multiplier = self._line_size_multipliers(raw_line)
            chunks.append(
                self._escpos_size(
                    width_multiplier,
                    height_multiplier,
                )
            )
            for rendered in rendered_lines:
                chunks.append(self._escpos_safe_text(rendered, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
            chunks.append(self._escpos_emphasis(False))
            chunks.append(self._escpos_size(False, False))
        if not is_rendered_receipt_image:
            chunks.append(b"\n")
        default_feed_lines = "1" if is_rendered_receipt_image else ("4" if is_kitchen_ticket else "10")
        feed_lines = max(0, int(os.getenv("IOT_ESCPOS_FEED_LINES", default_feed_lines)))
        chunks.append(b"\n" * feed_lines)
        if cut:
            # With a tight line spacing, line feeds alone may leave too
            # little paper below a raster image for slower thermal printers
            # to finish burning it before the cutter activates.
            try:
                cut_feed_dots = int(os.getenv("IOT_ESCPOS_CUT_FEED_DOTS", "160"))
            except ValueError:
                cut_feed_dots = 160
            cut_feed_dots = max(0, min(255, cut_feed_dots))
            if cut_feed_dots:
                chunks.append(bytes([0x1B, 0x4A, cut_feed_dots]))
            chunks.append(b"\x1dV\x00")
        return b"".join(chunks)

    def _render_kitchen_ticket_as_native_image(
        self,
        device: Device,
        lines: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Render kitchen ticket lines as a PIL image and print via Windows GDI.

        This completely bypasses ESC/POS byte generation. Instead, the ticket
        text is rendered as a bitmap image and sent to the Windows printer
        driver through the GDI API (win32ui). This is the "native" printing
        path — the printer driver handles all formatting, font selection, and
        paper handling.

        Returns True if the image was sent to the printer successfully.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            _logger.error("Kitchen native image print failed: PIL not available")
            return False

        # ── Build plain text from kitchen ticket lines ──────────────
        text_lines: list[str] = []
        width = self._escpos_line_width()
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            line_type = str(raw_line.get("type") or "")

            if line_type in {"image", "spacer", "job_split"}:
                continue

            if line_type == "product_line":
                qty = str(raw_line.get("qty") or "").strip()
                name = str(raw_line.get("name") or "").strip()
                total = str(raw_line.get("total") or "").strip()
                combo_items = [
                    str(item).strip()
                    for item in (raw_line.get("combo_items") or [])
                    if str(item).strip()
                ]
                if not qty or not name:
                    continue
                line_text = f"{qty} x {name}"
                if total:
                    line_text += f"  {total}"
                text_lines.append(line_text)
                for combo in combo_items:
                    text_lines.append(f"  + {combo}")
                continue

            if line_type == "header_meta_line":
                left = str(raw_line.get("left_text") or "").strip()
                right = str(raw_line.get("right_text") or "").strip()
                if left and right:
                    gap = max(1, width - self._text_width(left) - self._text_width(right))
                    text_lines.append(left + (" " * gap) + right)
                else:
                    text = left or right
                    if text:
                        text_lines.append(text)
                continue

            if line_type in {"service_info_block", "product_header"}:
                continue

            text = str(raw_line.get("text") or "").strip()
            if not text:
                continue

            classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )

            if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
                text_lines.append("-" * width)
                continue

            if "invoice-asterisk-border" in classes:
                continue

            text_lines.append(text)

        if not text_lines:
            _logger.warning("Kitchen ticket native image print skipped: no text content")
            return False

        # ── Render text as PIL image ────────────────────────────────
        # Font selection: try common monospace fonts for clean alignment
        font_size = 22
        font = None
        for font_name in ("cour.ttf", "consola.ttf", "lucon.ttf", "CascadiaMono.ttf"):
            try:
                font_path = f"C:\\Windows\\Fonts\\{font_name}"
                font = ImageFont.truetype(font_path, font_size)
                break
            except (IOError, OSError):
                continue
        if font is None:
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except (IOError, OSError):
                font = ImageFont.load_default()

        # Calculate character cell size for layout
        sample_char = "W"
        try:
            bbox = font.getbbox(sample_char)
            char_width = bbox[2] - bbox[0]
            char_height = bbox[3] - bbox[1]
        except AttributeError:
            try:
                char_width, char_height = font.getsize(sample_char)
            except AttributeError:
                char_width = font_size
                char_height = font_size

        line_spacing = max(char_height + 4, int(font_size * 1.25))
        margin = 24
        image_width = max(320, char_width * width + margin * 2)
        image_height = max(100, len(text_lines) * line_spacing + margin * 2)

        img = Image.new("RGB", (image_width, image_height), "white")
        draw = ImageDraw.Draw(img)

        y = margin
        for text_line in text_lines:
            draw.text((margin, y), text_line, fill="black", font=font)
            y += line_spacing

        # ── Convert to ESC/POS raster and send as raw data ──────────
        # Thermal receipt printers (like COCINA) do NOT support GDI-based
        # printing (win32ui.CreateDC). They expect raw ESC/POS data.
        # We convert the PIL image to ESC/POS raster format and send it
        # via the raw Windows print spooler (win32print.WritePrinter).
        _logger.info(
            "Kitchen ticket rendered as image lines=%s image=%sx%s char_width=%s char_height=%s",
            len(text_lines),
            image_width,
            image_height,
            char_width,
            char_height,
        )
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"native_receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.png"
        try:
            img.save(target, format="PNG")
        except Exception:
            pass
        # Convert PIL image to 1-bit black/white for ESC/POS raster
        bw_img = img.convert("1")
        escpos_bytes = self._image_to_escpos_raster(bw_img)
        escpos_bytes += b"\n" * 4 + (b"\x1dV\x00" if payload and payload.get("cut", True) else b"")
        spool_target = self.spool_dir / f"receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        try:
            spool_target.write_bytes(escpos_bytes)
        except Exception:
            pass
        return self._send_raw_to_printer(device, spool_target)

    def _build_kitchen_from_order_data(self, order_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Build a native Odoo kitchen ticket through the visual template."""
        changes = order_data.get("changes") if isinstance(order_data.get("changes"), dict) else {}
        table_number = str(order_data.get("table_number") or order_data.get("table_name") or "").strip()
        normalized = {
            **order_data,
            "kitchen": True,
            "kitchen_title": str(changes.get("title") or order_data.get("title") or "NUEVO"),
            "course_groups": changes.get("groupedData") or [],
            "table_id": {"table_number": table_number} if table_number else {},
            "config": {"name": str(order_data.get("config_name") or "")},
            "date_order": str(order_data.get("time") or order_data.get("date_order") or ""),
        }
        lines = build_lines(normalized, kitchen=True)

        # The visual kitchen template owns the line size.  Only honor the
        # legacy kitchen_font_mode when it is explicitly supplied; the old
        # default of "normal" silently overwrote the configured product size.
        font_mode = str(order_data.get("kitchen_font_mode") or "").strip().lower()
        if font_mode:
            for line in lines:
                if line.get("type") != "product_line":
                    continue
                line["double_width"] = font_mode == "double_width"
                line["double_height"] = font_mode == "double_height"
        _logger.info(
            "Built templated kitchen ticket lines=%s font_mode=%s",
            len(lines),
            font_mode,
        )
        return lines
    def _build_kitchen_escpos_bytes(
        self,
        lines: list[dict[str, Any]],
        cut: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> bytes:
        """Build ESC/POS bytes for kitchen tickets with formatting.

        Supports alignment, bold, and double-width/height for title,
        order type, table number, and other emphasized lines. Images
        (logos, QR codes, barcodes) are dropped. Product lines use
        the plain kitchen format (qty x name).
        """
        width = self._escpos_line_width()
        encoding, codepage = self._escpos_encoding_config(payload=payload, lines=lines)
        chunks: list[bytes] = [
            b"\x1b@",
            self._escpos_encoding_init(encoding, codepage, payload),
        ]
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            line_type = str(raw_line.get("type") or "")

            if line_type in {"image", "spacer", "job_split"}:
                continue

            if line_type == "vspace":
                chunks.append(b"\n")
                continue

            if line_type == "product_line":
                qty = str(raw_line.get("qty") or "").strip()
                name = str(raw_line.get("name") or "").strip()
                total = str(raw_line.get("total") or "").strip()
                combo_items = [
                    str(item).strip()
                    for item in (raw_line.get("combo_items") or [])
                    if str(item).strip()
                ]
                if not qty or not name:
                    continue
                line_text = f"{qty} x {name}"
                if total:
                    line_text += f"  {total}"
                dw, dh = self._line_size_multipliers(raw_line)
                bold = bool(raw_line.get("bold", False))
                chunks.append(self._escpos_align("left"))
                chunks.append(self._escpos_emphasis(bold))
                if dw != 1 or dh != 1:
                    chunks.append(self._escpos_size(dw, dh))
                chunks.append(
                    self._escpos_safe_text(line_text, encoding).encode(encoding, errors="replace")
                )
                if dw != 1 or dh != 1:
                    chunks.append(self._escpos_size(False, False))
                chunks.append(self._escpos_emphasis(False))
                chunks.append(b"\n")
                for combo in combo_items:
                    chunks.append(self._escpos_align("left"))
                    chunks.append(
                        self._escpos_safe_text(f"  - {combo}", encoding).encode(encoding, errors="replace")
                    )
                    chunks.append(b"\n")
                continue

            if line_type == "header_meta_line":
                left = str(raw_line.get("left_text") or "").strip()
                right = str(raw_line.get("right_text") or "").strip()
                if left and right:
                    gap = max(1, width - self._text_width(left) - self._text_width(right))
                    text = left + (" " * gap) + right
                else:
                    text = left or right
                if text:
                    is_bold = bool(raw_line.get("bold"))
                    dw = bool(raw_line.get("double_width"))
                    dh = bool(raw_line.get("double_height"))
                    chunks.append(self._escpos_align("left"))
                    chunks.append(self._escpos_emphasis(is_bold))
                    if dw or dh:
                        chunks.append(self._escpos_size(dw, dh))
                    chunks.append(
                        self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
                    )
                    if dw or dh:
                        chunks.append(self._escpos_size(False, False))
                    chunks.append(self._escpos_emphasis(False))
                    chunks.append(b"\n")
                continue

            if line_type == "service_info_block":
                table_text = str(raw_line.get("table_text") or "").strip()
                guests_text = str(raw_line.get("guests_text") or "").strip()
                served_by_text = str(raw_line.get("served_by_text") or "").strip()
                for text in (table_text, guests_text, served_by_text):
                    if text:
                        chunks.append(self._escpos_align("center"))
                        chunks.append(self._escpos_emphasis(True))
                        chunks.append(
                            self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
                        )
                        chunks.append(self._escpos_emphasis(False))
                        chunks.append(b"\n")
                continue

            if line_type == "product_header":
                continue

            text = str(raw_line.get("text") or "").strip()
            if not text:
                continue

            classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )

            if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
                chunks.append(self._escpos_align("left"))
                chunks.append(
                    self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace")
                )
                chunks.append(b"\n")
                continue

            if "invoice-asterisk-border" in classes:
                continue

            # Apply formatting for regular text lines
            align = str(raw_line.get("align") or "left")
            is_bold = bool(raw_line.get("bold"))
            dw = bool(raw_line.get("double_width"))
            dh = bool(raw_line.get("double_height"))

            chunks.append(self._escpos_align(align))
            chunks.append(self._escpos_emphasis(is_bold))
            if dw or dh:
                chunks.append(self._escpos_size(dw, dh))
            chunks.append(
                self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
            )
            if dw or dh:
                chunks.append(self._escpos_size(False, False))
            chunks.append(self._escpos_emphasis(False))
            chunks.append(b"\n")

        default_feed_lines = "4"
        feed_lines = max(0, int(os.getenv("IOT_ESCPOS_FEED_LINES", default_feed_lines)))
        chunks.append(b"\n" * feed_lines)
        if cut:
            chunks.append(b"\x1dV\x00")
        return b"".join(chunks)

    def _escpos_encoding_init(
        self,
        encoding: str,
        codepage: int,
        payload: dict[str, Any] | None = None,
    ) -> bytes:
        if encoding == "utf-8":
            return b""
        if encoding == "gb18030":
            payload = payload or {}
            chinese_mode = str(
                payload.get("cn_init_mode")
                or os.getenv("IOT_ESCPOS_CN_INIT_MODE", "esc_r_15_esc_t_255")
            ).strip().lower()
            if chinese_mode == "fs_amp":
                return b"\x1c&"
            if chinese_mode == "esc_t_255_fs_amp":
                return b"\x1bt\xff\x1c&"
            if chinese_mode == "esc_r_15_esc_t_255":
                return b"\x1bR\x0f\x1bt\xff"
            return b"\x1bt\xff"
        return bytes([0x1B, 0x74, codepage])

    def _escpos_line_spacing_command(self) -> bytes:
        # ESC/POS line spacing is measured in printer dots.  48 was too tight
        # for the current receipt layout; keep it configurable for different
        # printer mechanisms while using a more readable default.
        try:
            spacing = int(os.getenv("IOT_ESCPOS_LINE_SPACING", "36"))
        except ValueError:
            spacing = 36
        spacing = max(0, min(255, spacing))
        return bytes([0x1B, 0x33, spacing])

    def _escpos_text_newline(self) -> bytes:
        return b"\n"

    def _build_product_line_chunks(self, line: dict[str, Any], width: int, encoding: str) -> list[bytes]:
        qty = str(line.get("qty") or "").strip()
        product_name = str(line.get("name") or "").strip()
        unit_price = str(line.get("unit_price") or "").strip()
        total = str(line.get("total") or "").strip()
        discount_text = str(line.get("discount_text") or "").strip()
        original_total = str(line.get("original_total") or "").strip()
        combo_items = [item for item in (line.get("combo_items") or []) if item not in (None, "")]
        if not qty or not product_name:
            return []
        classes = [str(item) for item in (line.get("classes") or [])]
        is_kitchen = "kitchen-product-line" in classes

        if total:
            qty_width, total_width, name_width = self._receipt_column_layout(width)
        else:
            qty_width = max(2, min(3, len(qty)))
            total_width = 0
            name_width = max(8, width - qty_width - 1)
        name_lines = self._wrap_text(product_name, name_width) or [""]
        first_line_prefix = self._pad_right(qty, qty_width) if is_kitchen or not total else self._pad_center(qty, qty_width)
        if total:
            first_line_suffix = " " + self._pad_right(name_lines[0], name_width) + " " + self._pad_left(total, total_width)
        else:
            first_line_suffix = " " + self._pad_right(name_lines[0], name_width)

        chunks = [
            self._escpos_align("left"),
            self._escpos_emphasis(True),
            self._escpos_size(*self._line_size_multipliers(line)),
            self._escpos_safe_text(first_line_prefix, encoding).encode(encoding, errors="replace"),
            self._escpos_emphasis(False),
            self._escpos_safe_text(first_line_suffix, encoding).encode(encoding, errors="replace"),
            self._escpos_text_newline(),
        ]
        for extra_name in name_lines[1:]:
            continuation_line = " " * (qty_width + 1) + self._pad_right(extra_name, name_width)
            if total:
                continuation_line += " " + " " * total_width
            chunks.append(self._escpos_safe_text(continuation_line, encoding).encode(encoding, errors="replace"))
            chunks.append(self._escpos_text_newline())
        if combo_items:
            combo_indent = " " * 3
            combo_width = max(8, name_width - len(combo_indent))
            for combo_item in combo_items:
                combo_text = format_option(combo_item, fallback_qty=qty)
                combo_rows = self._wrap_text(combo_text, combo_width) or [combo_text]
                for row in combo_rows:
                    combo_line = " " * (qty_width + 1) + self._pad_right(combo_indent + row, name_width)
                    if total:
                        combo_line += " " + " " * total_width
                    # Combo (套餐) children — including their quantity suffix
                    # (e.g. "x2") — are printed bold to stand out.
                    chunks.append(self._escpos_emphasis(True))
                    chunks.append(self._escpos_safe_text(combo_line, encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_emphasis(False))
                    chunks.append(self._escpos_text_newline())
        if discount_text:
            discount_label = self._format_discount_text(discount_text, total, original_total)
            discount_rows = self._wrap_text(discount_label, name_width) or [discount_label]
            for row in discount_rows:
                discount_line = (
                    " " * (qty_width + 1)
                    + self._pad_right(row, name_width)
                    + " "
                    + " " * total_width
                )
                chunks.append(self._escpos_safe_text(discount_line, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        chunks.append(self._escpos_size(False, False))
        return chunks

    @staticmethod
    def _split_attribute_quantity(combo_item: str) -> tuple[str, str]:
        """Split a trailing quantity suffix off an attribute label.

        Odoo POS packs combo/attribute quantities into the label itself, e.g.
        "Lunch Salmon 20pc x2" or "Cafe 1.5L x1.5".  Returns ``(qty, name)``
        with the suffix removed; ``qty`` is empty when the label carries no
        quantity (e.g. "Water").
        """
        item = str(combo_item or "").strip()
        match = re.search(r"x(\d+(?:[.,]\d+)?)\s*$", item, flags=re.IGNORECASE)
        if not match:
            return "", item
        attr_name = item[: match.start()].strip().rstrip("xX\t ").strip()
        qty_raw = match.group(1).replace(",", ".")
        qty = qty_raw
        try:
            from decimal import Decimal
            qty_number = Decimal(qty_raw)
            if qty_number == qty_number.to_integral_value():
                qty = str(int(qty_number))
        except Exception:
            qty = qty_raw
        return qty, attr_name

    def _build_service_info_chunks(self, line: dict[str, Any], width: int, encoding: str) -> list[bytes]:
        table_text = str(line.get("table_text") or "").strip()
        guests_text = str(line.get("guests_text") or "").strip()
        served_by_text = str(line.get("served_by_text") or "").strip()
        if not table_text:
            return []

        chunks: list[bytes] = []

        chunks.append(self._escpos_align("center"))
        chunks.append(self._escpos_emphasis(True))
        chunks.append(self._escpos_size(True))
        for row in self._wrap_text(table_text, max(16, width // 2)) or [table_text]:
            chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
            chunks.append(self._escpos_text_newline())
        chunks.append(self._escpos_emphasis(False))
        chunks.append(self._escpos_size(False))

        if guests_text:
            chunks.append(self._escpos_align("center"))
            for row in self._wrap_text(guests_text, width) or [guests_text]:
                chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        if served_by_text:
            chunks.append(self._escpos_align("center"))
            for row in self._wrap_text(served_by_text, width) or [served_by_text]:
                chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        return chunks
