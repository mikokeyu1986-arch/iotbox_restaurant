from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from typing import Any
_logger = logging.getLogger(__name__)

class ReceiptNormalizationMixin:
    def _normalize_receipt_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        discount_total = Decimal("0")
        index = 0
        while index < len(lines):
            line = self._normalize_receipt_line_text(lines[index])
            if self._should_skip_ticket_prefix_line(line):
                index += 1
                continue
            if isinstance(line, dict) and str(line.get("type") or "") == "product_line":
                normalized.append(line)
                discount_total += self._product_discount_amount(line)
                index += 1
                continue
            if isinstance(line, dict) and str(line.get("type") or "") == "spacer":
                normalized.append(line)
                index += 1
                continue
            gift_card_block = self._consume_gift_card_section(lines, index)
            if gift_card_block:
                merged_lines, next_index = gift_card_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            invoice_block = self._consume_invoice_section(lines, index)
            if invoice_block:
                merged_lines, next_index = invoice_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            separator_block = self._prepend_separator_for_reference(lines, index)
            if separator_block:
                merged_lines, next_index = separator_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            header_block = self._consume_header_block(lines, index)
            if header_block:
                merged_lines, next_index = header_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            header_meta_block = self._consume_header_meta_pair(lines, index)
            if header_meta_block:
                merged_lines, next_index = header_meta_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            customer_block = self._consume_customer_block(lines, index)
            if customer_block:
                merged_lines, next_index = customer_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            centered_customer_block = self._consume_centered_customer_block(lines, index)
            if centered_customer_block:
                merged_lines, next_index = centered_customer_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            service_block = self._consume_service_info_block(lines, index)
            if service_block:
                merged_line, next_index = service_block
                normalized.append(merged_line)
                index = next_index
                continue
            change_block = self._consume_change_line(lines, index)
            if change_block:
                merged_line, next_index = change_block
                normalized.append(merged_line)
                index = next_index
                continue
            total_can_skip = self._skip_total_can_block(lines, index)
            if total_can_skip:
                index = total_can_skip
                continue
            summary_block = self._consume_summary_amount_line(lines, index)
            if summary_block:
                merged_line, next_index = summary_block
                normalized.append(merged_line)
                index = next_index
                continue
            label_amount_block = self._consume_label_amount_line(lines, index, discount_total)
            if label_amount_block:
                merged_line, next_index = label_amount_block
                normalized.append(merged_line)
                index = next_index
                continue
            total_block = self._consume_emphasized_total(lines, index)
            if total_block:
                merged_lines, next_index = total_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            duplicate_skip = self._skip_duplicate_summary(lines, index)
            if duplicate_skip:
                index = duplicate_skip
                continue
            payment_terminal_block = self._consume_payment_terminal_receipt(lines, index)
            if payment_terminal_block:
                merged_lines, next_index = payment_terminal_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            product_block = self._consume_product_block(lines, index)
            if product_block:
                merged_line, next_index = product_block
                normalized.append(merged_line)
                discount_total += self._product_discount_amount(merged_line)
                index = next_index
                continue
            kitchen_product_block = self._consume_kitchen_product_line(lines, index)
            if kitchen_product_block:
                merged_line, next_index = kitchen_product_block
                normalized.append(merged_line)
                index = next_index
                continue
            tracking_block = self._consume_tracking_number_line(lines, index)
            if tracking_block:
                normalized.append(tracking_block)
                index += 1
                continue
            if self._should_skip_orphan_weight_fragment(lines, index, normalized):
                index += 1
                continue
            normalized.append(line)
            index += 1
        normalized = self._ensure_discount_summary_line(normalized, discount_total)
        normalized = self._remove_discount_adjacent_separators(normalized)
        deduped: list[dict[str, Any]] = []
        previous_gift_card_title = False
        seen_receipt_code_lines: set[str] = set()
        for item in normalized:
            item_classes = [str(cls) for cls in item.get("classes") or []] if isinstance(item, dict) and isinstance(item.get("classes"), list) else []
            is_gift_card_title = isinstance(item, dict) and "gift-card-title" in item_classes
            if is_gift_card_title and previous_gift_card_title:
                continue
            if self._is_invoice_prompt_line(item):
                continue
            receipt_code_key = self._receipt_code_line_key(item)
            if receipt_code_key:
                if receipt_code_key in seen_receipt_code_lines:
                    continue
                seen_receipt_code_lines.add(receipt_code_key)
            deduped.append(item)
            previous_gift_card_title = is_gift_card_title
        return deduped

    def _normalize_receipt_line_text(self, value: Any):
        if isinstance(value, dict):
            normalized = {key: self._normalize_receipt_line_text(item) for key, item in value.items()}
            return self._normalize_print_line_content(normalized)
        if isinstance(value, list):
            return [self._normalize_receipt_line_text(item) for item in value]
        if isinstance(value, str):
            return self._normalize_print_text(str(value))
        return value

    def _normalize_print_line_content(self, line: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(line)
        line_type = str(normalized.get("type") or "").strip().lower()
        classes = (
            [str(cls).strip().lower() for cls in normalized.get("classes") or []]
            if isinstance(normalized.get("classes"), list)
            else []
        )

        total_like_line = False
        if "receipt-total" in classes or "receipt-total-emphasized" in classes or "label-total" in classes:
            total_like_line = True
        if line_type == "header_meta_line":
            left_text = str(normalized.get("left_text") or "").strip().lower()
            total_like_line = left_text.startswith("total")
        elif line_type != "product_line":
            text = str(normalized.get("text") or "").strip().lower()
            total_like_line = text.startswith("total")

        for key in ("text", "name", "left_text", "right_text", "qty", "total", "unit_price", "original_total"):
            value = normalized.get(key)
            if not isinstance(value, str) or not value:
                continue
            cleaned = self._normalize_print_text(value)
            if key in {"right_text", "total"} and total_like_line:
                amount = self._parse_decimal(cleaned)
                normalized[key] = self._format_amount_like("0.00 Eur", amount) if amount is not None else cleaned.replace("€", "Eur")
            else:
                normalized[key] = self._strip_currency_symbol(cleaned)
        return normalized

    def _normalize_print_text(self, text: str) -> str:
        normalized = self._repair_receipt_mojibake(str(text or ""))
        normalized = self._normalize_spanish_text(normalized)
        if normalized.strip().lower().startswith("table "):
            table_value = normalized.strip()[6:].strip()
            return f"MESA {table_value}".strip().upper()
        return normalized

    def _receipt_code_line_key(self, line: Any) -> str:
        if not isinstance(line, dict):
            return ""
        text = str(line.get("text") or "").strip()
        if not text:
            return ""
        normalized = self._normalize_print_text(text).strip().lower()
        normalized = normalized.replace("c贸digo", "codigo").replace("código", "codigo")
        normalized = re.sub(r"\s+", " ", normalized)
        match = re.search(r"\b(?:codigo|code)\s*:\s*([a-z0-9_-]+)\b", normalized)
        return f"codigo:{match.group(1).lower()}" if match else ""

    def _is_invoice_prompt_line(self, line: Any) -> bool:
        if not isinstance(line, dict):
            return False
        text = str(line.get("text") or "").strip()
        if not text:
            return False
        normalized = self._normalize_print_text(text).strip().lower()
        normalized = re.sub(r"\s+", " ", normalized)
        return (
            "need an invoice" in normalized
            or ("necesita" in normalized and "factura" in normalized)
        )

    def _ensure_receipt_qr_line(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        has_qr = any(
            isinstance(line, dict)
            and str(line.get("type") or "") == "image"
            and str(line.get("image_kind") or "").lower() == "qr"
            for line in lines
        )
        if has_qr:
            return lines

        ticket_url = ""
        insert_at = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if text.startswith(("http://", "https://")) and "/pos/ticket" in text:
                ticket_url = text
                insert_at = index
                break
        if not ticket_url:
            return lines

        qr_size = max(180, int(os.getenv("IOT_PORTAL_QR_SIZE", "200") or "200"))
        qr_line = {
            "type": "image",
            "src": f"/report/barcode?{urlencode({'barcode_type': 'QR', 'value': ticket_url, 'width': qr_size, 'height': qr_size})}",
            "align": "center",
            "classes": ["portal-qr", "auto-receipt-qr"],
            "width": qr_size,
            "height": qr_size,
            "image_kind": "qr",
        }
        _logger.info("Inserted missing receipt QR image for ticket_url=%s", ticket_url)
        return lines[:insert_at] + [qr_line] + lines[insert_at:]

    def _strip_currency_symbol(self, text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"(?i)\bEUR\b", "", cleaned)
        cleaned = cleaned.replace("€", "").replace("$", "")
        return re.sub(r"\s{2,}", " ", cleaned).strip()

    def _ensure_discount_summary_line(self, lines: list[dict[str, Any]], discount_total: Decimal) -> list[dict[str, Any]]:
        if discount_total <= 0:
            return lines

        # If any product line already carries its own discount description
        # (e.g. "50% de descuento en 540,87"), skip the aggregated summary
        # line to avoid duplication and wrong-format amounts.
        for line in lines:
            if isinstance(line, dict) and str(line.get("type") or "") == "product_line":
                if str(line.get("discount_text") or "").strip():
                    return lines

        for line in lines:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip().lower()
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            if "label-discount" in classes or text.startswith("discount"):
                return lines

        discount_line = {
            "type": "header_meta_line",
            "left_text": "Discount",
            "right_text": f"-{self._format_amount_like('$ 0.00', discount_total)}",
            "align": "left",
            "classes": ["label-discount"],
        }

        insert_at = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            text = str(line.get("text") or "").strip().lower()
            if "receipt-total-emphasized" in classes or text.startswith("total "):
                insert_at = index
                break

        if insert_at > 0:
            previous = lines[insert_at - 1]
            if isinstance(previous, dict):
                previous_text = str(previous.get("text") or "").strip()
                previous_classes = (
                    [str(cls) for cls in previous.get("classes") or []]
                    if isinstance(previous.get("classes"), list)
                    else []
                )
                if "receipt-separator" in previous_classes or self._is_separator_line(previous_text):
                    insert_at -= 1

        updated = list(lines)
        updated.insert(insert_at, discount_line)
        return updated

    def _remove_discount_adjacent_separators(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        total_lines = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                cleaned.append(line)
                continue
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            text = str(line.get("text") or "").strip()
            is_separator = "receipt-separator" in classes or self._is_separator_line(text)
            if not is_separator:
                cleaned.append(line)
                continue

            previous_line = lines[index - 1] if index > 0 else None
            next_line = lines[index + 1] if index + 1 < total_lines else None
            previous_classes = (
                [str(cls) for cls in previous_line.get("classes") or []]
                if isinstance(previous_line, dict) and isinstance(previous_line.get("classes"), list)
                else []
            )
            next_classes = (
                [str(cls) for cls in next_line.get("classes") or []]
                if isinstance(next_line, dict) and isinstance(next_line.get("classes"), list)
                else []
            )
            if "label-discount" in previous_classes or "label-discount" in next_classes:
                continue
            cleaned.append(line)
        return cleaned
