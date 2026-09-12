from __future__ import annotations

import hashlib
import json
import logging
from time import time
from typing import Any


_logger = logging.getLogger("dev")
_SENSITIVE_KEYS = {"token", "password", "authorization", "cookie", "session_id"}


def _short_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        text = str(value or "")
        return f"<redacted:{len(text)}>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return f"{value[:500]}...<truncated:{len(value)}>"
    return value


def summarize_action(data: dict[str, Any]) -> dict[str, Any]:
    receipt = data.get("receipt") if isinstance(data, dict) else None
    summary: dict[str, Any] = {
        "action": str(data.get("action") or "") if isinstance(data, dict) else "",
        "action_unique_id": str(data.get("action_unique_id") or "") if isinstance(data, dict) else "",
        "keys": sorted(data.keys()) if isinstance(data, dict) else [],
    }
    if isinstance(receipt, dict):
        structured = receipt.get("structured") if isinstance(receipt.get("structured"), dict) else {}
        lines = receipt.get("lines") if isinstance(receipt.get("lines"), list) else []
        items = structured.get("items") if isinstance(structured.get("items"), list) else []
        product_names: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                product_names.append(str(item.get("name")))
        for line in lines:
            if isinstance(line, dict) and line.get("name"):
                product_names.append(str(line.get("name")))
        summary.update(
            {
                "receipt_fingerprint": _short_hash(receipt),
                "line_count": len(lines),
                "structured_item_count": len(items),
                "products": product_names[:10],
            }
        )
    return summary


def dev_log(event: str, **fields: Any) -> None:
    payload = {
        "ts": round(time(), 3),
        "event": event,
        **{key: _redact(value, key) for key, value in fields.items()},
    }
    try:
        _logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        _logger.exception("failed to write dev log event=%s", event)
