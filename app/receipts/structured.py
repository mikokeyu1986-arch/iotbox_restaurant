from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlencode

from ..kitchen_template_store import apply_kitchen_template
from ..receipt_builder import (
    build_coupon_lines,
    build_loyalty_lines,
    build_promotion_lines,
)
from ..receipt_template_store import apply_template
from ..printing.common import perf_log as _perf_log

_logger = logging.getLogger(__name__)


def _split_receipt_label_amount(text: str) -> tuple[str, str] | None:
    """Split a receipt row into the template's left and right columns."""
    value = str(text or "").strip()
    match = re.match(
        r"^(.*?)(?:(?:[$€]|EUR)\s*)?[-+]?\s*\d[\d.,]*(?:\s*(?:[$€]|EUR))?$",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    left = match.group(1).strip()
    right = value[len(match.group(1)):].strip()
    return (left, right) if left and right else None

class StructuredReceiptMixin:
    def _build_structured_receipt_lines(self, receipt: dict[str, Any], template: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        loyalty_cards = [
            card for card in (receipt.get("loyalty_cards") or [])
            if isinstance(card, dict)
            and str(
                card.get("program_type")
                or ((card.get("program") or {}).get("program_type") if isinstance(card.get("program"), dict) else "")
            ).lower() in {"", "gift_card", "ewallet"}
        ]
        loyalty_card_names = {
            str(card.get("name") or "").strip().lower()
            for card in loyalty_cards
            if str(card.get("name") or "").strip()
        }
        _perf_log(
            "[IOT ESCPOS PAYLOAD] "
            + json.dumps(
                {
                    "summary_lines": receipt.get("summary_lines") or [],
                    "total_line": receipt.get("total_line") or "",
                    "payment_lines": receipt.get("payment_lines") or [],
                    "change_line": receipt.get("change_line") or "",
                    "discount_line": receipt.get("discount_line") or "",
                },
                ensure_ascii=False,
            )
        )

        logo = receipt.get("logo") if isinstance(receipt.get("logo"), dict) else None
        logo_src = str(logo.get("src") or "").strip() if logo else ""
        if logo_src:
            logo_src = self._with_logo_cache_buster(logo_src)
            lines.append(
                {
                    "type": "image",
                    "src": logo_src,
                    "align": "center",
                    "classes": ["pos-receipt-logo"],
                    "image_kind": "logo",
                }
            )

        company_section_added = False
        company_lines, inferred_reference = self._split_company_and_reference_lines(receipt.get("company_lines") or [])
        for text, is_bold in company_lines:
            company_section_added = True
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": is_bold,
                    "double_width": False,
                    "classes": ["company-info"],
                }
            )

        order_info_lines: list[dict[str, Any]] = []
        receipt_reference = ""
        reference_inputs = (inferred_reference, receipt.get("reference_text"))
        for value in reference_inputs:
            normalized = self._normalize_order_info_text(value)
            match = re.search(r"\b\d+(?:-\d+){2,}\b", normalized)
            if match:
                receipt_reference = match.group(0)
                break
        table_value = str(
            receipt.get("table")
            or receipt.get("table_name")
            or receipt.get("table_number")
            or receipt.get("table_display_name")
            or ((receipt.get("table_id") or {}).get("table_number") if isinstance(receipt.get("table_id"), dict) else "")
            or ""
        ).strip()
        if table_value and not table_value.upper().startswith(("MESA ", "TABLE ")):
            table_value = f"MESA {table_value}"
        tracking_value = (
            (
                receipt.get("tracking_number")
                or receipt.get("takeaway_number")
                or receipt.get("pickup_number")
                or receipt.get("order_number")
            )
            if receipt.get("is_restaurant")
            else ""
        )
        if tracking_value:
            tracking_value = str(tracking_value).strip()
            if not tracking_value.startswith("#"):
                tracking_value = f"# {tracking_value}"
        native_order_info = [value for value in (table_value, tracking_value, receipt.get("order_type")) if value]
        for value in (
            *native_order_info,
            *(() if receipt_reference else reference_inputs),
            receipt.get("date_text"),
            receipt.get("cashier_text"),
        ):
            text = self._normalize_order_info_text(value)
            if text:
                upper_text = text.upper()
                is_table_line = upper_text.startswith("MESA ") or upper_text.startswith("TABLE ")
                is_tracking_line = text.startswith("#")
                order_info_lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": is_table_line or is_tracking_line,
                        "double_width": is_table_line,
                        "double_height": is_table_line,
                        "classes": ["table-info"] if is_table_line else ["tracking-info"] if is_tracking_line else ["order-info"],
                    }
                )

        if company_section_added and order_info_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "company-order-spacer"],
                }
            )
        # The simplified-invoice heading belongs immediately above the pickup
        # number.  Keep the remaining order details in their natural place,
        # then append the pickup row after the invoice section below.
        tracking_order_info_lines = [
            line for line in order_info_lines
            if "tracking-info" in {str(value) for value in line.get("classes") or []}
        ]
        lines.extend(
            line for line in order_info_lines
            if "tracking-info" not in {str(value) for value in line.get("classes") or []}
        )

        # Detect whether this receipt carries a table (MESA/TABLE) marker.
        # Receipts without a table should stay compact: no extra blank lines
        # and no doubled separators around the barcode/product section.
        has_table = False
        for order_line in order_info_lines:
            upper = str(order_line.get("text") or "").upper()
            if "MESA" in upper or "TABLE" in upper:
                has_table = True
                break
        if not has_table:
            for header_text in receipt.get("header_lines") or []:
                upper = str(header_text or "").upper()
                if "MESA" in upper or "TABLE" in upper:
                    has_table = True
                    break

        # ── Simplified invoice info (Factura Simplificada) ─────────────
        factura_number = str(receipt.get("factura_simplificada_number") or "").strip()
        # Some Odoo POS payloads put the marker and the order reference in
        # reference_text (for example "Factura simplificada 262-1-000004")
        # but leave factura_simplificada_number empty.  It is still a
        # simplified invoice and must show its heading on the receipt.
        reference_text = " ".join(str(value or "") for value in reference_inputs)
        is_simplified_invoice = bool(factura_number) or bool(
            re.search(r"factura\s+simplificada", reference_text, flags=re.IGNORECASE)
        )
        if is_simplified_invoice:
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            lines.append(
                {
                    "text": "Factura Simplificada",
                    "align": "center",
                    "bold": True,
                    "double_width": False,
                    "double_height": False,
                    "classes": ["simplified-invoice-title"],
                }
            )
            if factura_number:
                lines.append(
                    {
                        "text": factura_number,
                        "align": "center",
                        "bold": False,
                        "double_width": False,
                        "double_height": False,
                        "classes": ["simplified-invoice-number"],
                    }
                )
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            if has_table:
                lines.append(
                    {
                        "type": "spacer",
                        "align": "left",
                        "classes": ["receipt-spacer", "company-order-spacer"],
                    }
                )

        lines.extend(tracking_order_info_lines)

        for item in receipt.get("company_lines") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # Remaining header lines that weren't promoted into the fixed
            # company/order sections stay out to avoid duplicated content.
            continue

        for text in receipt.get("header_lines") or []:
            text = str(text or "").strip()
            if text:
                lines.append({"text": text, "align": "center", "bold": False, "double_width": False, "classes": []})

        customer = receipt.get("customer") if isinstance(receipt.get("customer"), dict) else None
        if customer is None and isinstance(receipt.get("partner_id"), dict):
            customer = receipt.get("partner_id")
        if customer is None and isinstance(receipt.get("partner"), dict):
            customer = receipt.get("partner")
        if not customer:
            customer = self._load_customer_from_reference_text(receipt.get("reference_text"))
        customer_rows = [
            str(customer.get("name") or "").strip() if customer else "",
            str(customer.get("vat") or "").strip() if customer else "",
            str(customer.get("pos_contact_address") or customer.get("address") or customer.get("street") or "").strip() if customer else "",
            str(customer.get("street2") or "").strip() if customer else "",
            str(customer.get("region") or "").strip() if customer else "",
            str(customer.get("country") or "").strip() if customer else "",
            str(customer.get("phone") or "").strip() if customer else "",
            str(customer.get("mobile") or "").strip() if customer else "",
            str(customer.get("email") or "").strip() if customer else "",
        ]
        customer_rows = [text for text in customer_rows if text]
        if customer_rows:
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )
            for text in customer_rows:
                lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-info"],
                    }
                )
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )

        barcode = receipt.get("barcode") if isinstance(receipt.get("barcode"), dict) else None
        barcode_src = str(barcode.get("src") or "").strip() if barcode else ""
        if barcode_src:
            barcode_is_qr = self._is_qr_receipt_image_src(barcode_src, barcode)
            barcode_width = int(barcode.get("width") or (200 if barcode_is_qr else 260))
            barcode_height = int(barcode.get("height") or (barcode_width if barcode_is_qr else 58))
            barcode_classes = ["order-qr-img"] if barcode_is_qr else ["order-barcode-img"]
            if not barcode_is_qr:
                # The printed order reference belongs directly below its
                # Code128 image.  A separator here visually detaches the
                # human-readable number from the barcode it describes.
                barcode_classes.append("no-barcode-separator")
            lines.append(
                {
                    "type": "image",
                    "src": barcode_src,
                    "align": "center",
                    "classes": barcode_classes,
                    "width": barcode_width,
                    "height": barcode_height,
                    "image_kind": "qr" if barcode_is_qr else "barcode",
                }
            )
            if not barcode_is_qr:
                order_number = str(
                    receipt.get("pos_reference")
                    or receipt.get("order_reference")
                    or receipt.get("order_name")
                    or receipt.get("name")
                    or receipt_reference
                    or ""
                ).strip()
                if order_number:
                    lines.append(
                        {
                            "text": order_number,
                            "align": "center",
                            "bold": True,
                            "classes": ["order-barcode-reference"],
                        }
                    )

        receipt_items = [item for item in (receipt.get("items") or []) if isinstance(item, dict)]
        for item_index, item in enumerate(receipt_items):
            qty = str(item.get("qty") or "").strip()
            raw_name = str(item.get("name") or "").strip()
            total = str(item.get("total") or "").strip()
            if not qty or not raw_name or not total:
                continue
            name = raw_name
            if loyalty_card_names and name.strip().lower() in loyalty_card_names:
                continue
            raw_combo_items = (
                item.get("combo_items")
                or item.get("attribute_details")
                or item.get("attributes")
                or item.get("attribute_lines")
                or []
            )
            if isinstance(raw_combo_items, dict):
                raw_combo_items = [raw_combo_items]
            elif not isinstance(raw_combo_items, (list, tuple)):
                raw_combo_items = [raw_combo_items]
            combo_items = [combo for combo in raw_combo_items if combo not in (None, "")]
            # Odoo POS renders attributes as "Product (attr1, attr2)" inside the
            # display name.  Always remove that suffix when attributes are
            # available separately, otherwise the names print twice.
            name_match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", raw_name)
            if name_match:
                extracted_attrs = [s.strip() for s in name_match.group(2).split(",") if s.strip()]
                if extracted_attrs:
                    name = name_match.group(1).strip()
                    if not combo_items:
                        combo_items = extracted_attrs
            # NOTE: the product column header ("Uds. Producto Importe") and its
            # leading separator are intentionally NOT generated here. They are
            # printed once by _build_escpos_bytes() when it encounters the first
            # product_line, using _build_product_header_text() (fully aligned).
            # Generating them here would produce a flattened duplicate header.
            lines.append(
                {
                    "type": "product_line",
                    "qty": qty,
                    "name": name,
                    "unit_price": str(item.get("unit_price") or "").strip(),
                    "total": total,
                    "combo_items": combo_items,
                    "discount_text": str(item.get("discount_text") or "").strip(),
                    "original_total": str(item.get("original_total") or "").strip(),
                }
            )
            customer_note = str(item.get("customer_note") or "").strip()
            if customer_note:
                lines.append(
                    {
                        "text": customer_note,
                        "align": "left",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-note"],
                    }
                )
        summary_lines = [str(text or "").strip() for text in receipt.get("summary_lines") or [] if str(text or "").strip()]
        if summary_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "summary-line-spacer"],
                }
            )
        for index, text in enumerate(summary_lines):
            if text:
                next_text = summary_lines[index + 1].lower() if index + 1 < len(summary_lines) else ""
                is_subtotal_line = text.lower().startswith("subtotal")
                next_is_tax_line = self._looks_like_tax_summary_line(next_text)
                columns = _split_receipt_label_amount(text)
                if columns:
                    lines.append({
                        "type": "header_meta_line",
                        "left_text": columns[0],
                        "right_text": columns[1],
                        "align": "left",
                        "bold": False,
                        "double_width": False,
                        "classes": [],
                    })
                else:
                    lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": []})
                is_last_summary_line = index == len(summary_lines) - 1
                if (
                    not self._looks_like_tax_summary_line(text)
                    and not (is_subtotal_line and next_is_tax_line)
                    and not is_last_summary_line
                ):
                    lines.append(
                        {
                            "type": "spacer",
                            "align": "left",
                            "classes": ["receipt-spacer", "summary-line-spacer"],
                        }
                    )

        total_line = str(receipt.get("total_line") or "").strip()
        if total_line:
            columns = _split_receipt_label_amount(total_line)
            if columns:
                lines.append(
                    {
                        "type": "header_meta_line",
                        "left_text": columns[0].rstrip(":"),
                        "right_text": columns[1],
                        "align": "left",
                        "bold": True,
                        "double_width": False,
                        "double_height": True,
                        "classes": ["receipt-total"],
                    }
                )
            else:
                lines.append(
                    {
                        "text": total_line,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["receipt-total"],
                    }
                )

        # Do not insert a blank row between the payment method and Cambio.
        change_text = str(receipt.get("change_line") or "").strip()
        for item in receipt.get("payment_lines") or []:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                name = str(item.get("name") or item.get("method") or "").strip()
                amount = str(item.get("amount") or "").strip()
                if name and amount:
                    text = f"{name} {amount}"
            else:
                text = str(item or "").strip()
            if text:
                if text.upper().startswith("METHOD:"):
                    continue
                columns = _split_receipt_label_amount(text)
                if columns:
                    lines.append({
                        "type": "header_meta_line",
                        "left_text": columns[0],
                        "right_text": columns[1],
                        "align": "left",
                        "bold": False,
                        "double_width": False,
                        "classes": ["paymentlines"],
                    })
                else:
                    lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": ["paymentlines"]})
                if not change_text:
                    lines.append(
                        {
                            "type": "spacer",
                            "align": "left",
                            "classes": ["receipt-spacer", "summary-line-spacer"],
                        }
                    )

        if change_text:
            columns = _split_receipt_label_amount(change_text)
            if columns:
                lines.append({
                    "type": "header_meta_line",
                    "left_text": "Cambio",
                    "right_text": columns[1],
                    "align": "left",
                    "bold": False,
                    "double_width": False,
                    "classes": ["receipt-change"],
                })
            else:
                lines.append({"text": "Cambio", "align": "left", "bold": False, "double_width": False, "classes": ["receipt-change"]})

        discount_text = str(receipt.get("discount_line") or "").strip()
        if discount_text:
            lines.append({"text": discount_text, "align": "left", "bold": False, "double_width": False, "classes": []})

        for receipt_item in receipt.get("payment_terminal_receipts") or []:
            if not isinstance(receipt_item, dict):
                continue
            terminal_receipt_lines = self._iter_payment_terminal_receipt_lines(receipt_item)
            logo = receipt_item.get("logo") if isinstance(receipt_item.get("logo"), dict) else {}
            terminal_logo_src = str(
                receipt_item.get("nfc_logo_src")
                or receipt_item.get("nfcLogoSrc")
                or receipt_item.get("contactless_logo_src")
                or receipt_item.get("contactlessLogoSrc")
                or logo.get("src")
                or ""
            ).strip()
            is_contactless = bool(
                receipt_item.get("contactless")
                or receipt_item.get("is_contactless")
                or receipt_item.get("nfc")
                or any("CONTACTLESS" in text.upper() or "NFC" in text.upper() for text in terminal_receipt_lines)
            )
            if not terminal_logo_src and is_contactless:
                terminal_logo_src = "/assets/nfc_override.png"
            if terminal_logo_src:
                lines.append(
                    {
                        "type": "image",
                        "src": terminal_logo_src,
                        "align": "center",
                        "classes": ["payment-terminal-logo", "payment-terminal-nfc-icon", "redsys-nfc-logo"],
                        "width": 80,
                        "image_kind": "logo",
                    }
                )
            for text in terminal_receipt_lines:
                text = self._normalize_payment_terminal_line(text)
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": False,
                            "double_width": False,
                            "classes": ["payment-terminal-line", "pos-payment-terminal-receipt"],
                        }
                    )

        portal = receipt.get("portal") if isinstance(receipt.get("portal"), dict) else None
        if portal and portal.get("show") and (
            portal.get("show")
            or portal.get("qrSrc")
            or portal.get("qr_src")
            or portal.get("qr")
            or portal.get("src")
            or portal.get("url")
            or portal.get("code")
            or portal.get("title")
        ):
            qr_src = str(
                portal.get("qrSrc")
                or portal.get("qr_src")
                or portal.get("qr")
                or portal.get("src")
                or ""
            ).strip()
            if qr_src:
                qr_size = max(180, int(os.getenv("IOT_PORTAL_QR_SIZE", "200") or "200"))
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["m-0", "portal-qr"],
                        "width": qr_size,
                        "height": qr_size,
                        "image_kind": "qr",
                    }
                )
            for value, bold in (
                (portal.get("title"), True),
                (portal.get("url"), False),
                (portal.get("code"), False),
            ):
                text = str(value or "").strip()
                if text:
                    classes: list[str] = []
                    if value == portal.get("url"):
                        classes.append("portal-url")
                    elif value == portal.get("code"):
                        classes.append("unique-code")
                    elif value == portal.get("title"):
                        # Keep the invoice prompt with its QR/url/code group.
                        # Without this marker it falls through to totals/footer
                        # based on its position in the raw receipt payload.
                        classes.append("portal-title")
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )

        lines.extend(build_promotion_lines(receipt))
        lines.extend(build_coupon_lines(receipt))
        lines.extend(build_loyalty_lines(receipt))

        for loyalty_card in loyalty_cards:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "gift-card-spacer"],
                }
            )
            loyalty_card_code = str(loyalty_card.get("code") or "").strip()
            for value, bold, classes in (
                (loyalty_card.get("name"), True, ["gift-card-title"]),
                (loyalty_card_code, False, ["gift-card-code"]),
            ):
                text = str(value or "").strip()
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )
            gift_card_barcode_src = ""
            if loyalty_card_code:
                encoded_query = urlencode(
                    {
                        "barcode_type": "Code128",
                        "value": loyalty_card_code,
                        "width": 360,
                        "height": 80,
                    }
                )
                gift_card_barcode_src = f"/report/barcode?{encoded_query}"
            qr_src = str(loyalty_card.get("qrSrc") or "").strip()
            if gift_card_barcode_src:
                lines.append(
                    {
                        "type": "image",
                        "src": gift_card_barcode_src,
                        "align": "center",
                        "classes": ["gift-card-barcode"],
                        "width": 360,
                        "height": 80,
                        "image_kind": "barcode",
                    }
                )
            elif qr_src:
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["gift-card-qr"],
                        "width": 125,
                        "height": 125,
                        "image_kind": "qr",
                    }
                )
            amount_field, amount_text = self._gift_card_display_amount(loyalty_card)
            _logger.info(
                "Gift card payload code=%s amount_field=%s display_amount=%s payload=%s",
                loyalty_card_code or "<missing>",
                amount_field or "<none>",
                amount_text or "<empty>",
                json.dumps(loyalty_card, ensure_ascii=False, default=str),
            )
            if amount_text:
                lines.append(
                    {
                        "text": amount_text,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["gift-card-amount"],
                    }
                )
        # Footer lines: skip any line that duplicates the company name shown at
        # the top (e.g. a trailing "My Company") to avoid repetition.
        company_names = {
            str(item.get("text") or "").strip()
            for item in (receipt.get("company_lines") or [])
            if isinstance(item, dict)
            and item.get("bold")
            and str(item.get("text") or "").strip()
        }
        for text in receipt.get("footer_lines") or []:
            text = str(text or "").strip()
            if not text or text in company_names:
                continue
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": False,
                    "double_width": False,
                    "classes": ["pos-config-name"],
                }
            )

        return self._apply_visual_template_to_structured_lines(lines, template=template)

    def _apply_visual_template_to_structured_lines(
        self, lines: list[dict[str, Any]], template: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Tag native Odoo structured lines and apply the saved receipt template."""
        first_product = next(
            (
                index for index, line in enumerate(lines)
                if line.get("type") == "product_line"
                or "product-name" in {str(value) for value in line.get("classes") or []}
            ),
            len(lines),
        )
        first_payment = next(
            (
                index for index, line in enumerate(lines)
                if {"paymentlines", "payment-terminal-line", "pos-payment-terminal-receipt"}
                .intersection({str(value) for value in line.get("classes") or []})
            ),
            len(lines),
        )
        tagged: list[dict[str, Any]] = []
        product_header_added = False
        for index, source in enumerate(lines):
            line = dict(source)
            classes = {str(value) for value in line.get("classes") or []}
            text = str(line.get("text") or "").strip()
            if line.get("type") == "product_line" and not product_header_added:
                tagged.append({
                    "type": "product_header",
                    "bold": True,
                    "_template_block": "product_header",
                })
                product_header_added = True

            if (
                "portal-qr" in classes
                or "portal-url" in classes
                or "unique-code" in classes
                or "portal-title" in classes
                or (line.get("type") == "image" and str(line.get("image_kind") or "").lower() == "qr")
            ):
                block = "portal_prompt" if "portal-title" in classes else "qr"
            elif "pos-receipt-logo" in classes:
                block = "logo"
            elif "company-info" in classes:
                block = "company"
            elif classes.intersection({"customer-info", "customer-info-separator"}):
                block = "customer"
            elif any(value.startswith("simplified-invoice") or value == "invoice-asterisk-border" for value in classes):
                block = "invoice"
            elif "tracking-info" in classes:
                block = "tracking"
            elif "table-info" in classes:
                block = "table"
            elif classes.intersection({"order-info", "company-order-spacer", "order-barcode-img", "order-qr-img", "order-barcode-reference"}):
                block = "order_info"
            elif line.get("type") == "product_line" or classes.intersection({"customer-note", "product-line-spacer"}):
                block = "products"
            elif "pos-config-name" in classes:
                # Footer must be classified before the positional totals
                # fallback; otherwise a receipt without payment rows makes
                # footer lines look like totals.
                block = "footer"
            elif "receipt-total" in classes or (first_product < index < first_payment):
                block = "totals"
            elif classes.intersection({"payment-terminal-line", "pos-payment-terminal-receipt", "payment-terminal-logo"}) \
                    or any(value.startswith("redsys-") for value in classes):
                block = "redsys"
            elif "paymentlines" in classes:
                block = "payments"
            elif any(
                value.startswith(("promotion-", "reward-", "buy-x-get-y-"))
                for value in classes
            ):
                block = "promotions"
            elif any(
                value.startswith(("coupon-", "promo-code-", "next-order-coupon-"))
                for value in classes
            ):
                block = "coupons"
            elif any(value.startswith(("gift-card-", "ewallet-")) for value in classes):
                block = "vouchers"
            elif "loyalty" in classes or any(value.startswith("loyalty-") for value in classes):
                block = "loyalty"
            elif any(value.startswith("portal-") for value in classes):
                block = "qr"
            elif text.upper().startswith(("MESA ", "TABLE ")):
                block = "table"
            elif index < first_product:
                block = "order_info"
            elif index >= first_payment:
                block = "payments"
            else:
                block = "totals"
            line["_template_block"] = block
            tagged.append(line)
        def is_separator(candidate: dict[str, Any] | None) -> bool:
            text = str((candidate or {}).get("text") or "").strip()
            return bool(text) and set(text) <= {"-", "="}

        # Odoo/native renderers may surround TOTAL with fixed separators.
        # Remove only those adjacent separators; users can place separator
        # blocks anywhere they want in the visual template.
        without_total_separators: list[dict[str, Any]] = []
        for index, line in enumerate(tagged):
            previous = tagged[index - 1] if index > 0 else None
            following = tagged[index + 1] if index + 1 < len(tagged) else None
            previous_classes = {str(value) for value in (previous or {}).get("classes") or []}
            following_classes = {str(value) for value in (following or {}).get("classes") or []}
            if is_separator(line) and (
                "receipt-total" in previous_classes or "receipt-total" in following_classes
            ):
                continue
            without_total_separators.append(line)
        return apply_template(without_total_separators, template=template)

    def _apply_visual_template_to_kitchen_lines(
        self, lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Adapt legacy native kitchen lines to the saved kitchen template."""
        first_product = next(
            (index for index, line in enumerate(lines) if line.get("type") == "product_line"),
            len(lines),
        )
        last_product = max(
            (
                index for index, line in enumerate(lines)
                if line.get("type") == "product_line"
                or "product-name" in {str(value) for value in line.get("classes") or []}
                or "kitchen-note" in {str(value) for value in line.get("classes") or []}
            ),
            default=-1,
        )
        tagged: list[dict[str, Any]] = []
        order_type_assigned = False
        legacy_location = ""
        legacy_time = ""
        for index, source in enumerate(lines):
            line = dict(source)
            classes = {str(value) for value in line.get("classes") or []}
            text = str(line.get("text") or "").strip()
            if line.get("type") == "header_meta_line":
                if "kitchen-location-time" in classes:
                    line["_template_block"] = "location"
                    tagged.append(line)
                    continue
                left = str(line.get("left_text") or "").strip()
                right = str(line.get("right_text") or "").strip()
                if left:
                    tagged.append({
                        "text": left,
                        "align": "center",
                        "bold": bool(line.get("bold", True)),
                        "double_width": bool(line.get("double_width", False)),
                        "double_height": bool(line.get("double_height", True)),
                        "classes": ["kitchen-tracking-number"],
                        "_template_block": "tracking",
                    })
                if right:
                    if right.upper() in {"NUEVO", "CANCELA", "CAMBIO", "CANCELADO"}:
                        tagged.append({
                            "text": right,
                            "align": "center",
                            "bold": bool(line.get("bold", True)),
                            "classes": ["kitchen-status"],
                            "_template_block": "status",
                        })
                    else:
                        table_value = re.sub(r"^(?:MESA|TABLE)\s*", "", right, flags=re.IGNORECASE).strip()
                    if right.upper() not in {"NUEVO", "CANCELA", "CAMBIO", "CANCELADO"} and table_value:
                        tagged.append({
                            "text": table_value,
                            "align": "center",
                            "bold": bool(line.get("bold", True)),
                            "double_width": bool(line.get("double_width", False)),
                            "double_height": bool(line.get("double_height", True)),
                            "classes": ["kitchen-table-number"],
                            "_template_block": "order_meta",
                        })
                continue
            if line.get("type") == "product_line" or "kitchen-note" in classes or "product-name" in classes:
                block = "products"
                line["classes"] = list(classes | {"kitchen-product-line"})
            elif text and set(text) <= {"-", "="}:
                block = "separator_before" if index < first_product else "separator_after"
            elif "kitchen-footer" in classes:
                if re.fullmatch(r"\d{1,2}:\d{2}", text):
                    legacy_time = text
                else:
                    legacy_location = text
                continue
            elif text.startswith("#"):
                block = "tracking"
            elif "MESA" in text.upper() or "TABLE" in text.upper():
                table_value = re.sub(r"^(?:MESA|TABLE)\s*", "", text, flags=re.IGNORECASE).strip()
                if not table_value:
                    continue
                line["text"] = table_value
                line["classes"] = list(classes | {"kitchen-table-number"})
                block = "order_meta"
            elif index < first_product and not order_type_assigned and text:
                block = "order_type"
                order_type_assigned = True
            elif index < first_product:
                block = "status"
            elif index > last_product:
                block = "location"
            else:
                block = "products"
            line["_template_block"] = block
            tagged.append(line)
        if legacy_location or legacy_time:
            tagged.append({
                "type": "header_meta_line",
                "left_text": legacy_location,
                "right_text": legacy_time,
                "classes": ["kitchen-footer", "kitchen-location-time"],
                "_template_block": "location",
            })
        return apply_kitchen_template(tagged)

    def _split_receipt_product_name_and_options(self, raw_name: str) -> tuple[str, list[str]]:
        text = str(raw_name or "").strip()
        if not text:
            return "", []
        match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", text)
        if not match:
            return text, []
        base_name = match.group(1).strip() or text
        options_text = match.group(2).strip()
        if not options_text:
            return base_name, []
        options = [part.strip() for part in options_text.split(",") if part.strip()]
        return base_name, options
