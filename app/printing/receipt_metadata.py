from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any


class ReceiptMetadataMixin:
    def _split_payment_terminal_receipt_text(self, text: str) -> list[str]:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return []

        labels = [
            "Auth Code",
            "Card",
            "Comercio",
            "ETIQUETAAPP",
            "Factura",
            "Method",
            "Pedido",
            "RTS",
            "Terminal",
        ]
        label_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        matches = list(re.finditer(rf"(?P<label>{label_pattern})\s*:", compact))
        if not matches:
            return [compact]

        entries: list[str] = []
        seen: set[str] = set()
        trailing_message = ""

        for index, match in enumerate(matches):
            label = match.group("label").strip()
            value_start = match.end()
            value_end = matches[index + 1].start() if index + 1 < len(matches) else len(compact)
            value = compact[value_start:value_end].strip()
            for trailing_marker in (
                "OPERACION CONTACTLESS. FIRMA NO NECESARIA.",
                "OPERACION CON PIN. FIRMA NO NECESARIA.",
                "OPERACION AUT MOVIL FIRMA NO NECESARIA",
                "AUTORIZADA",
            ):
                marker_index = value.upper().find(trailing_marker)
                if marker_index > 0:
                    value = value[:marker_index].strip()
                    break
            if not value:
                continue

            entry = re.sub(r"\s+", " ", f"{label}: {value}").strip()
            if entry and entry not in seen:
                seen.add(entry)
                entries.append(entry)

        upper_compact = compact.upper()
        if "OPERACION CONTACTLESS. FIRMA NO NECESARIA." in upper_compact:
            trailing_message = "OPERACION CONTACTLESS. FIRMA NO NECESARIA."
        elif "OPERACION CON PIN. FIRMA NO NECESARIA." in upper_compact:
            trailing_message = "OPERACION CON PIN. FIRMA NO NECESARIA."
        elif "OPERACION AUT MOVIL FIRMA NO NECESARIA" in upper_compact:
            trailing_message = "OPERACION AUT MOVIL FIRMA NO NECESARIA"
        elif "AUTORIZADA" in upper_compact:
            trailing_message = "AUTORIZADA"
        else:
            auto_mobile_match = re.search(
                r"OPERACION\s+AUT\s*MOVIL[.\s-]*FIRMA\s+NO\s+NECESARIA\.?",
                upper_compact,
            )
            if auto_mobile_match:
                trailing_message = re.sub(r"\s+", " ", auto_mobile_match.group(0)).strip()

        if trailing_message:
            normalized_message = re.sub(r"\s+", " ", trailing_message).strip()
            if normalized_message and normalized_message not in seen:
                seen.add(normalized_message)
                entries.append(normalized_message)

        return entries or [compact]

    def _iter_payment_terminal_receipt_lines(self, receipt_item: dict[str, Any]) -> list[str]:
        collected: list[str] = []
        for value in receipt_item.get("lines") or []:
            text = str(value or "").strip()
            if text:
                collected.append(text)

        etiqueta_lines = self._extract_etiquetaapp_lines(receipt_item)
        if etiqueta_lines:
            insert_index = len(collected)
            for index, text in enumerate(collected):
                upper_text = text.upper()
                if "OPERACION CONTACTLESS" in upper_text or "OPERACION CON PIN" in upper_text or "OPERACION AUT MOVIL" in upper_text or "OPERACION AUT" in upper_text and "MOVIL" in upper_text or upper_text == "AUTORIZADA":
                    insert_index = index
                    break
            collected[insert_index:insert_index] = etiqueta_lines

        unique_lines: list[str] = []
        seen: set[str] = set()
        for text in collected:
            normalized = re.sub(r"\s+", " ", text).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_lines.append(normalized)
        return unique_lines

    def _normalize_payment_terminal_line(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        return compact

    def _is_payment_terminal_nfc_src(self, src: str) -> bool:
        normalized = str(src or "").strip().lower()
        return "nfc" in normalized and normalized.endswith((".png", ".svg", ".jpg", ".jpeg", ".webp"))

    def _extract_etiquetaapp_lines(self, payload: Any) -> list[str]:
        values: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized_key = re.sub(r"[^A-Z0-9]", "", str(key or "").upper())
                    if "ETIQUETAAPP" == normalized_key or "ETIQUETAAPP" in normalized_key:
                        self._append_etiquetaapp_value(values, value)
                    else:
                        walk(value)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        unique_lines: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                continue
            formatted = f"ETIQUETAAPP: {text}"
            if formatted in seen:
                continue
            seen.add(formatted)
            unique_lines.append(formatted)
        return unique_lines

    def _append_etiquetaapp_value(self, values: list[str], value: Any) -> None:
        if isinstance(value, dict):
            for nested_value in value.values():
                self._append_etiquetaapp_value(values, nested_value)
            return
        if isinstance(value, list):
            for item in value:
                self._append_etiquetaapp_value(values, item)
            return
        text = str(value or "").strip()
        if text:
            values.append(text)

    def _split_payment_terminal_message(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text).strip(" .")
        if not cleaned:
            return []
        chunks = [chunk.strip(" .") for chunk in re.split(r"(?<=[.!?])\s+", cleaned) if chunk.strip(" .")]
        return chunks or [cleaned]

    def _split_receipt_columns(self, text: str) -> list[str]:
        if " x " in text and self._looks_like_amount(text.rsplit(" ", 1)[-1]):
            return [chunk for chunk in re.split(r"\s{2,}|(?<=\S)\s(?=\d+[xX])", text) if chunk]
        return [chunk for chunk in re.split(r"\s{2,}", text) if chunk] or text.split(" ")

    def _split_label_amount_text(self, text: str) -> tuple[str, str]:
        compact = str(text or "").strip()
        if not compact:
            return "", ""
        match = re.search(
            r"(?P<amount>(?:[$€]\s*)?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?)$",
            compact,
        )
        if match:
            amount = match.group("amount").strip()
            label = compact[: match.start()].strip()
            if label:
                return label, amount
        return "", ""

    def _render_service_columns(self, columns: list[dict[str, str]], width: int) -> list[str]:
        if len(columns) != 3:
            return []

        separator = " "
        available_width = max(18, width - (len(columns) - 1) * len(separator))
        base_width = available_width // len(columns)
        column_widths = [base_width] * len(columns)
        column_widths[-1] += available_width - sum(column_widths)

        wrapped_columns: list[list[str]] = []
        for index, column in enumerate(columns):
            label = str(column.get("label") or "").strip()
            value = str(column.get("value") or "").strip()
            if label and value:
                text = f"{label}: {value}"
            else:
                text = value or label
            wrapped_columns.append(self._wrap_text(text, column_widths[index]) or [""])

        row_count = max(len(rows) for rows in wrapped_columns)
        rendered_rows: list[str] = []
        for row_index in range(row_count):
            parts: list[str] = []
            for column_index, rows in enumerate(wrapped_columns):
                cell = rows[row_index] if row_index < len(rows) else ""
                parts.append(self._pad_right(cell, column_widths[column_index]))
            rendered_rows.append(separator.join(parts).rstrip())
        return rendered_rows

    def _split_company_and_reference_lines(self, raw_lines: list[Any]) -> tuple[list[tuple[str, bool]], str]:
        company_lines: list[tuple[str, bool]] = []
        inferred_reference = ""
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            bold = bool(item.get("bold"))
            compact = re.sub(r"\s+", " ", text)
            ticket_match = re.search(r"(?i)\b(ticket\b.*)$", compact)
            if ticket_match:
                before = compact[: ticket_match.start()].strip(" -,:")
                after = ticket_match.group(1).strip()
                if before:
                    company_lines.append((before, bold))
                if after and not inferred_reference:
                    inferred_reference = after
                continue
            company_lines.append((compact, bold))
        return company_lines, inferred_reference

    def _normalize_order_info_text(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        compact = re.sub(r"\s+", " ", text)
        lowered = compact.lower()
        if compact.startswith("#"):
            return compact
        if lowered.startswith("ticket"):
            return compact
        if lowered.startswith("table "):
            table_value = compact[6:].strip()
            return f"MESA {table_value}".strip().upper()
        if lowered.startswith("mesa "):
            return compact.upper()
        if self._looks_like_header_date_line(compact):
            return f"Fecha: {compact}"
        if lowered.startswith("servido por:"):
            return compact
        if lowered.startswith("served by:"):
            return compact
        if lowered.startswith("nif "):
            return compact
        return f"Ticket: {compact}"

    def _load_customer_from_reference_text(self, value: Any) -> dict[str, str] | None:
        reference_text = str(value or "").strip()
        if not reference_text:
            return None
        match = re.search(r"(\d+-\d+-\d+)", reference_text)
        if not match:
            return None
        pos_reference = match.group(1)

        # Locate the optional dev project root used to look up the customer in
        # a local Odoo database. The hard-coded depth (parents[5]) only matches
        # the original developer checkout; guard it so a shallower deployment
        # (e.g. d:\\odoo\\iot_box_comercia) does not crash the preview/print.
        try:
            root_dir = Path(__file__).resolve().parents[5]
        except IndexError:
            return None
        query_python = root_dir / ".venv" / "Scripts" / "python.exe"
        config_path = root_dir / "instances" / "dev" / "config" / "odoo.conf"
        if not query_python.exists() or not config_path.exists():
            return None

        query_script = """
import json
import psycopg2
import sys
conn = psycopg2.connect(host='localhost', port=5432, dbname='odoo19_dev', user='odoo', password='odoo')
cur = conn.cursor()
cur.execute(
    '''
    SELECT rp.name, rp.vat, rp.street, rp.street2, rp.city, rp.zip,
           rp.phone, rp.mobile, rp.email, rc.name
    FROM pos_order po
    LEFT JOIN res_partner rp ON rp.id = po.partner_id
    LEFT JOIN res_country rc ON rc.id = rp.country_id
    WHERE po.pos_reference = %s
    ORDER BY po.id DESC
    LIMIT 1
    ''',
    (sys.argv[1],),
)
row = cur.fetchone()
cur.close()
conn.close()
if not row:
    print('{}')
else:
    name, vat, street, street2, city, zip_code, phone, mobile, email, country = row
    region = ', '.join([item for item in [city, zip_code] if item])
    print(json.dumps({
        'name': name or '',
        'vat': vat or '',
        'address': street or '',
        'street2': street2 or '',
        'region': region,
        'country': country or '',
        'phone': phone or '',
        'mobile': mobile or '',
        'email': email or '',
    }))
"""
        try:
            result = subprocess.run(
                [str(query_python), "-c", query_script, pos_reference],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        output = str(result.stdout or "").strip()
        if not output or output == "{}":
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "name": str(payload.get("name") or "").strip(),
            "vat": str(payload.get("vat") or "").strip(),
            "address": str(payload.get("address") or "").strip(),
            "region": str(payload.get("region") or "").strip(),
        }

    def _parse_service_contact_line(self, text: str) -> tuple[str, str, str]:
        compact = re.sub(r"\s+", " ", text).strip()
        match = re.search(
            r"(?i)table\s*(?P<table>[^,]+?)\s*,\s*guests?\s*:\s*(?P<guests>\d+)",
            compact,
        )
        if not match:
            return "", "", ""
        table_value = match.group("table").strip()
        guests_value = match.group("guests").strip()
        return f"Mesa {table_value}", table_value, guests_value

    def _parse_served_by_line(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        match = re.search(r"(?i)^served by\s*:\s*(?P<name>.+)$", compact)
        if not match:
            return ""
        return match.group("name").strip()

    def _looks_like_header_date_line(self, text: str) -> bool:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact or self._looks_like_amount(compact):
            return False
        has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b", compact))
        has_date = bool(re.search(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", compact))
        return has_time and has_date

    def _should_skip_ticket_prefix_line(self, line: dict[str, Any] | Any) -> bool:
        if not isinstance(line, dict):
            return False
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "ticket-name-prefix" not in classes:
            return False
        text = str(line.get("text") or "").strip().lower()
        return text in {"", "undefined", "none", "null"}

    def _should_skip_orphan_weight_fragment(
        self,
        lines: list[dict[str, Any]],
        start: int,
        normalized: list[dict[str, Any]],
    ) -> bool:
        line = lines[start]
        if not isinstance(line, dict):
            return False
        if line.get("type") == "image":
            return False

        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if classes or not text:
            return False

        compact = text.lower()
        if compact not in {"on", "kg", "g", "lb", "oz"}:
            return False

        previous = normalized[-1] if normalized else None
        if isinstance(previous, dict):
            prev_text = str(previous.get("text") or "").strip().lower()
            prev_type = str(previous.get("type") or "").strip().lower()
            if prev_type == "product_line" or prev_text.startswith(("subtotal", "tax", "total")):
                return True

        next_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(next_line, dict):
            return False
        next_text = str(next_line.get("text") or "").strip().lower()
        next_classes = (
            [str(cls) for cls in next_line.get("classes") or []]
            if isinstance(next_line.get("classes"), list)
            else []
        )
        return next_text.startswith(("subtotal", "tax", "total", "change")) or "ms-auto" in next_classes

    def _split_trailing_amount_text(self, text: str) -> str:
        match = re.match(
            r"^(?P<label>.+?)\s+(?P<amount>[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?)$",
            text.strip(),
        )
        if not match:
            return text
        return f"{match.group('label')}  {match.group('amount')}"

    def _looks_like_amount(self, value: str) -> bool:
        normalized = value.strip().replace(",", "")
        normalized = normalized.replace("$", "").replace("€", "")
        return bool(re.fullmatch(r"[-+]?\d+(?:[.:]\d{1,2})?", normalized))

    def _looks_like_qty_unit(self, value: str) -> bool:
        value = value.strip().replace("€", "").replace("$", "")
        patterns = [
            r"^\d+(?:[.,]\d+)?\s*[xX*]\s*\d+(?:[.,]\d{1,2})?$",
            r"^\d+(?:[.,]\d+)?\s+\d+(?:[.,]\d{1,2})?$",
        ]
        return any(re.fullmatch(pattern, value) for pattern in patterns)
