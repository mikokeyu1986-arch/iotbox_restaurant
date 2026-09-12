from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any
_logger = logging.getLogger(__name__)

class ReceiptSectionConsumerMixin:
    def _consume_gift_card_section(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if {"gift-card-title", "gift-card-code", "gift-card-amount", "gift-card-qr", "gift-card-barcode"}.intersection(classes):
            return None
        if text.lower() != "gift card":
            return None

        code_line = lines[start + 1] if start + 1 < len(lines) else None
        qr_line = lines[start + 2] if start + 2 < len(lines) else None
        amount_line = lines[start + 3] if start + 3 < len(lines) else None
        if not all(isinstance(item, dict) for item in [code_line, qr_line]):
            return None
        if str(qr_line.get("type") or "") != "image" or str(qr_line.get("image_kind") or "") not in {"qr", "barcode"}:
            return None

        merged_lines = [
            {**line, "align": "center", "bold": True},
            {**code_line, "align": "center"},
            {**qr_line, "align": "center"},
        ]
        next_index = start + 3
        if isinstance(amount_line, dict):
            amount_text = self._normalize_gift_card_amount(str(amount_line.get("text") or "").strip())
            if amount_text and self._looks_like_amount(amount_text):
                merged_lines.append(
                    {
                        "text": amount_text,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["gift-card-amount"],
                    }
                )
                next_index = start + 4
        return merged_lines, next_index

    def _normalize_gift_card_amount(self, text: str) -> str:
        amount = self._extract_amount(text)
        return amount or text.strip()

    def _gift_card_display_amount(self, loyalty_card: dict[str, Any]) -> tuple[str, str]:
        preferred_fields = (
            "point",
            "balance",
            "remaining_balance",
            "current_balance",
            "available_balance",
            "remaining_amount",
            "balance_amount",
            "amount",
        )
        for field_name in preferred_fields:
            raw_value = loyalty_card.get(field_name)
            text = str(raw_value or "").strip()
            if not text:
                continue
            if field_name == "point":
                normalized_number = self._normalize_gift_card_amount(text)
                if normalized_number:
                    return field_name, self._format_amount_like("0,00 €", self._parse_decimal(normalized_number) or Decimal("0"))
            normalized = self._normalize_gift_card_amount(text)
            if normalized:
                return field_name, normalized
        return "", ""

    def _consume_header_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "ticket-name-prefix" not in classes:
            return None

        ref_line = lines[start + 1] if start + 1 < len(lines) else None
        date_line = lines[start + 2] if start + 2 < len(lines) else None
        served_line = lines[start + 3] if start + 3 < len(lines) else None
        barcode_line = lines[start + 4] if start + 4 < len(lines) else None
        if not all(isinstance(item, dict) for item in [ref_line, date_line, served_line, barcode_line]):
            return None
        if str(barcode_line.get("type") or "") != "image" or str(barcode_line.get("image_kind") or "") != "barcode":
            return None

        ref_text = str(ref_line.get("text") or "").strip()
        date_text = str(date_line.get("text") or "").strip()
        served_text = str(served_line.get("text") or "").strip()
        ticket_text = str(line.get("text") or "").strip()
        if not ref_text or not date_text or not served_text:
            return None

        return (
            [
                {**line, "align": "center"},
                {**ref_line, "align": "center"},
                {**barcode_line, "align": "center"},
                {
                    "type": "header_meta_line",
                    "left_text": date_text,
                    "right_text": served_text,
                },
            ],
            start + 5,
        )

    def _consume_header_meta_pair(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        date_line = lines[start]
        served_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(date_line, dict) or not isinstance(served_line, dict):
            return None

        date_text = str(date_line.get("text") or "").strip()
        served_text = str(served_line.get("text") or "").strip()
        if not self._looks_like_header_date_line(date_text):
            return None
        if not self._parse_served_by_line(served_text):
            return None

        return (
            [
                {
                    "type": "header_meta_line",
                    "left_text": date_text,
                    "right_text": served_text,
                }
            ],
            start + 2,
        )

    def _consume_service_info_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "pos-receipt-contact" not in classes:
            return None

        text = str(line.get("text") or "").strip()
        served_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(served_line, dict):
            return None
        served_text = str(served_line.get("text") or "").strip()

        table_text, table_value, guests_value = self._parse_service_contact_line(text)
        served_value = self._parse_served_by_line(served_text)
        if not table_text or not table_value or not guests_value or not served_value:
            return None

        return (
            {
                "type": "service_info_block",
                "table_text": table_text,
                "guests_text": f"Guests: {guests_value}",
                "served_by_text": f"Served by: {served_value}",
            },
            start + 2,
        )

    def _consume_customer_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") == "image":
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text or classes or line.get("align") != "left":
            return None
        if self._is_separator_line(text):
            return None
        if self._is_orphan_weight_fragment(text):
            return None
        if self._looks_like_amount(text) or self._looks_like_tax_summary_line(text) or text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "pos:", "time:")):
            return None

        collected: list[dict[str, Any]] = []
        index = start
        while index < len(lines):
            candidate = lines[index]
            if not isinstance(candidate, dict):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if (
                not candidate_text
                or candidate_classes
                or candidate.get("align") != "left"
                or candidate.get("type") == "image"
                or self._is_separator_line(candidate_text)
                or self._is_orphan_weight_fragment(candidate_text)
                or self._looks_like_amount(candidate_text)
                or self._looks_like_tax_summary_line(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:"))
                or candidate_text.isdigit()
            ):
                break
            collected.append({**candidate, "align": "center"})
            index += 1

        if not collected:
            return None

        merged: list[dict[str, Any]] = [
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
            *collected,
            {"text": "", "align": "left", "classes": ["customer-spacer"]},
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
        ]
        return merged, index

    def _consume_centered_customer_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") == "image":
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text or classes or line.get("align") != "center":
            return None
        if self._is_separator_line(text):
            return None
        if self._looks_like_amount(text) or self._looks_like_tax_summary_line(text) or text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:")):
            return None

        if start <= 0:
            return None

        previous = lines[start - 1]
        if not isinstance(previous, dict):
            return None

        previous_text = str(previous.get("text") or "").strip().lower()
        previous_classes = (
            [str(cls) for cls in previous.get("classes") or []]
            if isinstance(previous.get("classes"), list)
            else []
        )
        if (
            "qty" in previous_classes
            or "product-price" in previous_classes
            or self._looks_like_amount(previous_text)
            or not previous_text.startswith("served by:")
        ):
            return None

        collected: list[dict[str, Any]] = []
        index = start
        while index < len(lines):
            candidate = lines[index]
            if not isinstance(candidate, dict):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if (
                not candidate_text
                or candidate_classes
                or candidate.get("type") == "image"
                or candidate.get("align") != "center"
                or self._is_separator_line(candidate_text)
                or self._looks_like_amount(candidate_text)
                or self._looks_like_tax_summary_line(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:"))
            ):
                break
            collected.append(candidate)
            index += 1

        if not collected:
            return None

        merged: list[dict[str, Any]] = [
            *collected,
            {"text": "", "align": "left", "classes": ["customer-spacer"]},
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
        ]
        return merged, index

    def _consume_change_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if "receipt-change" not in classes or not text:
            return None
        amount = self._extract_signed_amount(text)
        label = text
        if amount:
            label = text[: text.rfind(amount)].strip().rstrip("-").strip()
        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip().lower()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if candidate_text.startswith("change") or "label-change" in candidate_classes or "pos-receipt-right-align" in candidate_classes:
                next_index = candidate_index + 1
        return (
            {
                "text": f"{label}  {amount}".strip(),
                "align": "left",
                "classes": ["merged-label-amount", "label-change"],
            },
            next_index,
        )

    def _consume_tracking_number_line(self, lines: list[dict[str, Any]], start: int) -> dict[str, Any] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "tracking-number" not in classes:
            return None
        return {**line, "align": "center"}

    def _consume_payment_terminal_receipt(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if not {"payment-terminal-line", "pos-payment-terminal-receipt"}.intersection(classes):
            return None

        text = str(line.get("text") or "").strip()
        if not text:
            return None

        next_index = start + 1
        merged_terminal_lines: list[str] = []
        split_lines = self._split_payment_terminal_receipt_text(text)
        if split_lines:
            merged_terminal_lines.extend(split_lines)

        while next_index < len(lines):
            candidate = lines[next_index]
            if not isinstance(candidate, dict):
                break
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if not {"payment-terminal-line", "pos-payment-terminal-receipt"}.intersection(candidate_classes):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            if candidate_text:
                merged_terminal_lines.extend(self._split_payment_terminal_receipt_text(candidate_text))
            next_index += 1

        if not merged_terminal_lines:
            return None

        deduped_terminal_lines: list[str] = []
        seen: set[str] = set()
        for item in merged_terminal_lines:
            normalized = re.sub(r"\s+", " ", str(item or "")).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped_terminal_lines.append(normalized)

        normalized_lines: list[dict[str, Any]] = []
        normalized_lines.extend([
            {
                **line,
                "text": item,
                "align": "center",
                "classes": [*classes, "payment-terminal-detail"],
            }
            for item in deduped_terminal_lines
        ])
        normalized_lines.append({"text": "", "align": "left", "classes": ["receipt-spacer", "payment-terminal-spacer"]})
        return normalized_lines, next_index

    def _consume_summary_amount_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if not text:
            return None
        if "merged-label-amount" in classes:
            return None

        candidate_text = text
        next_offset = 1
        if text.lower() == "el":
            next_line = lines[start + 1] if start + 1 < len(lines) else None
            base_line = lines[start + 2] if start + 2 < len(lines) else None
            amount_line = lines[start + 3] if start + 3 < len(lines) else None
            if not isinstance(next_line, dict) or not isinstance(base_line, dict) or not isinstance(amount_line, dict):
                return None
            next_text = str(next_line.get("text") or "").strip()
            base_text = str(base_line.get("text") or "").strip()
            amount_text = str(amount_line.get("text") or "").strip()
            amount_classes = (
                [str(cls) for cls in amount_line.get("classes") or []]
                if isinstance(amount_line.get("classes"), list)
                else []
            )
            if "impuesto" not in next_text.lower():
                return None
            if not self._extract_amount(base_text) or not self._extract_amount(amount_text):
                return None
            if "ms-auto" not in amount_classes and "pos-receipt-right-align" not in amount_classes:
                return None
            candidate_text = f"{next_text} {text} {base_text}".strip()
            next_offset = 3

        mergeable_labels = {"subtotal", "impuesto", "tax"}
        if not any(key in candidate_text.lower() for key in mergeable_labels):
            return None

        amount = self._extract_amount(candidate_text) or ""
        if amount:
            label_text = candidate_text
            amount_index = label_text.rfind(amount)
            if amount_index >= 0:
                label_text = label_text[:amount_index].strip()
            merged_line = {
                "text": f"{label_text}  {amount}".strip(),
                "align": "left",
                "classes": ["merged-label-amount", *classes],
            }
            return (merged_line, start + 1)
        consumed_offset = next_offset
        for offset in range(next_offset, min(next_offset + 3, len(lines) - start)):
            next_line = lines[start + offset]
            if not isinstance(next_line, dict):
                continue
            next_text = str(next_line.get("text") or "").strip()
            candidate_amount = self._extract_amount(next_text)
            if not candidate_amount:
                continue
            amount = candidate_amount
            consumed_offset = offset
            break
        if not amount:
            return None
        merged_line = {
            "text": f"{candidate_text}  {amount}",
            "align": "left",
            "classes": ["merged-label-amount", *classes],
        }
        if "subtotal" in candidate_text.lower():
            return (
                merged_line,
                start + consumed_offset + 1,
            )
        return (merged_line, start + consumed_offset + 1)

    def _build_total_emphasis_block(self, label: str, amount: str) -> list[dict[str, Any]]:
        clean_label = str(label or "").strip().rstrip(":")
        clean_amount = str(amount or "").strip()
        merged_text = f"{clean_label} {clean_amount}".strip() if clean_amount else clean_label
        return [
            {
                "text": "-" * self._escpos_line_width(),
                "align": "left",
                "classes": ["receipt-separator"],
            },
            {
                "text": merged_text,
                "align": "center",
                "bold": True,
                "double_width": True,
                "classes": ["receipt-total-emphasized"],
            },
        ]

    def _consume_invoice_section(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") != "image" or str(line.get("image_kind") or "") != "qr":
            return None

        next_index = start + 1
        url_line = None
        code_line = None
        trailing_lines: list[dict[str, Any]] = []
        for candidate_index in range(start + 1, min(len(lines), start + 5)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            text = str(candidate.get("text") or "").strip()
            classes = [str(cls) for cls in candidate.get("classes") or []] if isinstance(candidate.get("classes"), list) else []
            if {"payment-terminal-line", "pos-payment-terminal-receipt", "payment-terminal-logo", "payment-terminal-nfc-icon"}.intersection(classes):
                break
            if not text:
                continue
            if self._is_invoice_prompt_line(candidate):
                next_index = candidate_index + 1
                continue
            is_portal_url = "portal-url" in classes or text.startswith(("http://", "https://"))
            if url_line is None and is_portal_url:
                url_line = candidate
                next_index = candidate_index + 1
                continue
            is_unique_code = "unique-code" in classes or text.lower().startswith("code:")
            if code_line is None and is_unique_code:
                code_line = candidate
                next_index = candidate_index + 1
                continue
            if "pos-config-name" in classes:
                next_index = candidate_index + 1
                continue
            trailing_lines.append(candidate)

        merged: list[dict[str, Any]] = [{**line, "align": "center"}]
        if url_line:
            merged.append({**url_line, "align": "center"})
        if code_line:
            merged.append({**code_line, "align": "center"})
        merged.extend({**item, "align": "center"} for item in trailing_lines)
        return merged, next_index

    def _prepend_separator_for_reference(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "pos-receipt-vat" not in classes:
            return None
        # VAT rows already sit between subtotal/total blocks which add their own
        # separators. Prepending another one creates the repeated dashed lines
        # visible around the tax section.
        merged_classes = ["merged-label-amount", *classes] if self._extract_amount(str(line.get("text") or "").strip()) else classes
        return (
            [
                {
                    **line,
                    "align": "left",
                    "classes": merged_classes,
                }
            ],
            start + 1,
        )

    def _consume_label_amount_line(
        self, lines: list[dict[str, Any]], start: int, discount_total: Decimal
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text:
            return None
        mergeable = {"paymentlines", "label-discount"}
        if not mergeable.intersection(classes):
            return None

        if "label-discount" in classes:
            amount = self._format_amount_like("$ 0.00", discount_total)
            return (
                {
                    "text": f"{text} {amount}",
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                min(len(lines), start + 2),
            )

        inline_amount = self._extract_amount(text)
        if inline_amount:
            label_text = text
            amount_index = label_text.rfind(inline_amount)
            if amount_index >= 0:
                label_text = label_text[:amount_index].strip()
            return (
                {
                    "text": f"{label_text} {inline_amount}".strip(),
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                start + 1,
            )

        for offset in range(1, 4):
            index = start + offset
            if index >= len(lines):
                break
            candidate = lines[index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            amount = self._extract_amount(candidate_text)
            if not amount:
                continue
            if "pos-receipt-right-align" not in candidate_classes and offset > 1:
                continue
            return (
                {
                    "text": f"{text} {amount}",
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                index + 1,
            )
        return None

    def _product_discount_amount(self, line: dict[str, Any]) -> Decimal:
        original_total = self._parse_decimal(str(line.get("original_total") or ""))
        final_total = self._parse_decimal(str(line.get("total") or ""))
        if original_total is None or final_total is None:
            return Decimal("0")
        diff = original_total - final_total
        if diff <= 0:
            return Decimal("0")
        return diff

    def _skip_total_can_block(self, lines: list[dict[str, Any]], start: int) -> int | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip().replace(" ", "").lower()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "totalcan" not in text:
            return None
        if "receipt-total" not in classes and "label-total" not in classes:
            return None

        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip().replace(" ", "").lower()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if "totalcan" in candidate_text or "label-total" in candidate_classes or "pos-receipt-right-align" in candidate_classes:
                next_index = candidate_index + 1
        return next_index

    def _consume_emphasized_total(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if "receipt-total" not in classes or not text:
            return None

        label = ""
        amount = ""
        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_amount = self._extract_amount(candidate_text) if candidate_text else ""
            if "me-1" in candidate_classes and candidate_text:
                label = candidate_text
                next_index = candidate_index + 1
                continue
            if not amount and candidate_text:
                extracted_amount = candidate_amount
                if extracted_amount and self._looks_like_amount(self._extract_amount(candidate_text) or candidate_text):
                    amount = extracted_amount
            if (
                "me-1" in candidate_classes
                or "label-total" in candidate_classes
                or "pos-receipt-right-align" in candidate_classes
            ):
                next_index = candidate_index + 1
        if not amount:
            amount = self._extract_amount(text)
        if not label:
            if amount and amount in text:
                return (self._build_total_emphasis_block(text.strip(), ""), next_index)
            label = text
            if amount:
                compact_amount = amount.strip()
                amount_index = label.rfind(compact_amount)
                if amount_index >= 0:
                    label = label[:amount_index].strip()
        label = re.sub(r"[\s:$]+$", "", label).strip()
        return (self._build_total_emphasis_block(label, amount), next_index)

    def _skip_duplicate_summary(self, lines: list[dict[str, Any]], start: int) -> int | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "receipt-total" not in classes or not text:
            return None
        if "fs-1" not in classes and "label-total" not in classes:
            compact = text.replace(" ", "").lower()
            if compact.startswith("totalcan"):
                return start + 1

        combined_text = text.replace(" ", "")
        window = lines[start + 1 : start + 4]
        if not window:
            return None

        label_index = None
        amount_index = None
        label_text = ""
        amount_text = ""
        for offset, candidate in enumerate(window, start=1):
            if not isinstance(candidate, dict):
                continue
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            candidate_text = str(candidate.get("text") or "").strip()
            if not candidate_text:
                continue
            if label_index is None and ("label-total" in candidate_classes or "me-1" in candidate_classes):
                label_index = start + offset
                label_text = candidate_text
                continue
            if amount_index is None and (
                "pos-receipt-right-align" in candidate_classes or self._looks_like_amount(self._extract_amount(candidate_text))
            ):
                amount_index = start + offset
                amount_text = self._extract_amount(candidate_text) or candidate_text

        if label_index is None:
            return None

        combined_next = f"{label_text}{amount_text}".replace(" ", "")
        if combined_next and (
            combined_text == combined_next
            or combined_text.endswith(amount_text.replace(" ", ""))
            or combined_text.startswith(label_text.replace(" ", ""))
        ):
            return label_index

        return None
