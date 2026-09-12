"""Build receipt lines from raw Odoo POS order JSON.

Odoo POS frontend sends **only** raw order data (no pre-rendered lines).
All receipt layout — company info, customer, products, totals, payments,
QR codes, footers — is built here in Python, then fed to
``DeviceManager._build_escpos_bytes()`` for ESC/POS byte generation.

Supports:
- Standard POS receipts (with company, customer, products, totals, payments)
- Kitchen tickets / preparation display tickets
- ``receipt_language`` field for multi-language label support
- Graceful fallback for missing fields
"""

from __future__ import annotations

import base64
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

_logger = logging.getLogger(__name__)

SEPARATOR = "-" * 48


# ── i18n labels ───────────────────────────────────────────────────────
# These can be overridden based on receipt_language from order config.

_LABELS: dict[str, dict[str, str]] = {
    "es_ES": {
        "TABLE": "MESA",
        "ORDER": "PEDIDO",
        "SUBTOTAL": "Subtotal",
        "DISCOUNT": "Descuento",
        "TAX": "IGIC 7%",
        "TOTAL": "TOTAL",
        "PAID": "Pagado",
        "DUE": "Pendiente",
        "CHANGE": "Cambio",
        "CODE": "Código",
        "NOTE": "Nota",
        "LOYALTY_WON": "Ganados",
        "LOYALTY_SPENT": "Utilizados",
        "LOYALTY_BALANCE": "Saldo",
        "POINTS": "Puntos",
        "UNTIL": "Hasta",
        "QTY": "Uds.",
        "PRODUCT": "Producto",
        "AMOUNT": "Importe",
    },
    "en_US": {
        "TABLE": "TABLE",
        "ORDER": "ORDER",
        "SUBTOTAL": "Subtotal",
        "DISCOUNT": "Discount",
        "TAX": "Tax",
        "TOTAL": "TOTAL",
        "PAID": "Paid",
        "DUE": "Due",
        "CHANGE": "Change",
        "CODE": "Code",
        "NOTE": "Note",
        "LOYALTY_WON": "Won",
        "LOYALTY_SPENT": "Spent",
        "LOYALTY_BALANCE": "Balance",
        "POINTS": "Points",
        "UNTIL": "Until",
        "QTY": "Qty",
        "PRODUCT": "Product",
        "AMOUNT": "Amount",
    },
    "zh_CN": {
        "TABLE": "桌号",
        "ORDER": "订单",
        "SUBTOTAL": "小计",
        "DISCOUNT": "折扣",
        "TAX": "税额",
        "TOTAL": "合计",
        "PAID": "已付",
        "DUE": "欠款",
        "CHANGE": "找零",
        "CODE": "编码",
        "NOTE": "备注",
        "LOYALTY_WON": "本次获得",
        "LOYALTY_SPENT": "本次使用",
        "LOYALTY_BALANCE": "积分余额",
        "POINTS": "积分",
        "UNTIL": "有效期至",
        "QTY": "数量",
        "PRODUCT": "商品",
        "AMOUNT": "金额",
    },
}

_FALLBACK_LANG = "en_US"


def _labels(lang: str) -> dict[str, str]:
    return _LABELS.get(lang, _LABELS[_FALLBACK_LANG])


def _resolve_lang(order: dict[str, Any]) -> str:
    """Resolve receipt language from order config or country."""
    config = order.get("config")
    if isinstance(config, dict):
        lang = _text(config.get("receipt_language"))
        if lang and lang in _LABELS:
            return lang
    company = order.get("company", {})
    if isinstance(company, dict):
        country = _text(company.get("country_id") or "")
        if country and "España" in country or "Spain" in country or "Estados Unidos" in country:
            return "es_ES"
    return _FALLBACK_LANG


# ── helpers ───────────────────────────────────────────────────────────

def _text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return str(value).strip()


def _decimal(value: Any) -> Decimal:
    parsed = _parse_decimal(value)
    return parsed if parsed is not None and parsed.is_finite() else Decimal("0")


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.,-]", "", str(value))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _money(order: dict[str, Any], amount: Any) -> str:
    amt = _decimal(amount)
    currency = order.get("currency", {}) or {}
    symbol = _text(currency.get("symbol") or "€")
    position = str(currency.get("position", "after")).strip().lower()
    formatted = f"{amt:.2f}"
    if symbol == "€":
        formatted = formatted.replace(".", ",")
    if position == "before":
        return f"{symbol}{formatted}"
    return f"{formatted} {symbol}"


def _order_date_text(order: dict[str, Any]) -> str:
    date_order = order.get("date_order", "")
    if not date_order:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", date_order)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return _text(date_order)[:16]


def _tracking_text(order: dict[str, Any]) -> str:
    config = order.get("config") if isinstance(order.get("config"), dict) else {}
    if not (order.get("is_restaurant") or config.get("module_pos_restaurant")):
        return ""
    tracking = _text(order.get("tracking_number"))
    if tracking:
        return f"#{tracking}"
    fallback = _text(order.get("name") or order.get("pos_reference"))
    if not fallback:
        return ""
    match = re.search(r"(\d+)\s*$", fallback)
    return f"#{match.group(1)}" if match else fallback


def _table_text(order: dict[str, Any]) -> str:
    table = order.get("table_id")
    if isinstance(table, dict):
        return _text(table.get("table_number") or table.get("name"))
    return _text(
        order.get("table_number")
        or order.get("table_name")
        or order.get("table")
        or order.get("table_display_name")
    )


# ── line / discount helpers ───────────────────────────────────────────

def _pick_number(line: dict[str, Any], keys: list[str]) -> Decimal:
    for key in keys:
        d = _parse_decimal(line.get(key))
        if d is not None and d > 0:
            return d
    return Decimal("0")


def _line_discounted_total(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "price_subtotal_incl", "priceSubtotalIncl",
        "price_incl", "priceIncl",
        "price_with_tax", "priceWithTax",
        "total_with_tax", "totalWithTax",
        "price", "total_rounded", "totalRounded",
    ])


def _line_original_unit_price(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "oldUnitPrice", "old_unit_price",
        "price_unit_before_discount", "priceUnitBeforeDiscount",
        "lst_price", "list_price", "listPrice",
    ])


def _line_discounted_unit_price(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "unit_price", "unitPrice", "price_unit", "priceUnit",
        "display_price", "displayPrice",
    ])


def _line_display_unit_price(line: dict[str, Any], total: Decimal) -> Decimal:
    """Return Odoo's unit price, deriving it from total/quantity if absent."""
    unit_price = _line_discounted_unit_price(line)
    if unit_price > 0:
        return unit_price
    qty = _line_qty(line)
    return total / qty if qty > 0 and total > 0 else Decimal("0")


def _line_qty(line: dict[str, Any]) -> Decimal:
    qty = line.get("qty") or line.get("quantity") or 0
    d = _parse_decimal(qty)
    return d if d is not None else Decimal("0")


def _line_original_total_from_fields(line: dict[str, Any]) -> Decimal:
    qty = _line_qty(line)
    direct = _pick_number(line, [
        "price_with_tax_before_discount", "priceWithTaxBeforeDiscount",
        "total_with_tax_before_discount", "totalWithTaxBeforeDiscount",
        "oldPrice", "old_price",
        "price_without_discount", "priceWithoutDiscount",
        "total_before_discount", "totalBeforeDiscount",
    ])
    if direct > 0:
        return direct
    unit = _line_original_unit_price(line)
    return unit * qty if qty and unit else Decimal("0")


def _line_discount_pct(line: dict[str, Any]) -> Decimal:
    discount_raw = line.get("discount")
    if discount_raw is not None:
        d = _parse_decimal(discount_raw)
        if d is not None and d > 0:
            return d

    direct = _pick_number(line, [
        "discount", "discount_pct", "discountPct",
        "discount_percent", "discountPercent",
    ])
    if direct > 0:
        return direct

    for key in ("discountStr", "discount_str", "discountText", "discount_text"):
        dt = _text(line.get(key))
        if dt:
            m = re.search(r"([\d.]+)", dt)
            if m:
                d = _parse_decimal(m.group(1))
                if d is not None and d > 0:
                    return d
            break

    discounted = _line_discounted_total(line)
    original = _line_original_total_from_fields(line)
    if original > discounted > 0:
        return ((original - discounted) / original) * Decimal("100")

    qty = _line_qty(line)
    discounted_unit = _line_discounted_unit_price(line)
    original_unit = _line_original_unit_price(line)
    if qty > 0 and original_unit > discounted_unit > 0:
        return ((original_unit - discounted_unit) / original_unit) * Decimal("100")
    return Decimal("0")


