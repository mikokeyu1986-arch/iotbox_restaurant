"""File-backed template for KIOSK pickup tickets."""
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any

BLOCKS = ("order_type", "tracking", "barcode", "payment_prompt", "location")

def _path() -> Path:
    root = Path(os.getenv("IOT_RESOURCE_DIR", Path(__file__).resolve().parent.parent))
    return Path(os.getenv("IOT_KIOSK_TICKET_TEMPLATE_PATH") or root / "kiosk_ticket_template.json")

def default_kiosk_ticket_template() -> dict[str, Any]:
    return {"version": 1, "name": "KIOSK 取餐小票", "paper_width": 48, "blocks": [
        {"id": "order_type", "enabled": True, "font_size": 2},
        {"id": "tracking", "enabled": True, "font_size": 2},
        {"id": "barcode", "enabled": True, "font_size": 1},
        {"id": "payment_prompt", "enabled": True, "font_size": 1},
        {"id": "location", "enabled": True, "font_size": 1},
    ]}

def load_kiosk_ticket_template() -> dict[str, Any]:
    try:
        source = json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else default_kiosk_ticket_template()
    except (OSError, json.JSONDecodeError): source = default_kiosk_ticket_template()
    saved = {str(item.get("id")): item for item in source.get("blocks", []) if isinstance(item, dict)}
    return {"version": 1, "name": str(source.get("name") or "KIOSK 取餐小票")[:80], "paper_width": 48, "blocks": [
        {"id": key, "enabled": bool(saved.get(key, {}).get("enabled", True)), "font_size": max(1, min(3, int(saved.get(key, {}).get("font_size", 1))))}
        for key in BLOCKS
    ]}

def apply_kiosk_ticket_template(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {key: [] for key in BLOCKS}
    for item in lines:
        row = dict(item); key = str(row.pop("_template_block", ""))
        if key in groups: groups[key].append(row)
    result = []
    for block in load_kiosk_ticket_template()["blocks"]:
        if not block["enabled"]: continue
        for row in groups[block["id"]]:
            if block["id"] in {"order_type", "tracking"}:
                size = block["font_size"]; row.update(width_multiplier=size, height_multiplier=size, double_width=size > 1, double_height=size > 1)
            if block["id"] == "tracking":
                row["align"] = "center"
            result.append(row)
    return result
