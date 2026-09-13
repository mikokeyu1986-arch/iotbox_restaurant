from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import unicodedata

from .product_options import format_option

class TextLayoutMixin:
    def _render_escpos_lines(self, line: dict[str, Any], width: int) -> list[str]:
        # The wrap width has to follow the real glyph width: a 3x-wide glyph fits
        # a third of the columns, and wrapping at width // 2 would overflow the
        # paper.  _line_size_multipliers reads width_multiplier when present and
        # falls back to the double_width boolean.
        width_multiplier, _height_multiplier = self._line_size_multipliers(line)
        effective_width = max(8, width // width_multiplier) if width_multiplier > 1 else width
        if line.get("type") == "spacer":
            return [""]
        classes = [str(cls) for cls in line.get("classes", [])] if isinstance(line.get("classes"), list) else []
        if line.get("type") == "product_line":
            return self._render_product_line(line, width)
        if line.get("type") == "product_header":
            return [str(line.get("text") or "").strip()]
        if line.get("type") == "header_meta_line":
            return self._render_header_meta_line(line, effective_width)
        preformatted_product_classes = {
            "receipt-product-header",
            "receipt-product-row",
            "receipt-product-option-row",
            "receipt-product-unit-price-row",
            "receipt-product-discount-row",
        }
        if preformatted_product_classes.intersection(classes):
            # The visual template already placed every field in an exact
            # character column. Re-parsing this text would collapse the
            # configured quantity/product gutter back to one space.
            text = str(line.get("text") or "").rstrip("\r\n")
            rendered = ""
            for char in text:
                if self._text_width(rendered + char) > effective_width:
                    break
                rendered += char
            return [rendered]
        if "kitchen-attribute" in classes:
            # Attribute indentation is intentional layout, not whitespace to
            # normalize. Keep exactly the four leading spaces produced by the
            # kitchen receipt builder.
            text = str(line.get("text") or "").rstrip("\r\n")
            rendered = ""
            for char in text:
                if self._text_width(rendered + char) > effective_width:
                    break
                rendered += char
            return [rendered] if rendered.strip() else []
        text = str(line.get("text") or "").strip()
        if not text and {"receipt-spacer", "customer-spacer", "product-section-spacer", "payment-terminal-spacer"}.intersection(classes):
            return [""]
        if not text:
            return []

        # Remove the currency symbol from subtotal / tax summary lines so the
        # receipt stays compact (the symbol is only shown on the total).
        compact_lower = text.strip().lower()
        if compact_lower.startswith("subtotal") or self._looks_like_tax_summary_line(text):
            text = re.sub(r"\s*[€$]\s*$", "", text)

        if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
            return ["-" * width]

        align = str(line.get("align") or "left")
        if "merged-label-amount" in classes:
            left_text, right_text = self._split_label_amount_text(text)
            if left_text and right_text:
                return self._wrap_column_line([left_text, right_text], effective_width, classes=classes)
            return self._wrap_aligned_text(text, effective_width, "left")
        if self._looks_like_tax_summary_line(text):
            left_text, right_text = self._split_label_amount_text(text)
            if left_text and right_text:
                return self._wrap_column_line([left_text, right_text], effective_width, classes=["merged-label-amount"])
        if "receipt-total-emphasized" in classes:
            return self._wrap_aligned_text(text, effective_width, "center")
        parts = self._split_receipt_columns(text)
        if len(parts) >= 2 and align == "left":
            return self._wrap_column_line(parts, effective_width, emphasize=bool(line.get("bold")), classes=classes)
        return self._wrap_aligned_text(text, effective_width, align)

    def _looks_like_tax_summary_line(self, text: str) -> bool:
        compact = str(text or "").strip().lower()
        if not compact:
            return False
        if not self._extract_amount(compact):
            return False
        tax_markers = ("igic", "iva", "vat", "tax", "impuesto")
        return any(marker in compact for marker in tax_markers)

    def _render_product_line(self, line: dict[str, Any], width: int) -> list[str]:
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
        first_qty = self._pad_right(qty, qty_width) if is_kitchen or not total else self._pad_center(qty, qty_width)
        first_row = first_qty + " " + self._pad_right(name_lines[0], name_width)
        if total:
            first_row += " " + self._pad_left(total, total_width)
        rows = [first_row]
        for extra_name in name_lines[1:]:
            rows.append(" " * (qty_width + 1) + extra_name)
        if combo_items:
            combo_indent = " " * 3
            combo_width = max(8, width - qty_width - 1 - len(combo_indent))
            for combo_item in combo_items:
                combo_text = format_option(combo_item, fallback_qty=qty)
                combo_rows = self._wrap_text(combo_text, combo_width) or [combo_text]
                rows.extend((" " * (qty_width + 1) + combo_indent + row) for row in combo_rows)
        if discount_text:
            discount_label = self._format_discount_text(discount_text, total, original_total)
            discount_rows = self._wrap_text(discount_label, name_width) or [discount_label]
            rows.extend((" " * (qty_width + 1)) + row for row in discount_rows)
        return rows

    def _render_header_meta_line(self, line: dict[str, Any], width: int) -> list[str]:
        left_text = str(line.get("left_text") or "").strip()
        right_text = str(line.get("right_text") or "").strip()
        if left_text and right_text:
            left_width = self._text_width(left_text)
            right_width = self._text_width(right_text)
            gap = max(1, width - left_width - right_width)
            if left_width + right_width + gap <= width:
                return [left_text + (" " * gap) + right_text]
            rows = self._wrap_aligned_text(left_text, width, "left")
            rows.extend(self._wrap_aligned_text(right_text, width, "right"))
            return rows
        if left_text:
            return self._wrap_aligned_text(left_text, width, "left")
        if right_text:
            return self._wrap_aligned_text(right_text, width, "right")
        return []

    def _format_discount_text(self, text: str, discounted_total: str, original_total: str) -> str:
        clean_text = str(text or "").strip()
        if not clean_text:
            return ""
        compact = clean_text.lower()
        # Full descriptions from the structured payload already include the
        # original price (e.g. "50% de descuento en 540,87 €") — use them
        # directly, only removing the trailing currency symbol.
        if "descuento" in compact or "discount" in compact or "desconto" in compact:
            return re.sub(r"\s*[€$]\s*$", "", clean_text)
        # Bare percentage label (raw order path): "50%" -> "50% discount off on 540,87"
        original_value = self._extract_amount(original_total)
        if not original_value:
            return clean_text
        return f"{clean_text} discount off on {original_value}"

    def _compute_discount_percentage(self, discounted_total: str, original_total: str) -> int | None:
        discounted_value = self._parse_decimal(discounted_total)
        original_value = self._parse_decimal(original_total)
        if discounted_value is None or original_value is None or original_value <= 0:
            return None
        discount_ratio = (original_value - discounted_value) / original_value
        if discount_ratio <= 0:
            return None
        return int((discount_ratio * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _wrap_column_line(
        self,
        parts: list[str],
        width: int,
        emphasize: bool = False,
        classes: list[str] | None = None,
    ) -> list[str]:
        classes = classes or []
        if "merged-label-amount" in classes:
            right = parts[-1].strip()
            left = " ".join(parts[:-1]).strip()
            total_width = max(12, min(18, width // 3))
            left_width = max(8, width - total_width - 1)
            left_lines = self._wrap_text(left, left_width) or [""]
            rows: list[str] = []
            for index, chunk in enumerate(left_lines):
                if index == 0:
                    rows.append(self._pad_right(chunk, left_width) + " " + self._pad_left(right, total_width))
                else:
                    rows.append(chunk)
            if "paymentlines" in classes:
                rows.append("")
            return rows
        product_layout = self._extract_product_line(parts)
        if product_layout:
            qty, product_name, unit_price, total = product_layout
            qty_width, total_width, name_width = self._receipt_column_layout(width)
            name_lines = self._wrap_text(product_name, name_width) or [""]
            rows = [
                self._pad_center(qty, qty_width)
                + " "
                + self._pad_right(name_lines[0], name_width)
                + " "
                + self._pad_left(total, total_width)
            ]
            for extra_name in name_lines[1:]:
                rows.append(" " * (qty_width + 1) + extra_name)
            if self._should_show_unit_price(qty):
                rows.extend(self._wrap_text(unit_price, width))
            return rows

        right = parts[-1]
        left = " ".join(parts[:-1]).strip()
        if len(parts) >= 3 and self._looks_like_amount(parts[-1]) and self._looks_like_amount(parts[-2]):
            right = f"{parts[-2]} {parts[-1]}"
            left = " ".join(parts[:-2]).strip()
        if "pos-receipt-right-align" in classes and len(parts) == 2:
            left, right = parts

        qty_width, right_width, name_width = self._receipt_column_layout(width)
        left_width = qty_width + 1 + name_width
        left_lines = self._wrap_text(left, left_width) or [""]
        rows: list[str] = []
        for index, chunk in enumerate(left_lines):
            if index == 0:
                rows.append(self._pad_right(chunk, left_width) + " " + self._pad_left(right, right_width))
            else:
                rows.append(chunk)
        return rows

    def _receipt_column_layout(self, width: int) -> tuple[int, int, int]:
        safe_width = max(16, width)
        qty_width = 2
        total_width = 8
        name_width = max(8, safe_width - qty_width - total_width - 2)
        return qty_width, total_width, name_width

    def _build_product_header_text(self, width: int) -> str:
        qty_width, total_width, name_width = self._receipt_column_layout(width)
        return (
            self._pad_center("Uds.", qty_width)
            + " "
            + self._pad_right("Producto", name_width)
            + " "
            + self._pad_left("Importe", total_width)
        )

    def _extract_product_line(self, parts: list[str]) -> tuple[str, str, str, str] | None:
        if len(parts) < 3:
            return None

        total = parts[-1].strip()
        if not self._looks_like_amount(total):
            return None

        qty_unit = parts[-2].strip()
        product_name = " ".join(parts[:-2]).strip()
        qty, unit_price = self._split_qty_unit(qty_unit)
        if product_name and qty and unit_price:
            return qty, product_name, unit_price, total

        merged = " ".join(parts).strip()
        patterns = [
            r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*[xX*]\s*(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)\s+(?P<total>[$€]?\d+(?:[.,]\d{1,2})?)$",
            r"^(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<name>.+?)\s+(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)\s+(?P<total>[$€]?\d+(?:[.,]\d{1,2})?)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, merged)
            if not match:
                continue
            name = match.group("name").strip()
            qty = match.group("qty").strip()
            unit = match.group("unit").strip()
            total = match.group("total").strip()
            if name and self._looks_like_amount(total):
                return qty, name, unit, total
        return None

    def _split_qty_unit(self, value: str) -> tuple[str, str]:
        value = value.strip()
        match = re.fullmatch(
            r"(?P<qty>\d+(?:[.,]\d+)?)\s*[xX*]\s*(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)",
            value,
        )
        if match:
            return match.group("qty").strip(), match.group("unit").strip()

        match = re.fullmatch(
            r"(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)",
            value,
        )
        if match:
            return match.group("qty").strip(), match.group("unit").strip()
        return "", ""

    def _should_show_unit_price(self, qty: str) -> bool:
        normalized = qty.strip().replace(",", ".")
        try:
            return float(normalized) != 1.0
        except ValueError:
            return True

    def _wrap_aligned_text(self, text: str, width: int, align: str) -> list[str]:
        rows = self._wrap_text(text, width)
        if align == "center":
            return [self._pad_center(row, width) for row in rows]
        if align == "right":
            return [self._pad_left(row, width) for row in rows]
        return rows

    def _truncate_to_width(self, text: str, width: int) -> str:
        text = str(text or "").strip()
        if not text or self._text_width(text) <= width:
            return text
        result = ""
        for char in text:
            if self._text_width(result + char) > width:
                break
            result += char
        return result

    def _wrap_text(self, text: str, width: int) -> list[str]:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []
        words = text.split(" ")
        rows: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if self._text_width(candidate) <= width:
                current = candidate
                continue
            if current:
                rows.append(current)
                current = ""
            if self._text_width(word) <= width:
                current = word
                continue
            rows.extend(self._break_long_token(word, width))
        if current:
            rows.append(current)
        return rows

    def _break_long_token(self, token: str, width: int) -> list[str]:
        rows: list[str] = []
        current = ""
        for char in token:
            candidate = current + char
            if current and self._text_width(candidate) > width:
                rows.append(current)
                current = char
            else:
                current = candidate
        if current:
            rows.append(current)
        return rows

    def _is_separator_line(self, text: str) -> bool:
        compact = text.strip()
        if len(compact) < 3:
            return False
        return len(set(compact)) == 1 and compact[0] in {"-", "=", "_", "*"}

    def _escpos_line_width(self) -> int:
        profile = self.local_config_getter().get("printer_profile", {})
        if isinstance(profile, dict) and profile.get(key := (
            "columns_font_b" if str(profile.get("font") or "a").lower() == "b" else "columns_font_a"
        )) is not None:
            try:
                return max(24, min(96, int(profile.get(key) or 0)))
            except (TypeError, ValueError):
                pass
        configured = os.getenv("IOT_ESCPOS_LINE_WIDTH", "").strip()
        if configured.isdigit():
            return max(24, int(configured))
        paper = os.getenv("IOT_ESCPOS_PAPER_WIDTH", "80").strip()
        return 48 if paper == "80" else 32

    def _escpos_encoding_config(
        self,
        payload: dict[str, Any] | None = None,
        lines: list[dict[str, Any]] | None = None,
    ) -> tuple[str, int]:
        payload = payload or {}
        requested_encoding = str(
            payload.get("python_encoding")
            or payload.get("encoding")
            or payload.get("charset")
            or payload.get("codepage")
            or ""
        ).strip().lower()
        if requested_encoding in {"utf-8", "utf8"}:
            return "gb18030", 255
        if requested_encoding in {"gb18030", "gbk", "cp936"}:
            return "gb18030", 255
        if requested_encoding in {"cp858", "ibm858"}:
            return "cp858", 19
        if requested_encoding in {"cp850", "ibm850"}:
            return "cp850", 2
        if requested_encoding in {"cp437", "ibm437"}:
            return "cp437", 0
        if requested_encoding in {"cp1252", "windows-1252", "windows1252"}:
            return "cp1252", 16
        # Odoo's kitchen-print action does not always send ``lang`` or an
        # encoding hint.  Choosing the western default in that case causes
        # every CJK glyph to be encoded as ``?``.  Inspect the structured
        # lines as a reliable fallback; explicit caller settings above still
        # take precedence.
        if lines and self._escpos_lines_contain_cjk(lines):
            return "gb18030", 255
        requested_lang = str(payload.get("lang") or "").strip().lower()
        if requested_lang.startswith("zh") and lines and self._is_kitchen_ticket_lines(lines):
            return "gb18030", 255
        if requested_lang.startswith("zh"):
            return "gb18030", 255
        configured = os.getenv("IOT_ESCPOS_ENCODING", "").strip().lower()
        if configured in {"cp858", "ibm858"}:
            return "cp858", 19
        if configured in {"cp850", "ibm850"}:
            return "cp850", 2
        if configured in {"cp437", "ibm437"}:
            return "cp437", 0
        if configured in {"cp1252", "windows-1252", "windows1252"}:
            return "cp1252", 16
        if configured in {"gb18030", "gbk", "cp936"}:
            return "gb18030", 255
        # PC858 is the European ESC/POS code page.  Unlike CP850 it has a
        # real euro glyph (Python encodes "€" as byte 0xD5), so Spanish POS
        # receipts print the symbol instead of falling back to "EUR".
        return "cp858", 19

    def _escpos_lines_contain_cjk(self, lines: list[dict[str, Any]]) -> bool:
        """Return whether a receipt has Han characters needing a CJK code page."""
        def contains_han(value: Any) -> bool:
            if isinstance(value, str):
                return any(
                    "\u3400" <= char <= "\u4dbf"
                    or "\u4e00" <= char <= "\u9fff"
                    or "\uf900" <= char <= "\ufaff"
                    for char in value
                )
            if isinstance(value, dict):
                return any(contains_han(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(contains_han(item) for item in value)
            return False

        return contains_han(lines)

    def _normalize_currency_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = str(text)
        for variant in ("芒鈥毬?", "\u0080"):
            normalized = normalized.replace(variant, "€")
        return normalized

    def _normalize_spanish_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = str(text)
        normalized = normalized.replace("驴", "?").replace("隆", "!")
        normalized = normalized.replace("潞", "o").replace("陋", "a")
        decomposed = unicodedata.normalize("NFKD", normalized)
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    def _repair_receipt_mojibake(self, text: str) -> str:
        if not text:
            return ""
        source = str(text)
        if not self._looks_like_receipt_mojibake(source):
            return source
        candidates = [source]
        for source_encoding in ("gbk", "gb18030"):
            try:
                repaired = source.encode(source_encoding, errors="ignore").decode("utf-8", errors="ignore")
            except Exception:
                continue
            if repaired and repaired not in candidates:
                candidates.append(repaired)
        return max(candidates, key=self._receipt_text_quality)

    def _looks_like_receipt_mojibake(self, text: str) -> bool:
        if not text:
            return False
        suspicious_fragments = (
            "鈧",
            "锟",
            "�",
            "Ã",
            "Â",
            "ð",
            "Ñ",
            "æ",
            "ç",
            "铆",
            "涓",
            "闂",
            "閿",
            "鍙",
            "浠",
            "銆",
        )
        if any(fragment in text for fragment in suspicious_fragments):
            return True
        if any(marker in text for marker in ("娑", "閸", "妤", "閺", "閳", "閿", "閵", "闂")):
            return True
        return any(unicodedata.category(char) in {"Cc", "Cf", "Co"} for char in text)

    def _receipt_text_quality(self, text: str) -> int:
        score = 0
        suspicious_fragments = ("铆", "涓", "闂", "閿", "鍙", "浠", "銆", "鈧", "锟", "Ã", "Â")
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                score += 4
            elif char.isalnum():
                score += 1
            elif char == "?":
                score -= 3
            elif unicodedata.category(char).startswith("C"):
                score -= 5
        if any(fragment in text for fragment in suspicious_fragments):
            score -= 20
        if any(marker in text for marker in ("娑", "閸", "妤", "閺", "閳", "閿", "閵", "闂")):
            score -= 12
        return score

    def _escpos_safe_text(self, text: str, encoding: str) -> str:
        normalized = self._normalize_currency_text(self._normalize_print_text(text))
        try:
            normalized.encode(encoding)
            return normalized
        except UnicodeEncodeError:
            sanitized = self._normalize_spanish_text(normalized)
            try:
                sanitized.encode(encoding)
                return sanitized
            except UnicodeEncodeError:
                return sanitized.replace("€", "EUR")

    def _text_width(self, text: str) -> int:
        text = self._normalize_currency_text(text)
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                width += 2
            else:
                width += 1
        return width

    def _pad_right(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self._text_width(text))

    def _pad_left(self, text: str, width: int) -> str:
        return " " * max(0, width - self._text_width(text)) + text

    def _pad_center(self, text: str, width: int) -> str:
        total = max(0, width - self._text_width(text))
        left = total // 2
        right = total - left
        return (" " * left) + text + (" " * right)

    def _escpos_align(self, align: str) -> bytes:
        if align == "center":
            return b"\x1ba\x01"
        if align == "right":
            return b"\x1ba\x02"
        return b"\x1ba\x00"

    def _escpos_emphasis(self, enabled: bool) -> bytes:
        return b"\x1bE\x01" if enabled else b"\x1bE\x00"

    def _normalize_escpos_multiplier(self, value: Any) -> int:
        if isinstance(value, bool):
            return 2 if value else 1
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(8, normalized))

    def _line_size_multipliers(self, line: dict[str, Any]) -> tuple[int, int]:
        width_multiplier = self._normalize_escpos_multiplier(
            line.get("width_multiplier", line.get("double_width"))
        )
        height_multiplier = self._normalize_escpos_multiplier(
            line.get("height_multiplier", line.get("double_height"))
        )
        return width_multiplier, height_multiplier

    def _escpos_size(self, double_width: Any, double_height: Any = False) -> bytes:
        width_multiplier = self._normalize_escpos_multiplier(double_width)
        height_multiplier = self._normalize_escpos_multiplier(double_height)
        size = ((width_multiplier - 1) << 4) | (height_multiplier - 1)
        return bytes([0x1D, 0x21, size])