def _line_original_total(line: dict[str, Any], discounted: Decimal) -> Decimal:
    direct = _line_original_total_from_fields(line)
    if direct > 0:
        return direct
    discount = _line_discount_pct(line)
    if not discount or discount >= 100 or not discounted:
        return Decimal("0")
    return discounted / (Decimal("1") - discount / Decimal("100"))


def _order_discount_total(order: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for line in order.get("lines", []):
        if not isinstance(line, dict) or line.get("combo_parent_id"):
            continue
        discounted = _line_discounted_total(line)
        original = _line_original_total(line, discounted)
        if original > discounted > 0:
            total += original - discounted
    return total


def _calculate_subtotal(order: dict[str, Any]) -> Decimal:
    """Calculate subtotal (sum of all line discounted totals)."""
    total = Decimal("0")
    for line in order.get("lines", []):
        if not isinstance(line, dict) or line.get("combo_parent_id"):
            continue
        total += _line_discounted_total(line)
    return total


def _qty_text(line: dict[str, Any]) -> str:
    qty = line.get("qty") or line.get("quantity") or 0
    if isinstance(qty, str):
        raw_qty = qty.strip()
        if "," in raw_qty:
            return raw_qty
    d = _parse_decimal(qty)
    if d is None:
        return "0"
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.2f}".rstrip("0").rstrip(".")


def _split_name_and_options(line: dict[str, Any]) -> tuple[str, list[str]]:
    display = line.get("orderDisplayProductName", {})
    options: list[str] = []
    if isinstance(display, dict):
        base_name = (
            _text(display.get("name"))
            or _text(line.get("basic_name"))
            or _text(line.get("product_name"))
            or _text(line.get("full_product_name"))
            or _text(line.get("name"))
        )
        attr_str = _text(display.get("attributeString") or display.get("attribute_string"))
        if attr_str:
            options.extend(s.strip() for s in attr_str.split(",") if s.strip())
    else:
        base_name = (
            _text(line.get("basic_name"))
            or _text(line.get("product_name"))
            or _text(line.get("full_product_name"))
            or _text(line.get("name"))
        )

    raw_attributes = line.get("attribute_value_names") or line.get("attribute_values") or []
    if isinstance(raw_attributes, (list, tuple)):
        for attribute in raw_attributes:
            value = _text(attribute)
            if value:
                options.append(value)
    elif raw_attributes:
        options.extend(s.strip() for s in _text(raw_attributes).split(",") if s.strip())
    full_name_for_attributes = (
        _text(line.get("full_product_name"))
        or _text(line.get("product_name"))
        or _text(line.get("name"))
    )
    name_match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", full_name_for_attributes)
    if name_match:
        options.extend(s.strip() for s in name_match.group(2).split(",") if s.strip())
    options = list(dict.fromkeys(options))
    if any(option.lower().startswith("customization: custom:") for option in options):
        options = [option for option in options if option.lower() != "custom"]
    options = [
        re.sub(r"^Customization:\s*", "", option, flags=re.IGNORECASE).strip()
        for option in options
    ]
    if options:
        return base_name, options

    full_name = (
        _text(line.get("full_product_name"))
        or _text(line.get("product_name"))
        or _text(line.get("name"))
    )
    if not full_name:
        full_name = _text(line.get("product_name"))
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", full_name) if full_name else None
    if match:
        return match.group(1).strip(), [s.strip() for s in match.group(2).split(",") if s.strip()]
    return base_name or full_name or "", []


# ── section builders ───────────────────────────────────────────────────

def _company_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    company = order.get("company")
    if not isinstance(company, dict):
        return []
    lines: list[dict[str, Any]] = []
    for field, bold in [("name", True), ("street", False), ("street2", False)]:
        val = _text(company.get(field))
        if val:
            lines.append({"text": val, "align": "center", "bold": bold})
    locality = " ".join(filter(None, [
        _text(company.get("zip")),
        _text(company.get("city")),
        _text(company.get("state_id")),
    ]))
    if locality:
        lines.append({"text": locality, "align": "center"})
    country = _text(company.get("country_id"))
    if country:
        lines.append({"text": country, "align": "center"})
    phone = _text(company.get("phone"))
    if phone:
        lines.append({"text": phone, "align": "center"})
    return lines


def _customer_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    partner = order.get("partner_id") or order.get("partner") or order.get("customer")
    if not isinstance(partner, dict):
        return []
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any, *, bold: bool = False, value_class: str = "") -> None:
        text = _text(value)
        identity = text.casefold()
        if not text or identity in seen:
            return
        seen.add(identity)
        classes = ["customer-info"]
        if value_class:
            classes.append(value_class)
        lines.append({"text": text, "align": "center", "bold": bold, "classes": classes})

    full_name = ", ".join(filter(None, [_text(partner.get("parent_name")), _text(partner.get("name"))]))
    if full_name:
        add(full_name, bold=True, value_class="customer-name")
    add(partner.get("vat") or partner.get("tax_id"), value_class="customer-vat")
    contact_address = _text(partner.get("pos_contact_address") or partner.get("contact_address"))
    if contact_address:
        add(contact_address, value_class="customer-address")
    else:
        add(partner.get("street") or partner.get("address"), value_class="customer-address")
        add(partner.get("street2"), value_class="customer-address")
        locality = " ".join(filter(None, [
            _text(partner.get("zip")), _text(partner.get("city")), _text(partner.get("state_id")),
        ]))
        add(locality, value_class="customer-region")
        add(partner.get("country_id"), value_class="customer-country")
    add(partner.get("phone"), value_class="customer-phone")
    add(partner.get("mobile"), value_class="customer-mobile")
    add(partner.get("email"), value_class="customer-email")
    return lines


def _portal_url(order: dict[str, Any]) -> str:
    base_url = _text(order.get("config", {}).get("_base_url") if isinstance(order.get("config"), dict) else "")
    return f"{base_url}/pos/ticket" if base_url else ""


def _local_receipt_logo_src() -> str:
    """Return a data-URI for the IoT Box's locally cached receipt logo.

    Reads ``<resource>/assets/logo.png``, which the IoT Box refreshes from
    Odoo via its logo sync (/api/logo/sync).  Returns "" when no local logo
    is available so the logo block is simply skipped.
    """
    resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", str(Path(__file__).resolve().parents[1])))
    logo_path = resource_dir / "assets" / "logo.png"
    try:
        if not logo_path.is_file():
            return ""
        data = logo_path.read_bytes()
        if not data:
            return ""
        return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
    except OSError:
        return ""


def _order_barcode_src(order: dict[str, Any]) -> str:
    """Return Odoo's Code128 image URL for the receipt number."""
    explicit_url = _text(order.get("order_barcode_url") or order.get("barcode_url"))
    if explicit_url:
        return explicit_url
    config = order.get("config", {}) if isinstance(order.get("config"), dict) else {}
    base_url = _text(config.get("_base_url") or order.get("_base_url")).rstrip("/")
    reference = _text(order.get("pos_reference") or order.get("name"))
    if not base_url or not reference:
        return ""
    return f"{base_url}/report/barcode?{urlencode({'barcode_type': 'Code128', 'value': reference, 'width': 420, 'height': 96})}"


def _ticket_qr_src(order: dict[str, Any]) -> str:
    company = order.get("company", {}) if isinstance(order.get("company"), dict) else {}
    if not order.get("finalized") or not order.get("access_token"):
        return ""
    base_url = _text(order.get("config", {}).get("_base_url") if isinstance(order.get("config"), dict) else "")
    if not base_url:
        return ""
    token = order["access_token"]
    validation_url = f"{base_url}/pos/ticket/validate?access_token={token}"
    return f"{base_url}/report/barcode?{urlencode({'barcode_type': 'QR', 'value': validation_url, 'width': 180, 'height': 180})}"


def _ticket_code_fallback(order: dict[str, Any]) -> str:
    """Derive a printable ticket code from the receipt reference when the
    order has no ``ticket_code`` (e.g. orders created via the Movi API).
    Returns the trailing digits of the reference, max 5 characters."""
    reference = _text(order.get("pos_reference") or order.get("name"))
    match = re.search(r"(\d+)\s*$", reference)
    if match:
        return match.group(1)[-5:]
    return ""


def _program_type(item: dict[str, Any]) -> str:
    program = item.get("program") or item.get("program_id")
    if isinstance(program, dict):
        value = program.get("program_type") or program.get("type")
    else:
        value = None
    return _text(item.get("program_type") or value).lower()


