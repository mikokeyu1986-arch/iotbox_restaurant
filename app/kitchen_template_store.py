"""Validated, file-backed configuration for the kitchen ticket editor."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from .receipt_template_store import _apply_horizontal_offset


BLOCKS = (
    ("tracking", "取餐号（顶部）"),
    ("order_type", "订单类型"),
    ("status", "菜品通知（NUEVO / CANCELA）"),
    ("order_meta", "桌号（仅显示字段值）"),
    ("separator_before", "商品前分隔线"),
    ("products", "菜序 / 厨房商品明细"),
    ("separator_after", "商品后分隔线"),
    ("location", "门店名称 / 下单时间（左右排列）"),
)
BLOCK_IDS = {key for key, _ in BLOCKS}
CONTENT_OVERRIDE_BLOCKS = {"tracking", "order_type", "status", "order_meta", "location"}
ALIGNS = {"inherit", "left", "center", "right"}
CUSTOM_KINDS = {"text", "separator", "spacer"}
CUSTOM_ID = re.compile(r"^custom_[a-z0-9_-]{4,64}$")
_logger = logging.getLogger(__name__)
_template_lock = threading.RLock()


def default_kitchen_template() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "默认厨房单",
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


def kitchen_template_path() -> Path:
    configured = os.getenv("IOT_KITCHEN_TEMPLATE_PATH", "").strip()
    if configured:
        return Path(configured)
    resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", Path(__file__).resolve().parent.parent))
    return resource_dir / "kitchen_template.json"


def validate_kitchen_template(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("厨房单模板必须是 JSON 对象")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("厨房单模板缺少 blocks 列表")
    labels = dict(BLOCKS)
    seen: set[str] = set()
    blocks: list[dict[str, Any]] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ValueError("每个厨房单区块必须是对象")
        block_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or ("builtin" if block_id in BLOCK_IDS else ""))
        # Templates saved before the combined footer used a separate time block.
        # Its data is now rendered on the right side of the location row.
        if block_id == "time" and kind == "builtin":
            continue
        is_builtin = kind == "builtin" and block_id in BLOCK_IDS
        is_custom = kind in CUSTOM_KINDS and bool(CUSTOM_ID.fullmatch(block_id))
        if (not is_builtin and not is_custom) or block_id in seen:
            raise ValueError(f"未知或重复的厨房单区块: {block_id or '<空>'}")
        align = str(raw.get("align") or "inherit")
        if align not in ALIGNS:
            raise ValueError(f"无效的对齐方式: {align}")
        bold = raw.get("bold", "inherit")
        if bold not in (True, False, "inherit"):
            raise ValueError("bold 必须是 true、false 或 inherit")
        try:
            offset = max(-12, min(12, int(raw.get("horizontal_offset") or 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            spacing = max(0, min(4, int(raw.get("spacing_after") or 0)))
        except (TypeError, ValueError):
            spacing = 0
        block = {
            "id": block_id,
            "kind": kind,
            "label": labels[block_id] if is_builtin else str(raw.get("label") or "自定义区块")[:40],
            "enabled": bool(raw.get("enabled", True)),
            "align": align,
            "bold": bold,
            "horizontal_offset": offset,
            "spacing_after": spacing,
        }
        if is_builtin:
            content = str(raw.get("content") or "")[:1000]
            block["content"] = content if block_id in CONTENT_OVERRIDE_BLOCKS else ""
            for size_key in (
                "font_size", "product_font_size", "attribute_font_size",
                "note_font_size", "order_note_font_size",
            ):
                try:
                    block[size_key] = max(1, min(3, int(raw.get(size_key) or 1)))
                except (TypeError, ValueError):
                    block[size_key] = 1
        elif kind == "text":
            block["text"] = str(raw.get("text") or "")[:1000]
            block["double_size"] = bool(raw.get("double_size", False))
        elif kind == "separator":
            character = str(raw.get("character") or "-")[:1]
            block["character"] = character if character in {"-", "=", "*", "·"} else "-"
        else:
            try:
                block["lines"] = max(1, min(6, int(raw.get("lines") or 1)))
            except (TypeError, ValueError):
                block["lines"] = 1
        blocks.append(block)
        seen.add(block_id)
    canonical = {block_id: index for index, (block_id, _) in enumerate(BLOCKS)}
    for block_id, label in BLOCKS:
        if block_id in seen:
            continue
        insert_at = next(
            (index for index, existing in enumerate(blocks)
             if existing["id"] in canonical and canonical[existing["id"]] > canonical[block_id]),
            len(blocks),
        )
        blocks.insert(insert_at, {
            "id": block_id, "kind": "builtin", "label": label, "enabled": True,
            "align": "inherit", "bold": "inherit", "horizontal_offset": 0,
            "spacing_after": 0, "content": "",
        })
    return {
        "version": 1,
        "name": str(payload.get("name") or "自定义厨房单")[:80],
        "paper_width": 48,
        "blocks": blocks,
    }


def load_kitchen_template() -> dict[str, Any]:
    path = kitchen_template_path()
    with _template_lock:
        if not path.exists():
            return default_kitchen_template()
        try:
            return validate_kitchen_template(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            _logger.exception("Invalid kitchen template at %s; using defaults without overwriting it", path)
            return default_kitchen_template()


def save_kitchen_template(payload: Any) -> dict[str, Any]:
    template = validate_kitchen_template(payload)
    path = kitchen_template_path()
    with _template_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return deepcopy(template)


def reset_kitchen_template() -> dict[str, Any]:
    return save_kitchen_template(default_kitchen_template())


def apply_kitchen_template(
    lines: list[dict[str, Any]], template: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    selected = validate_kitchen_template(template or load_kitchen_template())
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in BLOCK_IDS}
    untagged: list[dict[str, Any]] = []
    for source in lines:
        line = dict(source)
        block_id = str(line.pop("_template_block", ""))
        (grouped[block_id] if block_id in grouped else untagged).append(line)
    # The kitchen template is authoritative.  Do not prepend legacy/untagged
    # lines, otherwise the old native layout appears before the designed
    # tracking/order/products/location blocks.
    # Preserve the native kitchen header (company/operator/order reference)
    # before applying the configured kitchen sections.
    result: list[dict[str, Any]] = [
        line for line in untagged
        if str(line.get("text") or "").strip()
        and not ("product-name" in {str(value) for value in line.get("classes") or []})
        and not set(str(line.get("text") or "").strip()) <= {"-", "="}
    ]
    for block in selected["blocks"]:
        if not block["enabled"]:
            continue
        kind = block.get("kind", "builtin")
        if kind == "text":
            block_lines = [{
                "text": text,
                "align": "left" if block["align"] == "inherit" else block["align"],
                "bold": block["bold"] is True,
                "double_width": block.get("double_size", False),
                "double_height": block.get("double_size", False),
                "classes": ["template-custom-text"],
            } for text in str(block.get("text") or "").splitlines()]
        elif kind == "separator":
            block_lines = [{"text": str(block.get("character") or "-") * 48, "align": "left"}]
        elif kind == "spacer":
            block_lines = [{"type": "spacer", "align": "left"}
                           for _ in range(int(block.get("lines") or 1))]
        elif block.get("content"):
            block_lines = [{"text": text, "align": "center"}
                           for text in str(block["content"]).splitlines()]
        else:
            block_lines = grouped[block["id"]]
            if block["id"] in {"separator_before", "separator_after"} and block_lines:
                block_lines = block_lines[:1]
        for source_line in block_lines:
            line = dict(source_line)
            classes = {str(value) for value in line.get("classes") or []}
            if kind == "builtin":
                size = int(block.get("font_size") or 1)
                if block["id"] == "products":
                    if "kitchen-product-line" in classes:
                        size = int(block.get("product_font_size") or size)
                    elif "kitchen-attribute" in classes:
                        size = int(block.get("attribute_font_size") or size)
                    elif "kitchen-product-note" in classes:
                        size = int(block.get("note_font_size") or size)
                    elif "kitchen-order-note" in classes:
                        size = int(block.get("order_note_font_size") or size)
                line["width_multiplier"] = size
                line["height_multiplier"] = size
                line["double_width"] = size > 1
                line["double_height"] = size > 1
            if block["align"] != "inherit" and line.get("type") not in {"product_line", "header_meta_line"}:
                line["align"] = block["align"]
            if block["bold"] != "inherit":
                line["bold"] = block["bold"]
            _apply_horizontal_offset(line, block["horizontal_offset"], 48)
            result.append(line)
        if block_lines:
            result.extend({"type": "spacer", "align": "left"} for _ in range(block["spacing_after"]))
    return result
