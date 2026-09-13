"""Printer geometry shared by receipt preview and RAW ESC/POS output."""

from __future__ import annotations

import unicodedata
from typing import Any


DEFAULT_PROFILE = {
    "paper_width_mm": 80,
    "printable_width_dots": 576,
    "columns_font_a": 48,
    "columns_font_b": 64,
    "font": "a",
    "cjk_width": 2,
    "margin_left_dots": 0,
    "margin_right_dots": 0,
    "feed_lines": 4,
    "cut": True,
}


def validate_printer_profile(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("打印机 Profile 必须是 JSON 对象")
    merged = {**DEFAULT_PROFILE, **payload}

    def integer(name: str, minimum: int, maximum: int) -> int:
        try:
            value = int(merged[name])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} 必须是整数") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum}–{maximum} 之间")
        return value

    font = str(merged.get("font") or "a").lower()
    if font not in {"a", "b"}:
        raise ValueError("font 必须是 a 或 b")
    profile = {
        "paper_width_mm": integer("paper_width_mm", 48, 112),
        "printable_width_dots": integer("printable_width_dots", 320, 832),
        "columns_font_a": integer("columns_font_a", 24, 80),
        "columns_font_b": integer("columns_font_b", 32, 96),
        "font": font,
        "cjk_width": integer("cjk_width", 1, 2),
        "margin_left_dots": integer("margin_left_dots", 0, 160),
        "margin_right_dots": integer("margin_right_dots", 0, 160),
        "feed_lines": integer("feed_lines", 0, 12),
        "cut": bool(merged.get("cut", True)),
    }
    if profile["margin_left_dots"] + profile["margin_right_dots"] >= profile["printable_width_dots"]:
        raise ValueError("左右边距之和必须小于可打印点宽")
    return profile


def active_columns(profile: dict[str, Any]) -> int:
    selected = validate_printer_profile(profile)
    return selected["columns_font_b" if selected["font"] == "b" else "columns_font_a"]


def text_cells(value: Any, cjk_width: int = 2) -> int:
    return sum(
        cjk_width if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in str(value or "")
        if not unicodedata.combining(char)
    )


def line_diagnostics(lines: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    selected = validate_printer_profile(profile)
    columns = active_columns(selected)
    diagnostics = []
    for index, line in enumerate(lines):
        if line.get("type") == "image":
            continue
        width_multiplier = max(1, int(line.get("width_multiplier") or (2 if line.get("double_width") else 1)))
        available = max(1, columns // width_multiplier)
        text = str(line.get("text") or "")
        used = text_cells(text, selected["cjk_width"])
        diagnostics.append({
            "line": index + 1,
            "text": text,
            "used": used,
            "available": available,
            "overflow": used > available,
            "width_multiplier": width_multiplier,
        })
    return diagnostics


def calibration_lines(profile: dict[str, Any]) -> list[dict[str, Any]]:
    selected = validate_printer_profile(profile)
    width = active_columns(selected)
    ruler = "".join(str(index // 10 % 10) if index % 10 == 0 else " " for index in range(width))
    digits = "".join(str((index + 1) % 10) for index in range(width))
    return [
        {"text": "RAW ESC/POS 打印校准", "align": "center", "bold": True},
        {"text": f"{selected['paper_width_mm']}mm / {selected['printable_width_dots']} dots / {width} columns", "align": "center"},
        {"text": ruler, "align": "left"},
        {"text": digits, "align": "left"},
        {"text": "|" + "-" * max(0, width - 2) + "|", "align": "left"},
        {"text": "1x1 Latin 123 / 中文测试", "align": "left"},
        {"text": "1x2 HIGH / 中文", "align": "left", "height_multiplier": 2},
        {"text": "2x2 WIDE / 中文", "align": "left", "width_multiplier": 2, "height_multiplier": 2},
        {"text": f"Margins L{selected['margin_left_dots']} R{selected['margin_right_dots']} dots", "align": "left"},
        {"text": "校准完成 · 请检查左右边界、换行与切刀", "align": "center"},
    ]
