"""Validated, file-backed configuration for the visual receipt editor."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import unicodedata
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .printing.product_options import format_option


BLOCKS = (
    ("portal_prompt", "发票提示（系统语言）"),
    ("logo", "Logo"),
    ("company", "商家信息"),
    ("customer", "客户信息 (Información del cliente)"),
    ("table", "桌号"),
    ("invoice", "简化发票"),
    ("tracking", "取餐号"),
    ("order_info", "订单信息"),
    ("product_header", "商品表头"),
    ("products", "商品明细"),
    ("promotions", "促销 / 奖励 (Promociones)"),
    ("totals", "金额合计"),
    ("payments", "付款信息"),
    ("redsys", "Redsys 刷卡记录"),
    ("coupons", "优惠券 (Cupones)"),
    ("vouchers", "礼品卡 / 电子钱包 (Tarjeta de regalo / eWallet)"),
    ("loyalty", "会员积分 (Programa de fidelización)"),
    ("footer", "页脚文字"),
    ("delivery", "外卖信息"),
    ("qr", "二维码 / 票据码"),
)
BLOCK_IDS = {key for key, _ in BLOCKS}
ALIGNS = {"inherit", "left", "center", "right"}
CUSTOM_KINDS = {"text", "separator", "spacer"}
CUSTOM_ID = re.compile(r"^custom_[a-z0-9_-]{4,64}$")
CONTENT_OVERRIDE_BLOCKS = {"company", "invoice", "order_info", "product_header", "footer"}
# Font size is a glyph *height* multiplier: 1 = 标准, 2 = 中号, 3 = 大号,
# 4 = 特大, 5 = 超大.  The editor's dropdowns offer exactly this range.  ESC/POS
# allows up to 8, but a single-width glyph taller than this stops looking like
# text.  Keep in sync with the <select> options in web/index.html.
FONT_SIZE_CHOICES = (1, 2, 3, 4, 5)
MAX_FONT_SIZE = max(FONT_SIZE_CHOICES)


def font_size_multipliers(font_size: Any) -> tuple[int, int]:
    """Map a font-size choice to (width, height) glyph multipliers.

    Height carries the size; width follows at half of it, rounding up.  Width is
    what costs characters per line, so it only steps up every second level --
    a level-5 glyph grows to 3x5 instead of 5x5, which keeps roughly twice as
    many characters on the line for the same height.
    """
    try:
        size = max(1, min(MAX_FONT_SIZE, int(font_size or 1)))
    except (TypeError, ValueError):
        size = 1
    return -(-size // 2), size
_logger = logging.getLogger(__name__)
_template_lock = threading.RLock()


def _canonical_default_template() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "默认结账小票",
        "paper_width": 48,
        "blocks": [
            {
                "id": key,
                "kind": "builtin",
                "label": label,
                "enabled": True,
                "align": "inherit",
                "bold": "inherit",
                "horizontal_offset": 0,
                "spacing_after": 0,
                "content": "",
            }
            for key, label in BLOCKS
        ],
    }


def default_template() -> dict[str, Any]:
    """Return the single customer-receipt template, with a safe fallback."""
    # The configured path is the editable per-installation override.  Defaults
    # must always come from the bundled resource, otherwise a new (empty)
    # override path makes rendering silently fall back to a different layout.
    resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", Path(__file__).resolve().parent.parent))
    bundled_path = resource_dir / "receipt_template.json"
    try:
        return validate_template(json.loads(bundled_path.read_text(encoding="utf-8")))
    except Exception:
        _logger.exception("Invalid bundled receipt default at %s; using canonical layout", bundled_path)
        return _canonical_default_template()


def template_path() -> Path:
    configured = os.getenv("IOT_RECEIPT_TEMPLATE_PATH", "").strip()
    if configured:
        return Path(configured)
    resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", Path(__file__).resolve().parent.parent))
    return resource_dir / "receipt_template.json"


def validate_template(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("模板必须是 JSON 对象")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("模板缺少 blocks 列表")

    seen: set[str] = set()
    blocks: list[dict[str, Any]] = []
    labels = dict(BLOCKS)
    labels["qr"] = "二维码 / 发票提示 / 票据码"
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ValueError("每个模板区块必须是对象")
        block_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or ("builtin" if block_id in BLOCK_IDS else ""))
        is_builtin = kind == "builtin" and block_id in BLOCK_IDS
        is_custom = kind in CUSTOM_KINDS and bool(CUSTOM_ID.fullmatch(block_id))
        if (not is_builtin and not is_custom) or block_id in seen:
            raise ValueError(f"未知或重复的小票区块: {block_id or '<空>'}")
        align = str(raw.get("align") or "inherit")
        if align not in ALIGNS:
            raise ValueError(f"无效的对齐方式: {align}")
        bold_value = raw.get("bold", "inherit")
        if bold_value not in (True, False, "inherit"):
            raise ValueError("bold 必须是 true、false 或 inherit")
        try:
            spacing = max(0, min(4, int(raw.get("spacing_after") or 0)))
        except (TypeError, ValueError):
            spacing = 0
        try:
            horizontal_offset = max(-12, min(12, int(raw.get("horizontal_offset") or 0)))
        except (TypeError, ValueError):
            horizontal_offset = 0
        block = {
            "id": block_id,
            "kind": kind,
            "label": labels[block_id] if is_builtin else str(raw.get("label") or "自定义区块")[:40],
            "enabled": bool(raw.get("enabled", True)),
            "align": align,
            "bold": bold_value,
            "horizontal_offset": horizontal_offset,
            "spacing_after": spacing,
            "separator_after": bool(raw.get("separator_after", False)),
            "separator_after_character": (
                str(raw.get("separator_after_character") or "-")[:1]
                if str(raw.get("separator_after_character") or "-")[:1] in {"-", "=", "*"}
                else "-"
            ),
            "blank_line_after": bool(raw.get("blank_line_after", block_id == "footer")),
        }
        if is_builtin:
            content = str(raw.get("content") or "")[:1000]
            block["content"] = content if block_id in CONTENT_OVERRIDE_BLOCKS else ""
            if block_id == "tracking":
                block["double_size"] = bool(raw.get("double_size", False))
                try:
                    block["font_size"] = max(1, min(MAX_FONT_SIZE, int(raw.get("font_size") or 1)))
                except (TypeError, ValueError):
                    block["font_size"] = 1
            elif block_id == "table":
                block["tracking_double_size"] = bool(raw.get("tracking_double_size", False))
                # The table line has always been emitted at double size by the
                # builder; now that font_size drives it, default to 2 so the
                # printed result does not change for existing templates.
                try:
                    block["font_size"] = max(1, min(MAX_FONT_SIZE, int(raw.get("font_size") or 2)))
                except (TypeError, ValueError):
                    block["font_size"] = 2
            elif block_id == "products":
                try:
                    block["font_size"] = max(1, min(MAX_FONT_SIZE, int(raw.get("font_size") or 1)))
                except (TypeError, ValueError):
                    block["font_size"] = 1
            if block_id == "product_header":
                try:
                    qty_columns = max(5, min(12, int(raw.get("qty_columns") or 6)))
                except (TypeError, ValueError):
                    qty_columns = 6
                try:
                    amount_columns = max(8, min(16, int(raw.get("amount_columns") or 10)))
                except (TypeError, ValueError):
                    amount_columns = 10
                try:
                    product_columns = max(12, min(32, int(raw.get("product_columns") or 30)))
                except (TypeError, ValueError):
                    product_columns = 30
                default_gutter = max(0, 48 - qty_columns - product_columns - amount_columns)
                try:
                    raw_gutter = raw.get("gutter_columns", default_gutter)
                    gutter_columns = max(0, min(12, int(raw_gutter)))
                except (TypeError, ValueError):
                    gutter_columns = default_gutter
                # The configured columns may use fewer than 48 characters;
                # any remainder stays after the amount column instead of
                # being silently added back into the product/amount gutter.
                gutter_columns = min(gutter_columns, max(0, 48 - qty_columns - amount_columns - 12))
                product_columns = min(product_columns, 48 - qty_columns - gutter_columns - amount_columns)
                block.update({
                    "qty_label": str(raw.get("qty_label") or "Uds.")[:12],
                    "product_label": str(raw.get("product_label") or "Producto")[:32],
                    "amount_label": str(raw.get("amount_label") or "Importe")[:16],
                    "qty_columns": qty_columns,
                    "product_columns": product_columns,
                    "amount_columns": amount_columns,
                    "gutter_columns": gutter_columns,
                })
        elif kind == "text":
            block["text"] = str(raw.get("text") or "")[:1000]
            block["double_size"] = bool(raw.get("double_size", False))
        elif kind == "separator":
            character = str(raw.get("character") or "-")[:1]
            block["character"] = character if character in {"-", "=", "*", "·"} else "-"
        elif kind == "spacer":
            try:
                block["lines"] = max(1, min(6, int(raw.get("lines") or 1)))
            except (TypeError, ValueError):
                block["lines"] = 1
        blocks.append(block)
        seen.add(block_id)

    canonical_position = {block_id: index for index, (block_id, _) in enumerate(BLOCKS)}
    for block_id, label in BLOCKS:
        if block_id not in seen:
            new_block = {
                "id": block_id, "label": label, "enabled": True,
                "kind": "builtin", "align": "inherit", "bold": "inherit",
                "horizontal_offset": 0, "spacing_after": 0, "content": "",
            }
            if block_id == "product_header":
                new_block.update({
                    "qty_label": "Uds.", "product_label": "Producto", "amount_label": "Importe",
                    "qty_columns": 6, "product_columns": 30, "amount_columns": 10,
                    "gutter_columns": 2,
                })
            elif block_id == "products":
                new_block["font_size"] = 1
            insert_at = next(
                (
                    index for index, existing in enumerate(blocks)
                    if existing["id"] in canonical_position
                    and canonical_position[existing["id"]] > canonical_position[block_id]
                ),
                len(blocks),
            )
            blocks.insert(insert_at, new_block)

    # The invoice prompt is a native portal field and belongs immediately
    # before the QR/url/code group in the visual editor and on the receipt.
    portal_prompt = next((block for block in blocks if block["id"] == "portal_prompt"), None)
    if portal_prompt is not None:
        blocks = [block for block in blocks if block["id"] != "portal_prompt"]
        qr_index = next((index for index, block in enumerate(blocks) if block["id"] == "qr"), len(blocks))
        blocks.insert(qr_index, portal_prompt)

    # A simplified invoice identifies the sale, so it must print before the
    # pickup number even for templates saved before this layout rule existed.
    invoice_index = next((index for index, block in enumerate(blocks) if block["id"] == "invoice"), None)
    tracking_index = next((index for index, block in enumerate(blocks) if block["id"] == "tracking"), None)
    if invoice_index is not None and tracking_index is not None and invoice_index > tracking_index:
        invoice = blocks.pop(invoice_index)
        blocks.insert(tracking_index, invoice)

    # This runtime has one physical receipt profile: 80 mm / 48 characters.
    width = 48
    return {
        "version": 1,
        "name": str(payload.get("name") or "自定义小票")[:80],
        "paper_width": width,
        "blocks": blocks,
    }


def load_template() -> dict[str, Any]:
    path = template_path()
    with _template_lock:
        if not path.exists():
            return default_template()
        try:
            return validate_template(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            _logger.exception("Invalid receipt template at %s; using defaults without overwriting it", path)
            return default_template()


def save_template(payload: Any) -> dict[str, Any]:
    template = validate_template(payload)
    path = template_path()
    with _template_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return deepcopy(template)


def reset_template() -> dict[str, Any]:
    return save_template(default_template())


def apply_template(lines: list[dict[str, Any]], template: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Order, hide and style tagged receipt lines using a validated template."""
    selected = validate_template(template or load_template())
    product_header = next(
        (block for block in selected["blocks"] if block["id"] == "product_header"),
        {"qty_columns": 6, "product_columns": 30, "amount_columns": 10, "gutter_columns": 2},
    )
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in BLOCK_IDS}
    untagged: list[dict[str, Any]] = []
    for source in lines:
        line = dict(source)
        if str(line.get("text") or "").strip().upper().startswith("METHOD:"):
            continue
        block_id = str(line.pop("_template_block", ""))
        if block_id in grouped:
            grouped[block_id].append(line)
        else:
            untagged.append(line)

    # The visual template is authoritative.  Never prepend untagged legacy
    # lines: doing so lets fields such as METHOD leak between template blocks.
    # Every printable row must belong to an explicit template block.
    result: list[dict[str, Any]] = []
    for block in selected["blocks"]:
        if not block["enabled"]:
            continue
        kind = block.get("kind", "builtin")
        if kind == "text":
            block_lines = [
                {
                    "text": text,
                    "align": "left" if block["align"] == "inherit" else block["align"],
                    "bold": block["bold"] is True,
                    "double_width": block.get("double_size", False),
                    "double_height": block.get("double_size", False),
                    "classes": ["template-custom-text"],
                }
                for text in str(block.get("text") or "").splitlines()
            ]
        elif kind == "separator":
            block_lines = [{
                "text": str(block.get("character") or "-") * selected["paper_width"],
                "align": "left", "classes": ["template-custom-separator"],
            }]
        elif kind == "spacer":
            block_lines = [
                {"type": "spacer", "align": "left", "classes": ["template-custom-spacer"]}
                for _ in range(int(block.get("lines") or 1))
            ]
        elif block.get("content"):
            block_lines = [
                {"text": text, "align": "center", "classes": ["template-content-override"]}
                for text in str(block["content"]).splitlines()
            ]
        else:
            block_lines = grouped[block["id"]]
        rendered_lines: list[dict[str, Any]] = []
        for line in block_lines:
            if line.get("type") == "product_header":
                line = _render_product_header(line, selected["paper_width"], block)
            if line.get("type") == "product_line":
                # The product columns must be laid out for the width the glyphs
                # will actually occupy, or the row overflows the paper.
                rendered_lines.extend(
                    _render_product_lines(
                        line,
                        product_header,
                        selected["paper_width"],
                        font_size_multipliers(block.get("font_size") if block["id"] == "products" else 1)[0],
                    )
                )
            else:
                rendered_lines.append(line)
        for line in rendered_lines:
            # A larger size grows the glyph mostly upwards; the width steps up
            # only every second level (see font_size_multipliers).
            if block["id"] == "tracking" and "tracking-info" in {
                str(value) for value in line.get("classes") or []
            }:
                size = int(block.get("font_size") or (2 if block.get("double_size") else 1))
                width, height = font_size_multipliers(size)
                line["width_multiplier"] = width
                line["height_multiplier"] = height
                line["double_width"] = width > 1
                line["double_height"] = height > 1
            if block["id"] == "products":
                width, height = font_size_multipliers(block.get("font_size"))
                line["width_multiplier"] = width
                line["height_multiplier"] = height
                line["double_width"] = width > 1
                line["double_height"] = height > 1
            if block["id"] == "table" and str(line.get("text") or "").strip():
                # The size the editor's "取餐号双倍字号" toggle controls.  Skipped
                # for the trailing spacer so it cannot grow the paper feed.
                width, height = font_size_multipliers(block.get("font_size") or 2)
                line["width_multiplier"] = width
                line["height_multiplier"] = height
                line["double_width"] = width > 1
                line["double_height"] = height > 1
            if block["align"] != "inherit" and line.get("type") not in {"product_line", "header_meta_line"}:
                line["align"] = block["align"]
            if block["bold"] != "inherit":
                line["bold"] = block["bold"]
            _apply_horizontal_offset(line, block["horizontal_offset"], selected["paper_width"])
            result.append(line)
        if block_lines:
            for _ in range(block["spacing_after"]):
                result.append({"type": "spacer", "align": "left", "classes": ["template-spacing"]})
            # Independent of separator_after, matching the two separate toggles in
            # the editor: "Blank line after block" must work on its own.
            if block.get("separator_after"):
                result.append({
                    "text": block.get("separator_after_character", "-") * selected["paper_width"],
                    "align": "left",
                    "classes": ["template-block-separator"],
                })
            if block.get("blank_line_after"):
                result.append({"type": "spacer", "align": "left", "classes": ["template-block-blank-line"]})
    return result


