from __future__ import annotations

import pytest

from app.printer_profile import (
    DEFAULT_PROFILE,
    active_columns,
    calibration_lines,
    line_diagnostics,
    text_cells,
    validate_printer_profile,
)
from app.printing.text_layout import TextLayoutMixin


class _Layout(TextLayoutMixin):
    def __init__(self, profile):
        self.local_config_getter = lambda: {"printer_profile": profile}


def test_default_profile_is_valid_and_uses_font_a_columns():
    profile = validate_printer_profile({})
    assert profile == DEFAULT_PROFILE
    assert active_columns(profile) == 48


def test_empty_unsaved_profile_preserves_legacy_48_columns(monkeypatch):
    monkeypatch.delenv("IOT_ESCPOS_LINE_WIDTH", raising=False)
    monkeypatch.setenv("IOT_ESCPOS_PAPER_WIDTH", "80")
    assert _Layout({})._escpos_line_width() == 48


def test_font_b_selects_its_own_column_count():
    profile = validate_printer_profile({"font": "b", "columns_font_b": 72})
    assert active_columns(profile) == 72


def test_profile_rejects_impossible_margins():
    with pytest.raises(ValueError, match="左右边距"):
        validate_printer_profile({"printable_width_dots": 320, "margin_left_dots": 160, "margin_right_dots": 160})


def test_diagnostics_account_for_cjk_and_width_multiplier():
    profile = validate_printer_profile({"columns_font_a": 24})
    diagnostics = line_diagnostics([
        {"text": "中文" + "x" * 21},
        {"text": "1234567890123", "width_multiplier": 2},
    ], profile)
    assert text_cells("中文x") == 5
    assert diagnostics[0]["used"] == 25
    assert diagnostics[0]["overflow"] is True
    assert diagnostics[1]["available"] == 12
    assert diagnostics[1]["overflow"] is True


def test_calibration_ticket_matches_active_width():
    profile = validate_printer_profile({"columns_font_a": 42})
    lines = calibration_lines(profile)
    assert len(lines[2]["text"]) == 42
    assert len(lines[3]["text"]) == 42
    assert lines[4]["text"].startswith("|")
    assert lines[4]["text"].endswith("|")
