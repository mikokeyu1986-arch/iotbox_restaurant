from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

class ProductParserMixin:
    def _consume_product_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        qty_line = self._line_with_class(lines, start, "qty")
        name_line = None
        product_price_line = None
        extra_line = None
        combo_items: list[str] = []
        last_index = start

        if qty_line:
            window = lines[start + 1 : start + 6]
            qty = str(qty_line.get("text") or "").strip()
            for offset, candidate in enumerate(window, start=1):
                candidate_index = start + offset
                if not isinstance(candidate, dict):
                    continue
                classes = (
                    [str(cls) for cls in candidate.get("classes") or []]
                    if isinstance(candidate.get("classes"), list)
                    else []
                )
                if name_line is None and "d-inline" in classes:
                    name_line = candidate
                    last_index = candidate_index
                    continue
                if product_price_line is None and "product-price" in classes:
                    product_price_line = candidate
                    last_index = candidate_index
                    continue
                if extra_line is None and "price-per-unit" in classes:
                    extra_line = candidate
                    last_index = candidate_index
                    continue
                if "qty" in classes or "product-price" in classes:
                    break
        else:
            current = lines[start]
            if not isinstance(current, dict):
                return None
            classes = (
                [str(cls) for cls in current.get("classes") or []]
                if isinstance(current.get("classes"), list)
                else []
            )
            if "d-inline" not in classes:
                return None
            qty = "1"
            name_line = current
            for offset, candidate in enumerate(lines[start + 1 : start + 4], start=1):
                candidate_index = start + offset
                if not isinstance(candidate, dict):
                    continue
                candidate_classes = (
                    [str(cls) for cls in candidate.get("classes") or []]
                    if isinstance(candidate.get("classes"), list)
                    else []
                )
                if product_price_line is None and "product-price" in candidate_classes:
                    product_price_line = candidate
                    last_index = candidate_index
                    continue
                if extra_line is None and "price-per-unit" in candidate_classes:
                    extra_line = candidate
                    last_index = candidate_index
                    continue
                if "d-inline" in candidate_classes or "qty" in candidate_classes:
                    break

        if not name_line or not product_price_line:
            return None

        raw_product_price = str(product_price_line.get("text") or "").strip()
        extra_text = str(extra_line.get("text") or "").strip() if extra_line else ""
        total = self._normalize_amount_display(self._extract_amount(raw_product_price), raw_product_price)
        if not qty or not total:
            return None

        name = str(name_line.get("text") or "").strip()
        extra_amount = self._extract_amount(extra_text)
        qty = self._recover_product_qty(qty, total, extra_text)
        lowered_extra = extra_text.lower()
        if extra_text and ("descuento" in lowered_extra or "discount" in lowered_extra):
            discount_text = extra_text
            unit_price = self._derive_unit_price(qty, total, raw_product_price)
            original_total = self._normalize_amount_display(extra_amount, extra_text)
        else:
            discount_text = ""
            unit_price = self._normalize_amount_display(extra_amount, extra_text) if extra_amount else self._derive_unit_price(qty, total, raw_product_price)
            original_total = total

        combo_start_index = last_index + 1
        for offset, candidate in enumerate(lines[combo_start_index : combo_start_index + 5]):
            candidate_index = combo_start_index + offset
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if not candidate_text:
                continue
            if "d-inline" in candidate_classes and "product-price" not in candidate_classes:
                next_candidate = lines[candidate_index + 1] if candidate_index + 1 < len(lines) else None
                if isinstance(next_candidate, dict):
                    next_candidate_classes = (
                        [str(cls) for cls in next_candidate.get("classes") or []]
                        if isinstance(next_candidate.get("classes"), list)
                        else []
                    )
                    if "product-price" in next_candidate_classes:
                        break
                combo_items.append(candidate_text)
                last_index = candidate_index
                continue
            if (
                "qty" in candidate_classes
                or "product-price" in candidate_classes
                or self._looks_like_amount(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "discount"))
            ):
                break

        merged = {
            "type": "product_line",
            "qty": qty,
            "name": name,
            "unit_price": unit_price,
            "total": total,
            "discount_text": discount_text,
            "original_total": original_total,
            "combo_items": combo_items,
            "align": "left",
            "classes": ["product-line-merged"],
        }
        return merged, last_index + 1

    def _recover_product_qty(self, qty: str, total: str, extra_text: str) -> str:
        qty_value = self._parse_decimal(qty)
        if qty_value is None or qty_value != 0:
            return qty

        unit_amount = self._extract_amount(extra_text)
        unit_value = self._parse_decimal(unit_amount or extra_text)
        total_value = self._parse_decimal(total)
        if unit_value is None or total_value is None or unit_value == 0 or total_value == 0:
            return qty

        recovered = (total_value / unit_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return self._format_quantity_display(recovered, qty)

    def _format_quantity_display(self, value: Decimal, sample: str) -> str:
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if not text:
            text = "0"
        if "," in sample and "." not in sample:
            text = text.replace(".", ",")
        return text

    def _is_orphan_weight_fragment(self, text: str) -> bool:
        return text.strip().lower() in {"on", "kg", "g", "lb", "oz"}

    def _consume_kitchen_product_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        qty_line = lines[start] if start < len(lines) else None
        name_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(qty_line, dict) or not isinstance(name_line, dict):
            return None

        qty_classes = (
            [str(cls) for cls in qty_line.get("classes") or []]
            if isinstance(qty_line.get("classes"), list)
            else []
        )
        name_classes = (
            [str(cls) for cls in name_line.get("classes") or []]
            if isinstance(name_line.get("classes"), list)
            else []
        )
        if "me-3" not in qty_classes or "product-name" not in name_classes:
            return None

        qty = str(qty_line.get("text") or "").strip()
        name = str(name_line.get("text") or "").strip()
        if not qty or not name:
            return None
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", qty):
            return None

        return (
            {
                "text": f"{qty.rjust(2)} x {name}",
                "align": "left",
                "classes": ["kitchen-product-line"],
            },
            start + 2,
        )

    def _line_with_class(self, lines: list[dict[str, Any]], index: int, class_name: str) -> dict[str, Any] | None:
        if index >= len(lines):
            return None
        line = lines[index]
        if not isinstance(line, dict):
            return None
        classes = line.get("classes")
        if not isinstance(classes, list):
            return None
        if class_name not in [str(cls) for cls in classes]:
            return None
        return line

    def _extract_amount(self, text: str) -> str:
        text = text.strip()
        amount_pattern = r"[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?"
        matches = re.findall(amount_pattern, text)
        if not matches:
            return ""
        return matches[-1].strip()

    def _extract_signed_amount(self, text: str) -> str:
        text = text.strip()
        amount_pattern = r"[-+]?[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?"
        matches = re.findall(amount_pattern, text)
        if not matches:
            return self._extract_amount(text)
        return matches[-1].strip()

    def _derive_unit_price(self, qty: str, total: str, fallback: str) -> str:
        if not self._should_show_unit_price(qty):
            return ""

        qty_value = self._parse_decimal(qty)
        total_value = self._parse_decimal(total)
        if qty_value is not None and total_value is not None and qty_value != 0:
            unit_value = (total_value / qty_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return self._format_amount_like(total, unit_value)

        fallback_amount = self._extract_amount(fallback)
        return fallback_amount or fallback.strip()

    def _normalize_amount_display(self, amount_text: str, sample: str) -> str:
        amount_value = self._parse_decimal(amount_text)
        if amount_value is None:
            return amount_text
        return self._format_amount_like(amount_text or sample, amount_value)

    def _parse_decimal(self, value: str) -> Decimal | None:
        cleaned = self._normalize_currency_text(value).strip()
        cleaned = re.sub(r"(?i)EUR", "", cleaned)
        cleaned = cleaned.replace("€", "").replace("$", "")
        cleaned = cleaned.replace(" ", "")
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2:
                cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    def _format_amount_like(self, sample: str, amount: Decimal) -> str:
        stripped = self._normalize_currency_text(str(sample or "").strip())
        currency_symbols = "$€"
        symbol_prefix = ""
        symbol_suffix = ""
        decimal_separator = "."
        thousands_separator = ","

        first_digit_match = re.search(r"\d", stripped)
        first_digit_index = first_digit_match.start() if first_digit_match else -1
        if first_digit_index >= 0:
            prefix_part = stripped[:first_digit_index]
            suffix_part = stripped[first_digit_index + 1 :]
            for char in prefix_part:
                if char in currency_symbols:
                    symbol_prefix = char
                    break
            for char in reversed(suffix_part):
                if char in currency_symbols:
                    symbol_suffix = char
                    break
        elif stripped and stripped[0] in currency_symbols:
            symbol_prefix = stripped[0]
        elif stripped and stripped[-1] in currency_symbols:
            symbol_suffix = stripped[-1]

        separators = re.findall(r"\d([.,])\d", stripped)
        if separators:
            decimal_separator = separators[-1]
            thousands_separator = "." if decimal_separator == "," else ","

        text = f"{amount:.2f}"
        integer_part, decimal_part = text.split(".")
        grouped_integer = f"{int(integer_part):,}".replace(",", thousands_separator)
        text = f"{grouped_integer}{decimal_separator}{decimal_part}"

        if "\u20ac" in stripped:
            return f"{text} \u20ac"

        if symbol_prefix:
            space_after = " " if re.search(rf"{re.escape(symbol_prefix)}\s+\d", stripped) else ""
            return f"{symbol_prefix}{space_after}{text}"
        if symbol_suffix:
            space_before = " " if re.search(rf"\d\s+{re.escape(symbol_suffix)}", stripped) else ""
            return f"{text}{space_before}{symbol_suffix}"
        return text