def _render_product_header(line: dict[str, Any], width: int, block: dict[str, Any]) -> dict[str, Any]:
    qty = str(block.get("qty_label") or line.get("qty_label") or "Uds.")
    product = str(block.get("product_label") or line.get("product_label") or "Producto")
    amount = str(block.get("amount_label") or line.get("amount_label") or "Importe")
    qty_width = int(block.get("qty_columns") or 6)
    amount_width = int(block.get("amount_columns") or 10)
    product_width = int(block.get("product_columns") or 30)
    gutter_width = max(0, int(block.get("gutter_columns", width - qty_width - product_width - amount_width)))
    text = (
        _pad_right(qty, qty_width)
        + _pad_right(product, product_width)
        + (" " * gutter_width)
        + _pad_left(amount, amount_width)
    )
    return {
        "text": _pad_right(text, width),
        "align": "left",
        "bold": bool(line.get("bold", True)),
        "classes": ["receipt-product-header"],
    }


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
        qty_number = Decimal(qty_raw)
        if qty_number == qty_number.to_integral_value():
            qty = str(int(qty_number))
    except (InvalidOperation, ValueError):
        qty = qty_raw
    return qty, attr_name


def _render_product_lines(
    line: dict[str, Any], layout: dict[str, Any], width: int, width_multiplier: int = 1,
) -> list[dict[str, Any]]:
    """Lay out a product row for glyphs ``width_multiplier`` times as wide.

    ``width_multiplier`` is the ESC/POS width multiplier, not the font-size
    level: see ``font_size_multipliers``.
    """
    width_multiplier = max(1, min(3, int(width_multiplier or 1)))
    effective_width = max(16, width // width_multiplier)
    qty_width = int(layout.get("qty_columns") or 6)
    amount_width = int(layout.get("amount_columns") or 10)
    product_width = int(layout.get("product_columns") or 30)
    gutter_width = max(0, int(layout.get("gutter_columns", width - qty_width - product_width - amount_width)))
    if width_multiplier > 1:
        qty_width = max(3, min(4, (qty_width + width_multiplier - 1) // width_multiplier))
        amount_width = min(10, max(9, (amount_width + width_multiplier - 1) // width_multiplier))
        gutter_width = 1
        product_width = max(4, effective_width - qty_width - gutter_width - amount_width)
    qty = str(line.get("qty") or "")

    combo_items = [item for item in (line.get("combo_items") or []) if item not in (None, "")]

    name_parts = _wrap_cells(str(line.get("name") or ""), product_width) or [""]
    amount = str(line.get("total") or "")
    amount_parts = _wrap_cells(amount, amount_width) or [""]
    rows: list[dict[str, Any]] = []
    row_count = max(len(name_parts), len(amount_parts))
    for index in range(row_count):
        name_part = name_parts[index] if index < len(name_parts) else ""
        amount_part = amount_parts[index] if index < len(amount_parts) else ""
        text = (
            _pad_right(qty if index == 0 else "", qty_width)
            + _pad_right(name_part, product_width)
            + (" " * gutter_width)
            + _pad_left(amount_part, amount_width)
        )
        rows.append({
            "text": _pad_right(text, effective_width),
            "align": "left",
            "bold": bool(line.get("bold", False)),
            "classes": ["receipt-product-row"],
        })
    for option in combo_items:
        option_text = format_option(option, fallback_qty=qty)
        option_width = product_width if width_multiplier == 1 else effective_width - qty_width
        for option_part in _wrap_cells(option_text, option_width):
            rows.append({
                "text": _pad_right((
                    _pad_right("", qty_width) + _pad_right(option_part, option_width)
                ), effective_width),
                "align": "left",
                # Combo (套餐) children — including their quantity suffix — are
                # printed bold to stand out on the receipt.
                "bold": True,
                "classes": ["receipt-product-option-row"],
            })
    discount = str(line.get("discount_text") or "").strip()
    if discount:
        original_total = _spanish_decimal_money(str(line.get("original_total") or "").strip())
        discount_text = (
            f"{discount} de descuento en {original_total}"
            if original_total
            else f"{discount} de descuento"
        )
        detail_width = product_width if width_multiplier == 1 else effective_width - qty_width
        for discount_part in _wrap_cells(discount_text, detail_width):
            rows.append({
                "text": _pad_right((
                    _pad_right("", qty_width) + _pad_right(discount_part, detail_width)
                ), effective_width),
                "align": "left",
                "classes": ["receipt-product-discount-row"],
            })
    return rows


def _spanish_decimal_money(value: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d{2}(?:\s*(?:€|EUR))?$)", ",", value)


def _show_unit_price(qty: str) -> bool:
    try:
        return Decimal(str(qty).strip().replace(",", ".")) != Decimal("1")
    except (InvalidOperation, ValueError):
        return True


def _cell_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _truncate_cells(text: str, width: int) -> str:
    result = []
    used = 0
    for char in str(text):
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result)


def _wrap_cells(text: str, width: int) -> list[str]:
    remaining = str(text).strip()
    rows: list[str] = []
    while remaining:
        part = _truncate_cells(remaining, width)
        if not part:
            break
        consumed = len(part)
        if consumed < len(remaining) and not remaining[consumed].isspace():
            word_boundary = part.rfind(" ")
            if word_boundary > 0:
                part = part[:word_boundary]
                consumed = word_boundary + 1
        rows.append(part.rstrip())
        remaining = remaining[consumed:].lstrip()
    return rows


def _pad_right(text: str, width: int) -> str:
    clipped = _truncate_cells(str(text), width)
    return clipped + (" " * max(0, width - _cell_width(clipped)))


def _pad_left(text: str, width: int) -> str:
    source = str(text)
    if _cell_width(source) > width:
        source = _truncate_cells(source, width)
    return (" " * max(0, width - _cell_width(source))) + source


def _apply_horizontal_offset(line: dict[str, Any], offset: int, width: int) -> None:
    """Convert text alignment to exact character padding with a signed offset."""
    if (
        not offset
        or line.get("type") in {"image", "product_line", "header_meta_line", "spacer"}
        or any(str(cls).startswith("receipt-product-") for cls in line.get("classes", []))
    ):
        return
    text = str(line.get("text") or "")
    if not text or set(text) <= {"-", "=", "*", "·"}:
        return
    align = str(line.get("align") or "left")
    if align == "right":
        base = width - len(text)
    elif align == "center":
        base = (width - len(text)) // 2
    else:
        base = 0
    padding = max(0, min(width - 1, base + offset))
    line["text"] = (" " * padding) + text
    line["align"] = "left"