def _collect_records(order: dict[str, Any], field_names: tuple[str, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field_name in field_names:
        raw_cards = order.get(field_name)
        if isinstance(raw_cards, dict):
            raw_cards = [raw_cards]
        if not isinstance(raw_cards, list):
            continue
        for raw_card in raw_cards:
            if not isinstance(raw_card, dict):
                continue
            code = _text(
                raw_card.get("code") or raw_card.get("barcode")
                or raw_card.get("coupon_code") or raw_card.get("voucher_code")
            )
            identity = code or _text(raw_card.get("id") or raw_card.get("name") or raw_card.get("title"))
            identity = identity.casefold()
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            result.append(raw_card)
    return result


def _voucher_cards(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect gift cards and eWallets, excluding coupon/loyalty programs."""
    cards = _collect_records(order, (
        "loyalty_cards", "loyaltyCards", "gift_cards", "giftCards",
        "vouchers", "wallets", "e_wallets", "eWallets",
    ))
    return [
        card for card in cards
        if not _program_type(card) or _program_type(card) in {"gift_card", "ewallet"}
    ]


def _coupon_cards(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect Odoo coupon codes generated or activated for the order."""
    cards = _collect_records(order, (
        "new_coupon_info", "newCouponInfo", "coupons", "coupon_codes",
        "activated_coupons", "activatedCoupons",
    ))
    return [
        card for card in cards
        if not _program_type(card)
        or _program_type(card) in {"coupons", "promo_code", "next_order_coupons"}
    ]


def _promotion_records(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect explicit promotions and Odoo POS reward order lines."""
    records = _collect_records(order, (
        "promotions", "applied_promotions", "appliedPromotions",
        "rewards", "claimed_rewards", "claimedRewards",
    ))
    for line in order.get("lines") or order.get("orderlines") or []:
        if not isinstance(line, dict) or not line.get("is_reward_line"):
            continue
        reward = line.get("reward_id") or line.get("reward")
        reward = reward if isinstance(reward, dict) else {}
        record = {
            "name": reward.get("description") or reward.get("name")
                    or line.get("full_product_name") or line.get("product_name"),
            "program": reward.get("program_id") or line.get("program_id"),
            "reward_type": reward.get("reward_type") or line.get("reward_type"),
            "amount": line.get("price_subtotal_incl") or line.get("price") or line.get("total"),
            "points_cost": line.get("points_cost"),
            "code": line.get("reward_identifier_code"),
        }
        if not _program_type(record) or _program_type(record) in {"promotion", "buy_x_get_y"}:
            records.append(record)
    return [
        record for record in records
        if not _program_type(record) or _program_type(record) in {"promotion", "buy_x_get_y"}
    ]


def _voucher_amount(order: dict[str, Any], card: dict[str, Any]) -> str:
    for field_name in (
        "balance", "remaining_balance", "current_balance", "available_balance",
        "remaining_amount", "balance_amount", "amount", "point",
    ):
        raw_value = card.get(field_name)
        if raw_value in (None, ""):
            continue
        parsed = _parse_decimal(raw_value)
        return _money(order, parsed) if parsed is not None else _text(raw_value)
    return ""


def build_voucher_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    labels = _labels(_resolve_lang(order))
    for card in _voucher_cards(order):
        lines.append({
            "type": "spacer", "align": "left",
            "classes": ["receipt-spacer", "gift-card-spacer"],
        })
        name = _text(
            card.get("name") or card.get("title") or card.get("program_name")
            or "Tarjeta regalo"
        )
        code = _text(
            card.get("code") or card.get("barcode")
            or card.get("coupon_code") or card.get("voucher_code")
        )
        if name:
            lines.append({
                "text": name, "align": "center", "bold": True,
                "classes": ["gift-card-title"],
            })
        if code:
            lines.append({
                "text": code, "align": "center",
                "classes": ["gift-card-code"],
            })
        explicit_barcode = _text(card.get("barcodeSrc") or card.get("barcode_src") or card.get("barcode_url"))
        qr_src = _text(card.get("qrSrc") or card.get("qr_src") or card.get("qr_url"))
        if code or explicit_barcode:
            barcode_src = explicit_barcode or f"/report/barcode?{urlencode({'barcode_type': 'Code128', 'value': code, 'width': 360, 'height': 80})}"
            lines.append({
                "type": "image", "src": barcode_src, "align": "center",
                "classes": ["gift-card-barcode"], "width": 360, "height": 80,
                "image_kind": "barcode", "barcode_type": "Code128",
                "barcode_value": code,
            })
        elif qr_src:
            lines.append({
                "type": "image", "src": qr_src, "align": "center",
                "classes": ["gift-card-qr"], "width": 125, "height": 125,
                "image_kind": "qr",
            })
        expiration = _text(card.get("expiration_date") or card.get("expirationDate"))
        if expiration:
            lines.append({
                "text": f"{labels['UNTIL']}: {expiration}", "align": "center",
                "classes": ["gift-card-expiration"],
            })
        amount = _voucher_amount(order, card)
        if amount:
            lines.append({
                "text": amount, "align": "center", "bold": True,
                "double_width": True, "classes": ["gift-card-amount"],
            })
    return lines


def build_coupon_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Render Odoo coupon codes separately from gift-card balances."""
    lines: list[dict[str, Any]] = []
    labels = _labels(_resolve_lang(order))
    for coupon in _coupon_cards(order):
        name = _text(coupon.get("program_name") or coupon.get("name") or coupon.get("title") or "Cupón")
        code = _text(coupon.get("code") or coupon.get("barcode") or coupon.get("coupon_code"))
        lines.append({"type": "spacer", "classes": ["receipt-spacer", "coupon-spacer"]})
        lines.append({"text": name, "align": "center", "bold": True, "classes": ["coupon-title"]})
        if code:
            lines.append({"text": code, "align": "center", "classes": ["coupon-code"]})
            lines.append({
                "type": "image", "src": f"/report/barcode?{urlencode({'barcode_type': 'Code128', 'value': code, 'width': 360, 'height': 80})}",
                "align": "center", "classes": ["coupon-barcode"], "width": 360, "height": 80,
                "image_kind": "barcode", "barcode_type": "Code128", "barcode_value": code,
            })
        expiration = _text(coupon.get("expiration_date") or coupon.get("expirationDate"))
        if expiration:
            lines.append({"text": f"{labels['UNTIL']}: {expiration}", "align": "center", "classes": ["coupon-expiration"]})
    return lines


def build_promotion_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Render applied promotion/reward metadata without hiding reward product lines."""
    lines: list[dict[str, Any]] = []
    for promotion in _promotion_records(order):
        program = promotion.get("program") or promotion.get("program_id")
        program_name = _text(program.get("name")) if isinstance(program, dict) else ""
        name = _text(promotion.get("name") or promotion.get("description") or program_name or "Promoción")
        reward_type = _text(promotion.get("reward_type") or promotion.get("type"))
        amount = promotion.get("amount") or promotion.get("discount_amount")
        points_cost = promotion.get("points_cost")
        code = _text(promotion.get("code") or promotion.get("reward_identifier_code"))
        detail_parts = []
        if reward_type:
            detail_parts.append(reward_type.replace("_", " "))
        parsed_amount = _parse_decimal(amount)
        if parsed_amount not in (None, Decimal("0")):
            detail_parts.append(_money(order, parsed_amount))
        if _parse_decimal(points_cost) not in (None, Decimal("0")):
            detail_parts.append(f"{_points_text(points_cost)} pts")
        lines.append({"text": name, "align": "left", "bold": True, "classes": ["promotion-title"]})
        if program_name and program_name.casefold() != name.casefold():
            lines.append({"text": program_name, "align": "left", "classes": ["promotion-program"]})
        if detail_parts:
            lines.append({"text": " · ".join(detail_parts), "align": "left", "classes": ["promotion-detail"]})
        if code:
            lines.append({"text": code, "align": "left", "classes": ["promotion-code"]})
    return lines


def _payment_terminal_receipts(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect Redsys/payment-terminal receipts from Odoo's common payload shapes."""
    receipts = _collect_records(order, (
        "payment_terminal_receipts", "paymentTerminalReceipts",
        "redsys_receipts", "redsysReceipts", "card_receipts", "cardReceipts",
    ))
    for payment in order.get("payment_lines") or order.get("statement_ids") or []:
        if not isinstance(payment, dict):
            continue
        receipt_value = (
            payment.get("payment_terminal_receipt") or payment.get("paymentTerminalReceipt")
            or payment.get("terminal_receipt") or payment.get("receipt") or payment.get("ticket")
        )
        terminal_fields = {
            "authorization_code", "auth_code", "transaction_id", "terminal_id",
            "card_type", "card_number", "cardholder_name", "merchant_id", "stan", "rrn",
        }
        if receipt_value or terminal_fields.intersection(payment):
            entry = dict(payment)
            if receipt_value and not entry.get("receipt"):
                entry["receipt"] = receipt_value
            receipts.append(entry)
    return receipts


def build_payment_terminal_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Render Redsys card transaction records while preserving native receipt text."""
    lines: list[dict[str, Any]] = []
    field_labels = (
        (("card_type", "card_brand"), "Tarjeta"),
        (("card_number", "masked_card", "pan"), "Tarjeta nº"),
        (("cardholder_name",), "Titular"),
        (("authorization_code", "auth_code"), "Autorización"),
        (("terminal_id", "terminal"), "Terminal"),
        (("transaction_id", "transaction", "operation_id"), "Transacción"),
        (("merchant_id", "merchant"), "Comercio"),
        (("stan",), "STAN"),
        (("rrn",), "RRN"),
    )
    for receipt in _payment_terminal_receipts(order):
        receipt_lines: list[str] = []
        raw_lines = receipt.get("lines")
        if isinstance(raw_lines, list):
            receipt_lines.extend(_text(value) for value in raw_lines if _text(value))
        for field_name in (
            "receipt", "payment_terminal_receipt", "paymentTerminalReceipt",
            "terminal_receipt", "ticket", "text",
        ):
            raw_text = receipt.get(field_name)
            if isinstance(raw_text, str) and raw_text.strip():
                receipt_lines.extend(line.strip() for line in raw_text.splitlines() if line.strip())
        if not receipt_lines:
            for aliases, label in field_labels:
                value = next((_text(receipt.get(alias)) for alias in aliases if _text(receipt.get(alias))), "")
                if value:
                    receipt_lines.append(f"{label}: {value}")
        if not receipt_lines:
            continue
        lines.append({
            "type": "spacer", "align": "left",
            "classes": ["receipt-spacer", "payment-terminal-spacer", "redsys-spacer"],
        })
        logo = receipt.get("logo") if isinstance(receipt.get("logo"), dict) else {}
        nfc_src = _text(
            receipt.get("nfc_logo_src") or receipt.get("nfcLogoSrc")
            or receipt.get("contactless_logo_src") or receipt.get("contactlessLogoSrc")
            or logo.get("src")
        )
        is_contactless = bool(
            receipt.get("contactless") or receipt.get("is_contactless") or receipt.get("nfc")
            or any("CONTACTLESS" in text.upper() or "NFC" in text.upper() for text in receipt_lines)
        )
        if nfc_src or is_contactless:
            lines.append({
                "type": "image", "src": nfc_src or "/assets/nfc_override.png", "align": "center",
                "classes": ["payment-terminal-logo", "payment-terminal-nfc-icon", "redsys-nfc-logo"],
                "width": 80, "image_kind": "logo",
            })
        for text in receipt_lines:
            lines.append({
                "text": text, "align": "center",
                "classes": ["payment-terminal-line", "pos-payment-terminal-receipt", "redsys-receipt-line"],
            })
    return lines


def _loyalty_stats(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Odoo getLoyaltyPoints()-shaped rows plus common serialized aliases."""
    for field_name in ("loyalty_points", "loyaltyPoints", "loyalty_stats", "loyaltyStats"):
        raw_stats = order.get(field_name)
        if isinstance(raw_stats, dict):
            raw_stats = list(raw_stats.values())
        if isinstance(raw_stats, list):
            return [
                item for item in raw_stats
                if isinstance(item, dict)
                and (not _program_type(item) or _program_type(item) == "loyalty")
            ]

    ui_state = order.get("uiState") if isinstance(order.get("uiState"), dict) else {}
    changes = order.get("couponPointChanges") or ui_state.get("couponPointChanges")
    if isinstance(changes, dict):
        changes = list(changes.values())
    if not isinstance(changes, list):
        return []
    result = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        if _program_type(change) and _program_type(change) != "loyalty":
            continue
        result.append({
            "couponId": change.get("coupon_id"),
            "points": {
                "name": change.get("point_name") or change.get("name") or "Points",
                "won": change.get("points") or 0,
                "spent": change.get("spent") or 0,
                "balance": change.get("balance") or 0,
                "total": change.get("total") or 0,
            },
        })
    return result


def _points_text(value: Any) -> str:
    parsed = _parse_decimal(value)
    if parsed is None:
        return _text(value)
    if parsed == parsed.to_integral_value():
        return str(int(parsed))
    return f"{parsed:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def build_loyalty_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Odoo-native loyalty won/spent/balance rows without currency symbols."""
    labels = _labels(_resolve_lang(order))
    lines: list[dict[str, Any]] = []
    for stat in _loyalty_stats(order):
        program = stat.get("program") if isinstance(stat.get("program"), dict) else {}
        if program and program.get("portal_visible") is False:
            continue
        points = stat.get("points") if isinstance(stat.get("points"), dict) else stat
        name = _text(points.get("name") or stat.get("name") or labels["POINTS"])
        won = points.get("won")
        spent = points.get("spent")
        balance = points.get("balance")
        total = points.get("total")
        if not any(_parse_decimal(value) not in (None, Decimal("0")) for value in (won, spent, balance, total)):
            continue
        lines.append({
            "type": "spacer", "align": "left",
            "classes": ["receipt-spacer", "loyalty-spacer"],
        })
        for label, value, value_class in (
            (f"{name} {labels['LOYALTY_WON']}", won, "loyalty-won"),
            (f"{name} {labels['LOYALTY_SPENT']}", spent, "loyalty-spent"),
            (f"{labels['LOYALTY_BALANCE']} {name}", balance if balance not in (None, "") else total, "loyalty-balance"),
        ):
            parsed = _parse_decimal(value)
            if parsed is None or parsed == 0:
                continue
            lines.append({
                "type": "header_meta_line",
                "left_text": label,
                "right_text": _points_text(value),
                "classes": ["loyalty-points", value_class],
            })
    return lines


# ── KIOSK pickup ticket builder ───────────────────────────────────────

def build_kiosk_pickup_ticket_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the dedicated KIOSK pickup ticket through its IoT template."""
    lines: list[dict[str, Any]] = []
    def add(block: str, row: dict[str, Any]) -> None:
        lines.append({**row, "_template_block": block})
    service = str(order.get("service") or order.get("service_type") or "").lower()
    add("order_type", {"text": "COMER AQUÍ" if service in {"dine-in", "dine_in", "dinein"} else "PARA LLEVAR", "align": "center", "bold": True})
    tracking = _text(order.get("tracking_number") or order.get("takeaway_number") or order.get("floating_order_name"))
    if tracking: add("tracking", {"text": f"# {tracking}", "align": "center", "bold": True, "classes": ["tracking-number", "kiosk-tracking-number"]})
    reference = _text(order.get("pos_reference") or order.get("name")); barcode = _order_barcode_src(order)
    if barcode: add("barcode", {"type": "image", "src": barcode, "align": "center", "width": 420, "height": 96, "image_kind": "barcode", "barcode_type": "Code128", "barcode_value": reference})
    if reference: add("barcode", {"text": reference, "align": "center"})
    if not order.get("finalized"):
        add("payment_prompt", {"text": "-" * 48, "align": "left", "classes": ["kiosk-payment-separator"]})
        add("payment_prompt", {"text": "POR FAVOR, PASE POR CAJA PARA PAGAR", "align": "center", "bold": True, "classes": ["kiosk-payment-prompt"]})
        add("payment_prompt", {"text": "-" * 48, "align": "left", "classes": ["kiosk-payment-separator"]})
    config = order.get("config") if isinstance(order.get("config"), dict) else {}
    point, placed = _text(config.get("name") or order.get("pos_config_name") or order.get("config_name")), _order_date_text(order)
    if point or placed: add("location", {"type": "header_meta_line", "left_text": point, "right_text": placed, "align": "left", "classes": ["kiosk-location-time"]})
    from .kiosk_ticket_template_store import apply_kiosk_ticket_template
    return apply_kiosk_ticket_template(lines)

# ── kitchen ticket builder ────────────────────────────────────────────

def build_kitchen_ticket_lines(
    order: dict[str, Any],
    template: dict[str, Any] | None = None,
    preview_fields: bool = False,
) -> list[dict[str, Any]]:
    """Build a kitchen / preparation display ticket.

    Matches the auto-print style from _buildKitchenEscposLines() in the
    Odoo preparation display service:
      1. Tracking number at the very top
      2. Order type and "NUEVO" title
      3. Raw table number without a MESA/TABLE prefix
      4. Product lines and separators
      5. Config name (left) + order time (right)
    """
    lines: list[dict[str, Any]] = []
    lang = _resolve_lang(order)
    L = _labels(lang)

    def mark_block(start: int, block_id: str) -> None:
        for template_line in lines[start:]:
            template_line["_template_block"] = block_id

    # ── 1. Tracking number at the very top, independent from table ──
    block_start = len(lines)
    tracking = _text(
        order.get("tracking_number")
        or order.get("takeaway_number")
        or order.get("pickup_number")
        or order.get("order_number")
    )
    if tracking:
        lines.append({
            "text": f"# {tracking}",
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
            "classes": ["kitchen-tracking-number"],
        })
    mark_block(block_start, "tracking")

    # ── 2. Order type line ──
    block_start = len(lines)
    service_type = order.get("service_type") if isinstance(order.get("service_type"), dict) else {}
    chino_order_type = _text(order.get("chino_order_type"))
    order_type = _text(
        service_type.get("label") or service_type.get("code")
        or order.get("preset_name") or chino_order_type or "DINE IN"
    )
    lines.append({
        "text": order_type,
        "align": "center", "bold": True,
        "double_width": True, "double_height": True,
        "classes": ["kitchen-order-type"],
    })
    mark_block(block_start, "order_type")

    # ── 3. Native kitchen notification: NUEVO, CANCELA, CAMBIO, etc. ──
    block_start = len(lines)
    changes = order.get("changes") if isinstance(order.get("changes"), dict) else {}
    order_notes = [
        _text(order.get("general_customer_note")),
        _text(order.get("internal_note")),
    ]
    order_notes = list(dict.fromkeys(note for note in order_notes if note))
    kitchen_title = _text(
        order.get("kitchen_title")
        or changes.get("title")
        or order.get("title")
        or ("NOTA" if order_notes else "NUEVO")
    ).upper()
    lines.append({
        "text": kitchen_title,
        "align": "center", "bold": True,
        "double_width": True, "double_height": True,
        "classes": ["kitchen-status"],
    })
    mark_block(block_start, "status")

    # ── 4. Table number value only (no MESA/TABLE prompt) ──
    block_start = len(lines)
    table = _table_text(order)
    if table:
        lines.append({
            "text": table,
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
            "classes": ["kitchen-table-number"],
        })
    mark_block(block_start, "order_meta")

    block_start = len(lines)
    lines.append({"text": SEPARATOR, "align": "left"})
    mark_block(block_start, "separator_before")

    # ── 4. Course-grouped product lines (qty × name; never receipt amount columns) ──
    block_start = len(lines)

    def course_name(group: dict[str, Any]) -> str:
        course = group.get("course") or group.get("course_id") or group.get("courseId")
        if isinstance(course, dict):
            value = course.get("name") or course.get("display_name") or course.get("sequence_name")
            if value:
                return _text(value)
        return _text(group.get("course_name") or group.get("courseName") or group.get("name"))

    def group_items(group: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen_objects: set[int] = set()
        stable_positions: dict[str, int] = {}
        for key in ("items", "new", "cancelled", "noteUpdate", "data", "lines"):
            values = group.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or id(item) in seen_objects:
                    continue
                normalized = dict(item)
                if key == "cancelled":
                    normalized["_cancelled"] = True
                stable_id = next(
                    (
                        str(normalized.get(field))
                        for field in ("uuid", "id", "orderline_id", "line_id")
                        if normalized.get(field) not in (None, "")
                    ),
                    "",
                )
                if stable_id and stable_id in stable_positions:
                    existing = result[stable_positions[stable_id]]
                    if normalized.get("_cancelled"):
                        existing["_cancelled"] = True
                    continue
                if stable_id:
                    stable_positions[stable_id] = len(result)
                result.append(normalized)
                seen_objects.add(id(item))
        return result

    def render_kitchen_item(raw_line: dict[str, Any]) -> None:
        if not isinstance(raw_line, dict):
            return
        qty = _qty_text(raw_line)
        name, options = _split_name_and_options(raw_line)
        name = name or _text(raw_line.get("basic_name") or raw_line.get("name"))
        if not name:
            return
        cancelled = bool(raw_line.get("_cancelled") or raw_line.get("cancelled"))
        lines.append({
            "type": "product_line",
            "qty": qty,
            "name": name,
            "total": "",
            "double_width": True,
            "kitchen_notification": "CANCELA" if cancelled else "",
            "classes": ["kitchen-product-line"] + (["kitchen-cancelled-line"] if cancelled else []),
        })
        if options:
            for opt in options:
                lines.append({
                    "text": f"    + {opt}",
                    "align": "left",
                    "classes": ["kitchen-note", "kitchen-attribute"],
                })
        # Odoo sends two independent kitchen note fields:
        # ``note`` is the product/orderline note and ``customer_note`` is the
        # customer note.  Both must reach the kitchen ticket; previously only
        # customer_note was rendered.
        notes: list[str] = []
        for raw_note in (raw_line.get("note"), raw_line.get("customer_note")):
            note = _text(raw_note)
            if note and note not in notes:
                notes.append(note)
        for note in notes:
            lines.append({
                "text": f"  {L['NOTE']}: {note}",
                "align": "left", "bold": True,
                "classes": ["kitchen-note", "kitchen-product-note"],
            })

    raw_groups = order.get("course_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raw_groups = changes.get("groupedData")
    if isinstance(raw_groups, list) and raw_groups:
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            name = course_name(group)
            if name:
                lines.append({
                    "text": f"** {name} **", "align": "center", "bold": True,
                    "double_width": True, "double_height": True,
                    "classes": ["kitchen-course-header"],
                })
            for raw_line in group_items(group):
                render_kitchen_item(raw_line)
    else:
        flat_lines = order.get("lines")
        if not isinstance(flat_lines, list) or not flat_lines:
            flat_lines = changes.get("data") or []
        for raw_line in flat_lines:
            if isinstance(raw_line, dict):
                render_kitchen_item(raw_line)
    for order_note in order_notes:
        lines.append({
            "text": f"  {L['NOTE']}: {order_note}",
            "align": "left", "bold": True,
                "classes": ["kitchen-note", "kitchen-order-note"],
        })
    mark_block(block_start, "products")
    block_start = len(lines)
    lines.append({"text": SEPARATOR, "align": "left"})
    mark_block(block_start, "separator_after")

    # ── 5. Config name (left) + order time (right) ──
    block_start = len(lines)
    config = order.get("config", {}) if isinstance(order.get("config"), dict) else {}
    config_name = _text(config.get("name") or order.get("pos_reference") or "")
    if not config_name:
        company = order.get("company", {}) if isinstance(order.get("company"), dict) else {}
        config_name = _text(company.get("name"))
    date_text = _order_date_text(order)
    time_part = date_text[11:16] if len(date_text) >= 16 else date_text
    if config_name or time_part:
        lines.append({
            "type": "header_meta_line",
            "left_text": config_name,
            "right_text": time_part,
            "classes": ["kitchen-footer", "kitchen-location-time"],
        })
    mark_block(block_start, "location")

    _logger.info(
        "Built kitchen ticket lines=%s tracking=%s",
        len(lines), tracking or "<none>",
    )
    if preview_fields:
        placeholders: dict[str, list[dict[str, Any]]] = {
            "tracking": [{
                "text": "# {{ tracking_number }}", "align": "center", "bold": True,
                "double_width": True, "double_height": True,
                "classes": ["kitchen-tracking-number"],
            }],
            "order_type": [{
                "text": "{{ chino_order_type || 'DINE IN' }}", "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            }],
            "status": [{
                "text": "{{ kitchen_title || changes.title || 'NUEVO' }}",
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            }],
            "order_meta": [{
                "text": "{{ table_id.table_number }}", "align": "center", "bold": True,
                "double_width": True, "double_height": True,
                "classes": ["kitchen-table-number"],
            }],
            "separator_before": [{"text": SEPARATOR, "align": "left"}],
            "products": [
                {
                    "text": "** {{ course_groups[].course_name || changes.groupedData[].course.name }} **",
                    "align": "center", "bold": True,
                    "double_width": True, "double_height": True,
                    "classes": ["kitchen-course-header"],
                },
                {
                    "type": "product_line", "qty": "{{ course_groups[].items[].qty }}",
                    "name": "{{ course_groups[].items[].full_product_name }}", "total": "",
                    "double_width": True, "classes": ["kitchen-product-line"],
                },
                # These carry the same classes as the real ticket
                # (``receipt_builder.py:1112``/``:1127``/``:1157``) so the
                # editor's per-line-type size and bold are visible in the
                # preview.  Without the classes the preview cannot show them.
                {
                    "text": "    + {{ course_groups[].items[].orderDisplayProductName.attributeString }}",
                    "align": "left", "classes": ["kitchen-note", "kitchen-attribute"],
                },
                {
                    "text": "  NOTA: {{ course_groups[].items[].customer_note }}",
                    "align": "left", "bold": True,
                    "classes": ["kitchen-note", "kitchen-product-note"],
                },
                {
                    "text": "  NOTA: {{ general_customer_note }}", "align": "left", "bold": True,
                    "classes": ["kitchen-note", "kitchen-order-note"],
                },
            ],
            "separator_after": [{"text": SEPARATOR, "align": "left"}],
            "location": [{
                "type": "header_meta_line",
                "left_text": "{{ config.name }}",
                "right_text": "{{ date_order.time }}",
                "classes": ["kitchen-footer", "kitchen-location-time"],
            }],
        }
        seen: set[str] = set()
        preview_lines: list[dict[str, Any]] = []
        for line in lines:
            block_id = str(line.get("_template_block") or "")
            if block_id not in placeholders:
                preview_lines.append(line)
            elif block_id not in seen:
                preview_lines.extend({**item, "_template_block": block_id} for item in placeholders[block_id])
                seen.add(block_id)
        for block_id, block_lines in placeholders.items():
            if block_id not in seen:
                preview_lines.extend({**item, "_template_block": block_id} for item in block_lines)
        lines = preview_lines
    from .kitchen_template_store import apply_kitchen_template

    return apply_kitchen_template(lines, template)


# ── main POS receipt builder ──────────────────────────────────────────

def build_receipt_lines(
    order: dict[str, Any],
    template: dict[str, Any] | None = None,
    preview_fields: bool = False,
) -> list[dict[str, Any]]:
    """Build receipt lines from raw Odoo POS order JSON.

    All layout decisions are made here — the Odoo JS simply serialises
    the order and sends it across.  Returns a list of dicts compatible
    with ``DeviceManager._build_escpos_bytes()``.
    """
    lines: list[dict[str, Any]] = []
    lang = _resolve_lang(order)
    L = _labels(lang)
    is_final = bool(order.get("finalized"))
    discount_total = _order_discount_total(order)
    config = order.get("config", {}) if isinstance(order.get("config"), dict) else {}

    def mark_block(start: int, block_id: str) -> None:
        for template_line in lines[start:]:
            template_line["_template_block"] = block_id

    # ══════════════════════════════════════════════════════════════════
    # 1. Logo
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    # The IoT Box renders the logo from its local cache (assets/logo.png,
    # refreshed from Odoo via /api/logo/sync).  Falls back to a caller-
    # provided logo URL only when no local logo is cached yet.
    logo_url = _local_receipt_logo_src() or _text(config.get("receiptLogoUrl"))
    if logo_url:
        lines.append({
            "type": "image", "src": logo_url, "align": "center",
            "classes": ["pos-receipt-logo"],
            "image_kind": "logo",
        })
    mark_block(block_start, "logo")

    # ══════════════════════════════════════════════════════════════════
    # 2. Company
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.extend(_company_lines(order))
    mark_block(block_start, "company")

    # ══════════════════════════════════════════════════════════════════
    # 3. Customer
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    customer_info = _customer_lines(order)
    if customer_info:
        lines.extend(customer_info)
        lines.append({"text": "", "align": "left"})

    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})
    mark_block(block_start, "customer")

    # ══════════════════════════════════════════════════════════════════
    # 4. Shared date+cashier info (used below table)
    # ══════════════════════════════════════════════════════════════════
    reference = _text(order.get("pos_reference") or order.get("name"))
    date_text = _order_date_text(order)
    operator = _text(order.get("user_id", {}).get("name"))
    
    info_parts = []
    if date_text:
        info_parts.append(date_text)
    if operator:
        info_parts.append(operator)
    info_text = " | ".join(info_parts) if info_parts else ""

    # ══════════════════════════════════════════════════════════════════
    # 5. Pickup number and table are independent template sections.
    # ══════════════════════════════════════════════════════════════════
    table = _table_text(order)
    order_marker = _tracking_text(order)
    block_start = len(lines)
    if order_marker:
        lines.append({
            "text": order_marker, "align": "center", "bold": True,
            "double_width": True, "double_height": True,
            "classes": ["tracking-number", "tracking-info"],
        })
        lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})
    mark_block(block_start, "tracking")

    block_start = len(lines)
    if table:
        text = f"{L['TABLE']} {table}"
        lines.append({
            "text": text, "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })
        lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})
    mark_block(block_start, "table")

    # ══════════════════════════════════════════════════════════════════
    # 6. Simplified invoice info (Factura Simplificada)
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    if is_final:
        order_name = _text(order.get("name"))
        seq_match = re.search(r"(\d+)$", order_name) if order_name else None
        if seq_match:
            seq_number = seq_match.group(1)
            current_year = date_text[:4] if date_text else "2026"
            lines.append({"text": "*" * 26, "align": "center", "classes": ["invoice-asterisk-border"]})
            lines.append({"text": "Factura Simplificada", "align": "center", "bold": True})
            lines.append({"text": f"Fs/{current_year}/{seq_number}", "align": "center"})
            lines.append({"text": "*" * 26, "align": "center", "classes": ["invoice-asterisk-border"]})
            lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})
    mark_block(block_start, "invoice")

    # ══════════════════════════════════════════════════════════════════
    # 7. Reference + date + cashier (below table)
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    order_label = f"{L['ORDER']} {reference}" if reference else ""
    if order_label:
        lines.append({
            "text": order_label,
            "align": "center", "bold": True,
        })
        barcode_src = _order_barcode_src(order)
        if barcode_src:
            lines.append({
                "type": "image",
                "src": barcode_src,
                "align": "center",
                "width": 420,
                "height": 96,
                "image_kind": "barcode",
                "barcode_type": "Code128",
                "barcode_value": reference,
                "classes": ["receipt-order-barcode", "no-barcode-separator"],
            })
    if info_text:
        lines.append({
            "text": info_text,
            "align": "center",
        })
    if order_label or info_text:
        lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})
    mark_block(block_start, "order_info")

    # ══════════════════════════════════════════════════════════════════
    # 8. Product column header
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.append({
        "type": "product_header",
        "qty_label": L["QTY"],
        "product_label": L["PRODUCT"],
        "amount_label": L["AMOUNT"],
        "bold": True,
    })
    mark_block(block_start, "product_header")

    # ══════════════════════════════════════════════════════════════════
    # 9. Product lines
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    for raw_line in order.get("lines", []):
        if not isinstance(raw_line, dict) or raw_line.get("combo_parent_id"):
            continue
        name, options = _split_name_and_options(raw_line)
        discounted = _line_discounted_total(raw_line)
        # A combo (套餐) parent row can have zero price while its children carry
        # the amount; keep the row (with its combo items) so the combo is not
        # dropped from the receipt.
        if discounted <= 0 and not options:
            continue
        discount_pct = _line_discount_pct(raw_line)
        original = _line_original_total(raw_line, discounted)
        entry: dict[str, Any] = {
            "type": "product_line",
            "qty": _qty_text(raw_line),
            "name": name,
            "unit_price": _money(order, _line_display_unit_price(raw_line, discounted)),
            "total": _money(order, discounted),
            "combo_items": options,
        }
        if discount_pct > 0:
            percent_text = format(discount_pct.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
            entry["discount_text"] = f"{percent_text}%"
        if original > 0:
            entry["original_total"] = _money(order, original)
        lines.append(entry)
        lines.append({
            "type": "spacer", "align": "left",
            "classes": ["receipt-spacer", "product-line-spacer"],
        })
        note = _text(raw_line.get("customer_note"))
        if note:
            lines.append({
                "text": f"  {L['NOTE']}: {note}",
                "align": "left", "classes": ["customer-note"],
            })
    mark_block(block_start, "products")

    # Applied promotions and reward metadata are independently configurable.
    block_start = len(lines)
    lines.extend(build_promotion_lines(order))
    mark_block(block_start, "promotions")

    # ══════════════════════════════════════════════════════════════════
    # 9. Spacer (replaces separator before Discount/Tax)
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 10. Subtotal
    # ══════════════════════════════════════════════════════════════════
    subtotal_amount = _calculate_subtotal(order)
    if subtotal_amount > 0:
        lines.append({
            "type": "header_meta_line",
            "left_text": L["SUBTOTAL"],
            "right_text": _money(order, subtotal_amount),
        })

    # ══════════════════════════════════════════════════════════════════
    # 11. Discount summary
    # ══════════════════════════════════════════════════════════════════
    if discount_total > 0:
        amt = _decimal(discount_total)
        lines.append({
            "type": "header_meta_line",
            "left_text": L["DISCOUNT"],
            "right_text": f"-{_money(order, amt)}",
        })

    # ══════════════════════════════════════════════════════════════════
    # 12. Tax (per tax group when available, matching the desktop POS)
    # ══════════════════════════════════════════════════════════════════
    tax_details = order.get("tax_details")
    tax_amount = order.get("amountTaxes")
    if isinstance(tax_details, list) and tax_details:
        # One row per tax group — same as the native POS receipt.
        for td in tax_details:
            if not isinstance(td, dict):
                continue
            label = _text(td.get("group_name") or td.get("group_label") or L["TAX"])
            amt = _decimal(td.get("tax_amount") or td.get("tax_amount_currency"))
            if amt > 0:
                lines.append({
                    "type": "header_meta_line",
                    "left_text": label,
                    "right_text": _money(order, amt),
                })
    elif tax_amount is not None and _decimal(tax_amount) > 0:
        tax_names = order.get("tax_names", [])
        tax_label = ", ".join(tax_names) if tax_names else L["TAX"]
        lines.append({
            "type": "header_meta_line",
            "left_text": tax_label,
            "right_text": _money(order, tax_amount),
        })

    # ══════════════════════════════════════════════════════════════════
    # 13. TOTAL (separators are controlled by custom template blocks)
    # ══════════════════════════════════════════════════════════════════
    total_due = order.get("totalDue")
    if total_due is not None:
        lines.append({
            "text": f"{L['TOTAL']} {_money(order, total_due)}",
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })
    mark_block(block_start, "totals")

    # ══════════════════════════════════════════════════════════════════
    # 14. Payment section (final receipts only)
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    if is_final:
        # Individual payment lines (show payment method name instead of PAID label)
        payment_lines = order.get("payment_lines") or order.get("statement_ids") or []
        has_payment_line = False
        for pl in payment_lines:
            if isinstance(pl, dict):
                pname = _text(pl.get("name") or pl.get("payment_method_id"))
                pamount = pl.get("amount")
                if pamount:
                    lines.append({
                        "type": "header_meta_line",
                        "left_text": pname or "Pago",
                        "right_text": _money(order, pamount),
                    })
                    has_payment_line = True

        # If no individual payment lines, show the total paid amount with a generic label
        amount_paid = order.get("amountPaid")
        if amount_paid is not None and not has_payment_line:
            lines.append({
                "type": "header_meta_line",
                "left_text": L["PAID"],
                "right_text": _money(order, amount_paid),
            })

        # Change calculation (only show if there is change)
        if amount_paid is not None:
            total_val = _decimal(total_due or 0)
            paid_val = _decimal(amount_paid)
            change = max(Decimal("0"), paid_val - total_val)
            if change > 0:
                lines.append({
                    "type": "header_meta_line",
                    "left_text": L["CHANGE"],
                    "right_text": _money(order, change),
                })
    mark_block(block_start, "payments")

    # ══════════════════════════════════════════════════════════════════
    # Redsys / payment-terminal card transaction receipt
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.extend(build_payment_terminal_lines(order))
    mark_block(block_start, "redsys")

    # ══════════════════════════════════════════════════════════════════
    # 15. Coupons generated or activated by Odoo loyalty programs
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.extend(build_coupon_lines(order))
    mark_block(block_start, "coupons")

    # ══════════════════════════════════════════════════════════════════
    # 16. Gift cards and eWallet balances
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.extend(build_voucher_lines(order))
    mark_block(block_start, "vouchers")

    # ══════════════════════════════════════════════════════════════════
    # 17. Loyalty membership points (never currency)
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    lines.extend(build_loyalty_lines(order))
    mark_block(block_start, "loyalty")

    # ══════════════════════════════════════════════════════════════════
    # 17. QR + Portal URL
    # ══════════════════════════════════════════════════════════════════
    invoice_qr_lines = []
    if is_final:
        qr_src = _ticket_qr_src(order)
        portal = _portal_url(order)
        ticket_code = _text(order.get("ticket_code"))
        if not ticket_code:
            # Fallback so "Code:" still prints for orders created without a
            # ticket code (e.g. via the Movi API).  Uses the trailing digits
            # of the receipt reference (max 5), matching Odoo's 5-char code.
            ticket_code = _ticket_code_fallback(order)
        if qr_src or portal or ticket_code:
            invoice_qr_lines.append({"text": "", "align": "left"})
            # Same prompt the native POS shows above the invoice QR code.
            invoice_qr_lines.append({
                "text": "NECESITA UNA FACTURA",
                "align": "center", "bold": True,
                "classes": ["portal-title"],
            })
        if qr_src:
            invoice_qr_lines.append({
                "type": "image", "src": qr_src, "align": "center",
                "classes": ["portal-qr"],
                "width": 180, "height": 180, "image_kind": "qr",
            })
        url_mode = _text(order.get("company", {}).get("point_of_sale_ticket_portal_url_display_mode"))
        if portal:
            invoice_qr_lines.append({"text": portal, "align": "center", "classes": ["portal-url"]})
        if ticket_code:
            invoice_qr_lines.append({
                "text": f"{L['CODE']}: {ticket_code}",
                "align": "center", "classes": ["unique-code"],
            })
            invoice_qr_lines.append({
                "text": "", "align": "left", "classes": ["receipt-spacer", "after-unique-code"],
            })

    # ══════════════════════════════════════════════════════════════════
    # 17. Footer
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    footer = _text(config.get("receipt_footer"))
    if footer:
        for fl in footer.split("\n"):
            fl = fl.strip()
            if fl:
                lines.append({"text": fl, "align": "center", "classes": ["pos-config-name"]})
    mark_block(block_start, "footer")

    # ══════════════════════════════════════════════════════════════════
    # 18. Takeout/Delivery info — LARGE, at the very bottom for tear-off
    # ══════════════════════════════════════════════════════════════════
    block_start = len(lines)
    chino_order_type = _text(order.get("chino_order_type"))
    if chino_order_type == "DELIVERY":
        chino_floating = _text(order.get("chino_floating_order_name"))

        # Blank lines so this section can be torn off
        for _ in range(4):
            lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})

        lines.append({"text": SEPARATOR, "align": "left"})
        # 1. Order type + number (only show number for takeout, not delivery phone)
        lines.append({
            "text": chino_order_type,
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })
        if chino_floating and not re.match(r"^\d{9}$", chino_floating):
            lines.append({
                "text": chino_floating,
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })
        # 2. Blank line
        lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})
        # 3. Phone + 4. Address — smaller font
        partner = order.get("partner_id", {})
        if isinstance(partner, dict):
            phone = _text(partner.get("phone") or partner.get("mobile"))
            if phone:
                lines.append({"text": phone, "align": "center", "bold": True})
            address = _text(partner.get("pos_contact_address") or partner.get("street"))
            if address:
                lines.append({"text": address, "align": "center", "bold": True})
        # 5. Blank line
        lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})
        # 6. Total
        total_due = order.get("totalDue")
        if total_due is not None:
            amt = _decimal(total_due)
            lines.append({
                "text": f"TOTAL: {amt:.2f} €",
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })
        lines.append({"text": SEPARATOR, "align": "left"})
    mark_block(block_start, "delivery")

    # Keep the invoice QR and related data as the final receipt content.
    if invoice_qr_lines:
        for invoice_qr_line in invoice_qr_lines:
            invoice_qr_line["_template_block"] = "qr"
        lines.extend(invoice_qr_lines)

    _logger.info(
        "Built receipt lines lang=%s lines=%s is_final=%s discount_total=%s",
        lang, len(lines), is_final, float(discount_total),
    )
    if preview_fields:
        lines = _replace_with_field_placeholders(lines)
    from .receipt_template_store import apply_template

    return apply_template(lines, template)


def _replace_with_field_placeholders(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace Odoo values with their source field names for the editor preview."""
    placeholders: dict[str, list[dict[str, Any]]] = {
        "logo": [{
            "type": "image", "src": "{{ config.receiptLogoUrl }}", "align": "center",
            "image_kind": "logo", "classes": ["pos-receipt-logo"],
        }],
        "company": [
            {"text": "{{ company.name }}", "align": "center", "bold": True},
            {"text": "{{ company.street }}", "align": "center"},
            {"text": "{{ company.zip }} {{ company.city }}", "align": "center"},
            {"text": "{{ company.country_id }}", "align": "center"},
            {"text": "{{ company.phone }}", "align": "center"},
        ],
        "customer": [
            {"text": "{{ partner_id.parent_name }} / {{ partner_id.name }}", "align": "center", "bold": True,
             "classes": ["customer-info", "customer-name"]},
            {"text": "{{ partner_id.vat }}", "align": "center",
             "classes": ["customer-info", "customer-vat"]},
            {"text": "{{ partner_id.pos_contact_address }}", "align": "center",
             "classes": ["customer-info", "customer-address"]},
            {"text": "{{ partner_id.street }} / {{ partner_id.street2 }}", "align": "center",
             "classes": ["customer-info", "customer-address-fields"]},
            {"text": "{{ partner_id.zip }} {{ partner_id.city }} {{ partner_id.state_id.name }}", "align": "center",
             "classes": ["customer-info", "customer-region"]},
            {"text": "{{ partner_id.country_id.name }}", "align": "center",
             "classes": ["customer-info", "customer-country"]},
            {"text": "{{ partner_id.phone }} / {{ partner_id.mobile }}", "align": "center",
             "classes": ["customer-info", "customer-phone"]},
            {"text": "{{ partner_id.email }}", "align": "center",
             "classes": ["customer-info", "customer-email"]},
        ],
        "table": [{
            "text": "MESA {{ table_id.table_number }}", "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        }],
        "invoice": [
            {"text": "**************************", "align": "center"},
            {"text": "Factura Simplificada", "align": "center", "bold": True},
            {"text": "Fs/{{ date_order.year }}/{{ name.sequence }}", "align": "center"},
            {"text": "**************************", "align": "center"},
        ],
        "order_info": [
            {"text": "PEDIDO {{ pos_reference }}", "align": "center", "bold": True},
            {
                "type": "image",
                "src": "{{ pos_reference | barcode('Code128') }}",
                "align": "center",
                "image_kind": "barcode",
                "barcode_type": "Code128",
                "barcode_value": "{{ pos_reference }}",
                "classes": ["receipt-order-barcode", "no-barcode-separator"],
            },
            {"text": "{{ date_order }} | {{ user_id.name }}", "align": "center"},
        ],
        "products": [{
            "type": "product_line",
            "qty": "qty",
            "name": "full_product_name",
            "unit_price": "unit_price",
            "total": "price_subtotal_incl",
            "discount_text": "discount%",
            "original_total": "price_without_discount",
            "combo_items": ["orderDisplayProductName.attributeString"],
        }],
        "promotions": [
            {"text": "{{ lines[].reward_id.name }}", "align": "left", "bold": True,
             "classes": ["promotion-title"]},
            {"text": "{{ lines[].reward_id.program_id.name }}", "align": "left",
             "classes": ["promotion-program"]},
            {"text": "{{ lines[].reward_id.reward_type }} · {{ lines[].points_cost }} pts",
             "align": "left", "classes": ["promotion-detail"]},
            {"text": "{{ lines[].reward_identifier_code }}", "align": "left",
             "classes": ["promotion-code"]},
        ],
        "totals": [
            {"type": "header_meta_line", "left_text": "Subtotal", "right_text": "{{ subtotal }}"},
            {"type": "header_meta_line", "left_text": "Descuento", "right_text": "{{ discount_total }}"},
            {"type": "header_meta_line", "left_text": "{{ tax_names[] }}", "right_text": "{{ amountTaxes }}"},
            {"text": "TOTAL {{ totalDue }}", "align": "center", "bold": True,
             "double_width": True, "double_height": True},
        ],
        "payments": [
            {"type": "header_meta_line", "left_text": "{{ payment_lines[].name }}",
             "right_text": "{{ payment_lines[].amount }}"},
            {"type": "header_meta_line", "left_text": "Cambio", "right_text": "{{ change }}"},
        ],
        "redsys": [
            {"type": "image", "src": "/assets/nfc_override.png", "align": "center", "width": 80,
             "image_kind": "logo",
             "classes": ["payment-terminal-logo", "payment-terminal-nfc-icon", "redsys-nfc-logo"]},
            {"text": "{{ payment_terminal_receipts[].lines[] }}", "align": "center",
             "classes": ["payment-terminal-line", "pos-payment-terminal-receipt", "redsys-receipt-line"]},
            {"text": "{{ payment_lines[].payment_terminal_receipt }}", "align": "center",
             "classes": ["payment-terminal-line", "pos-payment-terminal-receipt", "redsys-receipt-line"]},
            {"text": "{{ payment_lines[].card_type }} / {{ payment_lines[].card_number }}", "align": "center",
             "classes": ["redsys-card-field"]},
            {"text": "{{ payment_lines[].authorization_code }} / {{ payment_lines[].transaction_id }}", "align": "center",
             "classes": ["redsys-transaction-field"]},
            {"text": "{{ payment_lines[].terminal_id }} / {{ payment_lines[].merchant_id }}", "align": "center",
             "classes": ["redsys-terminal-field"]},
        ],
        "coupons": [
            {"type": "spacer", "align": "left", "classes": ["coupon-spacer"]},
            {"text": "{{ new_coupon_info[].program_name }}", "align": "center", "bold": True,
             "classes": ["coupon-title"]},
            {"text": "{{ new_coupon_info[].code }}", "align": "center",
             "classes": ["coupon-code"]},
            {"type": "image", "src": "{{ new_coupon_info[].code | barcode('Code128') }}",
             "barcode_value": "{{ new_coupon_info[].code }}", "align": "center",
             "image_kind": "barcode", "classes": ["coupon-barcode"]},
            {"text": "{{ new_coupon_info[].expiration_date }}", "align": "center",
             "classes": ["coupon-expiration"]},
            {"text": "{{ coupons[].program_type }}", "align": "center",
             "classes": ["coupon-program-type-field"]},
        ],
        "vouchers": [
            {"type": "spacer", "align": "left", "classes": ["gift-card-spacer"]},
            {"text": "{{ loyalty_cards[].name }}", "align": "center", "bold": True,
             "classes": ["gift-card-title"]},
            {"text": "{{ loyalty_cards[].code }}", "align": "center",
             "classes": ["gift-card-code"]},
            {"type": "image", "src": "{{ loyalty_cards[].code | barcode('Code128') }}",
             "barcode_value": "{{ loyalty_cards[].code }}", "align": "center",
             "image_kind": "barcode", "classes": ["gift-card-barcode"]},
            {"text": "{{ loyalty_cards[].balance }}", "align": "center", "bold": True,
             "double_width": True, "classes": ["gift-card-amount"]},
            {"text": "{{ loyalty_cards[].point }}", "align": "center",
             "classes": ["gift-card-point-field"]},
            {"text": "{{ loyalty_cards[].qrSrc }}", "align": "center",
             "classes": ["gift-card-qr-field"]},
            {"text": "{{ loyalty_cards[].program_type }}", "align": "center",
             "classes": ["gift-card-program-type-field"]},
        ],
        "loyalty": [
            {"type": "header_meta_line", "left_text": "{{ loyalty_points[].points.name }} Ganados",
             "right_text": "{{ loyalty_points[].points.won }}", "classes": ["loyalty-points", "loyalty-won"]},
            {"type": "header_meta_line", "left_text": "{{ loyalty_points[].points.name }} Utilizados",
             "right_text": "{{ loyalty_points[].points.spent }}", "classes": ["loyalty-points", "loyalty-spent"]},
            {"type": "header_meta_line", "left_text": "Saldo {{ loyalty_points[].points.name }}",
             "right_text": "{{ loyalty_points[].points.balance }}", "classes": ["loyalty-points", "loyalty-balance"]},
            {"text": "{{ loyalty_points[].points.total }}", "align": "right",
             "classes": ["loyalty-total-field"]},
        ],
        "footer": [{"text": "{{ config.receipt_footer }}", "align": "center"}],
        "delivery": [
            {"text": "{{ chino_order_type }}", "align": "center", "bold": True,
             "double_width": True, "double_height": True},
            {"text": "{{ partner_id.phone }}", "align": "center"},
            {"text": "{{ partner_id.pos_contact_address }}", "align": "center"},
        ],
        "portal_prompt": [
            {"text": "{{ portal_title }}", "align": "center", "bold": True,
             "classes": ["portal-title"]},
        ],
        "qr": [
            {"type": "image", "src": "{{ ticket_qr_url }}", "align": "center",
             "image_kind": "qr", "classes": ["portal-qr"]},
            {"text": "{{ portal_url }}", "align": "center"},
            {"text": "Código: {{ ticket_code }}", "align": "center"},
        ],
    }
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for line in lines:
        block_id = str(line.get("_template_block") or "")
        if block_id not in placeholders:
            result.append(line)
            continue
        if block_id in seen:
            continue
        seen.add(block_id)
        for placeholder in placeholders[block_id]:
            result.append({**placeholder, "_template_block": block_id})
    for block_id, block_lines in placeholders.items():
        if block_id in seen:
            continue
        for placeholder in block_lines:
            result.append({**placeholder, "_template_block": block_id})
    return result


# ── public dispatch ───────────────────────────────────────────────────

def build_lines(order: dict[str, Any], kitchen: bool = False) -> list[dict[str, Any]]:
    """Dispatch to the appropriate builder based on ticket type."""
    if kitchen:
        return build_kitchen_ticket_lines(order)
    return build_receipt_lines(order)
