from __future__ import annotations

import re
from typing import Any


def option_parts(item: Any, fallback_qty: str = "") -> tuple[str, str, str]:
    """Return ``(quantity, name, unit price)`` for a product option."""
    if isinstance(item, dict):
        name = _first_text(item, "name", "display_name", "label", "value", "attribute_name")
        quantity = _first_text(item, "qty", "quantity", "count", "attribute_qty", "attribute_quantity")
        price = _first_text(item, "unit_price", "price", "price_extra", "extra_price", "attribute_price")
        return quantity or str(fallback_qty or "").strip(), name, price

    text = str(item or "").strip()
    price = ""
    price_match = re.search(r"\s*[（(]\s*\+?\s*([^()（）]+?)\s*[）)]\s*$", text)
    if price_match:
        price = price_match.group(1).strip()
        text = text[: price_match.start()].strip()

    prefix_match = re.match(r"^\+?\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(.+)$", text)
    if prefix_match:
        return _clean_number(prefix_match.group(1)), prefix_match.group(2).strip(), price

    suffix_match = re.search(r"\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*$", text)
    if suffix_match:
        name = text[: suffix_match.start()].strip()
        return _clean_number(suffix_match.group(1)), name, price

    return str(fallback_qty or "").strip(), text, price


def format_option(item: Any, fallback_qty: str = "") -> str:
    quantity, name, price = option_parts(item, fallback_qty=fallback_qty)
    if not name:
        return ""
    # A single selected attribute is the normal case; printing ``1 X`` on
    # every option makes multi-attribute products needlessly noisy. Keep the
    # quantity only when it conveys additional information (including 0.5).
    prefix = f"+ {quantity} X " if quantity and not _is_one(quantity) else "+ "
    price_suffix = f" (+{price})" if price else ""
    return f"{prefix}{name}{price_suffix}"


def option_label(item: Any, fallback_qty: str = "") -> str:
    """Return an option without its price, for tickets that show no amounts.

    The kitchen ticket lists what to cook, not what it costs, so an attribute
    price must never reach it -- neither from a structured option
    (``{"name": ..., "unit_price": ...}``) nor from a label that already
    carries the amount (``"Extra queso (+1,50 €)"``).
    """
    if isinstance(item, dict):
        quantity, name, _price = option_parts(item, fallback_qty=fallback_qty)
        if not name:
            return ""
        # Same rule as format_option: a plain 1 adds nothing to the kitchen.
        return f"{quantity} X {name}" if quantity and not _is_one(quantity) else name
    cleaned = strip_option_price(item)
    prefix_match = re.match(r"^\+?\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(.+)$", cleaned)
    if prefix_match:
        quantity = _clean_number(prefix_match.group(1))
        name = prefix_match.group(2).strip()
        return f"{quantity} X {name}" if not _is_one(quantity) else name
    return cleaned


def strip_option_price(text: Any) -> str:
    """Drop a trailing price such as ``(+1,50 €)`` from an option label."""
    value = str(text or "").strip()
    match = _TRAILING_PARENS.search(value)
    if match and _looks_like_price(match.group(1)):
        return value[: match.start()].strip()
    return value


# A price is only ever stripped when the parenthesised tail really reads as
# money, so a qualifier such as "(sin gluten)" survives untouched.
_TRAILING_PARENS = re.compile(r"\s*[（(]\s*\+?\s*([^()（）]*)\s*[）)]\s*$")
_PRICE_MARKERS = ("€", "$", "¥", "eur", "usd", "元")


def _looks_like_price(value: str) -> bool:
    text = str(value or "").strip()
    if not text or not any(char.isdigit() for char in text):
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _PRICE_MARKERS):
        return True
    return bool(re.fullmatch(r"\+?\s*\d+(?:[.,]\d+)?", text))


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _clean_number(value: str) -> str:
    raw = str(value or "").strip().replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return raw
    return str(int(number)) if number.is_integer() else raw


def _is_one(value: str) -> bool:
    try:
        return float(str(value).strip().replace(",", ".")) == 1
    except ValueError:
        return False
