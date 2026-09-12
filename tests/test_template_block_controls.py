"""Per-block decorations in the visual template editors.

These three controls were silently inert on the kitchen ticket:

* "Separator after block" and "Blank line after block" — ``validate_kitchen_template``
  dropped both fields before saving, and ``apply_kitchen_template`` had no branch
  that emitted them at all.
* "取餐号双倍字号" — the editor wrote ``double_size`` / ``tracking_double_size``,
  which the renderers never honoured.  The toggle is now a view onto ``font_size``.

The receipt had the same double-size problem for its ``table`` block, and its
blank-line flag was nested inside the separator flag, so it could not be used on
its own.

Note: both validators insert any canonical block missing from the payload, so
blocks must be looked up by id rather than by position.
"""

from __future__ import annotations

import unittest

from app.kitchen_template_store import apply_kitchen_template, validate_kitchen_template
from app.receipt_template_store import apply_template, validate_template


def _block(template, block_id):
    return next(block for block in template["blocks"] if block["id"] == block_id)


def _receipt_template(block_id="totals", **overrides):
    block = {"id": block_id, "kind": "builtin", "enabled": True}
    block.update(overrides)
    return validate_template(
        {"version": 1, "name": "test", "paper_width": 48, "blocks": [block]}
    )


def _kitchen_template(block_id="products", **overrides):
    block = {"id": block_id, "kind": "builtin", "enabled": True}
    block.update(overrides)
    return validate_kitchen_template(
        {"version": 1, "name": "test", "paper_width": 48, "blocks": [block]}
    )


def _classes(lines, name):
    return [line for line in lines if name in (line.get("classes") or [])]


class KitchenBlockDecorationTests(unittest.TestCase):
    def test_validator_keeps_the_separator_and_blank_line_flags(self):
        """Saving must not discard them — this is what made the toggles inert."""
        template = _kitchen_template(
            separator_after=True, separator_after_character="=", blank_line_after=True
        )
        block = _block(template, "products")
        self.assertTrue(block["separator_after"])
        self.assertEqual(block["separator_after_character"], "=")
        self.assertTrue(block["blank_line_after"])

    def test_separator_character_defaults_when_absent(self):
        template = _kitchen_template(separator_after=True)
        self.assertEqual(_block(template, "products")["separator_after_character"], "-")

    def test_renderer_emits_each_decoration_independently(self):
        """They are two separate toggles, so neither may require the other."""
        source = [{"text": "PIZZA", "_template_block": "products"}]
        cases = {
            (False, False): (0, 0),
            (True, False): (1, 0),
            (False, True): (0, 1),
            (True, True): (1, 1),
        }
        for (separator, blank), (expect_sep, expect_blank) in cases.items():
            with self.subTest(separator=separator, blank=blank):
                template = _kitchen_template(
                    separator_after=separator,
                    separator_after_character="=",
                    blank_line_after=blank,
                )
                lines = apply_kitchen_template(source, template)
                self.assertEqual(len(_classes(lines, "template-block-separator")), expect_sep)
                self.assertEqual(len(_classes(lines, "template-block-blank-line")), expect_blank)

    def test_separator_is_a_full_paper_width(self):
        template = _kitchen_template(separator_after=True, separator_after_character="=")
        lines = apply_kitchen_template(
            [{"text": "PIZZA", "_template_block": "products"}], template
        )
        separator = _classes(lines, "template-block-separator")[0]
        self.assertEqual(separator["text"], "=" * template["paper_width"])


class ReceiptBlockDecorationTests(unittest.TestCase):
    def test_blank_line_after_does_not_require_a_separator(self):
        source = [{"text": "TOTAL", "_template_block": "totals"}]
        template = _receipt_template(separator_after=False, blank_line_after=True)
        lines = apply_template(source, template)
        self.assertEqual(len(_classes(lines, "template-block-separator")), 0)
        self.assertEqual(len(_classes(lines, "template-block-blank-line")), 1)

    def test_separator_after_does_not_require_a_blank_line(self):
        source = [{"text": "TOTAL", "_template_block": "totals"}]
        template = _receipt_template(separator_after=True, blank_line_after=False)
        lines = apply_template(source, template)
        self.assertEqual(len(_classes(lines, "template-block-separator")), 1)
        self.assertEqual(len(_classes(lines, "template-block-blank-line")), 0)


class TrackedBlockSizeTests(unittest.TestCase):
    """The "取餐号双倍字号" toggle is a view onto font_size.

    Height carries the size; width follows at half of it, so a bigger glyph
    grows mostly upwards instead of eating the line.
    """

    def test_receipt_table_size_follows_font_size(self):
        source = [{"text": "MESA A1", "_template_block": "table"}]
        for font_size, (width, height) in (
            (1, (1, 1)), (2, (1, 2)), (3, (2, 3)), (4, (2, 4)), (5, (3, 5)),
        ):
            with self.subTest(font_size=font_size):
                lines = apply_template(source, _receipt_template("table", font_size=font_size))
                self.assertEqual(lines[0]["width_multiplier"], width)
                self.assertEqual(lines[0]["height_multiplier"], height)
                self.assertEqual(lines[0]["double_width"], width > 1)
                self.assertEqual(lines[0]["double_height"], height > 1)

    def test_receipt_table_is_taller_by_default(self):
        """The table line was always emphasised; keep that as the default."""
        template = _receipt_template("table")
        self.assertEqual(_block(template, "table")["font_size"], 2)
        lines = apply_template([{"text": "MESA A1", "_template_block": "table"}], template)
        self.assertEqual(lines[0]["height_multiplier"], 2)

    def test_receipt_table_spacer_is_not_enlarged(self):
        """Only the text line takes the multiplier, not the trailing spacer."""
        template = _receipt_template("table", font_size=5)
        lines = apply_template(
            [
                {"text": "MESA A1", "_template_block": "table"},
                {"text": "", "align": "left", "classes": ["receipt-spacer"], "_template_block": "table"},
            ],
            template,
        )
        spacer = next(line for line in lines if line.get("classes") == ["receipt-spacer"])
        self.assertNotIn("height_multiplier", spacer)

    def test_kitchen_pickup_number_follows_font_size(self):
        """The kitchen scale is 1-3; a line type's setting drives the glyph size."""
        source = [{"text": "# 42", "_template_block": "tracking"}]
        for font_size, (width, height) in ((1, (1, 1)), (2, (1, 2)), (3, (2, 3))):
            with self.subTest(font_size=font_size):
                template = _kitchen_template("tracking", font_size=font_size)
                lines = apply_kitchen_template(source, template)
                self.assertEqual(lines[0]["width_multiplier"], width)
                self.assertEqual(lines[0]["height_multiplier"], height)

    def test_width_grows_half_as_fast_as_height(self):
        """The point of the mapping: a level-5 glyph is 3x5, not 5x5."""
        from app.receipt_template_store import font_size_multipliers

        self.assertEqual(font_size_multipliers(1), (1, 1))
        self.assertEqual(font_size_multipliers(2), (1, 2))
        self.assertEqual(font_size_multipliers(3), (2, 3))
        self.assertEqual(font_size_multipliers(4), (2, 4))
        self.assertEqual(font_size_multipliers(5), (3, 5))
        # Clamped at both ends, and tolerant of junk.
        self.assertEqual(font_size_multipliers(0), (1, 1))
        self.assertEqual(font_size_multipliers(99), (3, 5))
        self.assertEqual(font_size_multipliers(None), (1, 1))

    def test_a_wider_glyph_still_fits_the_paper(self):
        """Wrapping must follow the glyph width, not assume a 2x maximum.

        A 3x-wide row holds a third of the columns; wrapping at width // 2 would
        overflow the paper.
        """
        from app.device_manager import DeviceManager
        from app.event_bus import EventBus
        import tempfile
        from pathlib import Path

        manager = DeviceManager(EventBus(), Path(tempfile.mkdtemp()), iot_identifier="b")
        for width_multiplier, expected in ((1, 48), (2, 24), (3, 16)):
            with self.subTest(width_multiplier=width_multiplier):
                rendered = manager._render_escpos_lines(
                    {
                        "text": "A" * 60,
                        "align": "left",
                        "width_multiplier": width_multiplier,
                        "height_multiplier": width_multiplier,
                    },
                    48,
                )
                for row in rendered:
                    self.assertLessEqual(
                        len(row), expected,
                        f"row wider than the printable area at {width_multiplier}x",
                    )


class FontSizeScaleTests(unittest.TestCase):
    """The kitchen offers 1..3, the customer receipt 1..5."""

    def test_every_choice_survives_the_kitchen_validator(self):
        for size in (1, 2, 3):
            with self.subTest(size=size):
                template = _kitchen_template("tracking", font_size=size)
                self.assertEqual(_block(template, "tracking")["font_size"], size)

    def test_every_choice_survives_the_receipt_validator(self):
        for block_id in ("tracking", "table", "products"):
            for size in (1, 2, 3, 4, 5):
                with self.subTest(block=block_id, size=size):
                    template = _receipt_template(block_id, font_size=size)
                    self.assertEqual(_block(template, block_id)["font_size"], size)

    def test_sizes_beyond_the_scale_are_clamped(self):
        self.assertEqual(_block(_kitchen_template("tracking", font_size=9), "tracking")["font_size"], 3)
        self.assertEqual(_block(_receipt_template("products", font_size=9), "products")["font_size"], 5)

    def test_the_editors_offer_their_own_range(self):
        """The receipt's dropdown is hand-written in index.html -- keep it in step.

        The kitchen's rows are built in JS from the list the server sends, so
        only the server list constrains them.
        """
        from pathlib import Path

        from app.kitchen_template_store import FONT_SIZE_CHOICES as kitchen_choices
        from app.receipt_template_store import FONT_SIZE_CHOICES as receipt_choices

        self.assertEqual(kitchen_choices, (1, 2, 3))
        self.assertEqual(receipt_choices, (1, 2, 3, 4, 5))
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        for size in receipt_choices:
            self.assertIn(f'<option value="{size}">', html)


class ShippedTemplateDefaultTests(unittest.TestCase):
    """The shipped default and the per-installation override are separate files.

    They used to be the same path, which made reset_template() write the file
    back to itself (a visible no-op) and let one installation's tuning change
    what every other box starts from.
    """

    def test_the_defaults_live_under_templates(self):
        from pathlib import Path

        from app.kitchen_template_store import default_kitchen_template
        from app.receipt_template_store import default_template

        root = Path(__file__).resolve().parent.parent
        self.assertEqual(default_template()["name"], "默认结账小票")
        self.assertTrue(default_kitchen_template()["blocks"])
        for name in ("receipt_template.json", "kitchen_template.json"):
            self.assertTrue((root / "templates" / name).is_file(), f"missing templates/{name}")

    def test_the_default_path_is_not_the_override_path(self):
        from pathlib import Path

        from app.kitchen_template_store import default_kitchen_template, kitchen_template_path
        from app.receipt_template_store import default_template, template_path

        root = Path(__file__).resolve().parent.parent
        self.assertEqual(template_path(), root / "receipt_template.json")
        self.assertEqual(kitchen_template_path(), root / "kitchen_template.json")
        self.assertNotEqual(template_path().parent, root / "templates")
        self.assertEqual(default_template()["paper_width"], 48)
        self.assertEqual(default_kitchen_template()["paper_width"], 48)

    def test_resetting_restores_the_shipped_layout(self):
        """A reset must actually change a tuned override back."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import app.kitchen_template_store as kitchen_store

        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "kitchen_template.json"
            with patch.dict(
                __import__("os").environ, {"IOT_KITCHEN_TEMPLATE_PATH": str(override)}
            ):
                tuned = kitchen_store.default_kitchen_template()
                tuned["name"] = "tuned by the restaurant"
                kitchen_store.save_kitchen_template(tuned)
                self.assertEqual(
                    kitchen_store.load_kitchen_template()["name"], "tuned by the restaurant"
                )
                restored = kitchen_store.reset_kitchen_template()
            self.assertNotEqual(restored["name"], "tuned by the restaurant")

    def test_the_gitignore_keeps_the_overrides_out_but_the_defaults_in(self):
        """A bare pattern matches at any depth -- the root ones must be anchored."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        ignored = subprocess.run(
            ["git", "check-ignore", "receipt_template.json", "kitchen_template.json"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(
            len(ignored.stdout.split()), 2, "the per-installation overrides must stay ignored"
        )
        tracked = subprocess.run(
            ["git", "check-ignore", "templates/receipt_template.json", "templates/kitchen_template.json"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(tracked.stdout.strip(), "", "the shipped defaults must be trackable")

    def test_the_installer_ships_the_defaults(self):
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "build_installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("templates;templates", script)


class KitchenLineSettingTests(unittest.TestCase):
    """Each kitchen line type carries its own size and bold."""

    def _line(self, block_id, classes, **block_overrides):
        template = _kitchen_template(block_id, **block_overrides)
        return apply_kitchen_template(
            [{"text": "X", "classes": list(classes), "_template_block": block_id}], template
        )[0]

    def test_every_line_type_is_covered_by_the_class_list(self):
        """The class list must not drift from what the builder emits."""
        import re
        from pathlib import Path

        from app.kitchen_template_store import LINE_CLASS_ALIASES, LINE_CLASS_ORDER

        source = (Path(__file__).resolve().parent.parent / "app" / "receipt_builder.py").read_text(
            encoding="utf-8"
        )
        body = source[source.index("def build_kitchen_ticket_lines"):]
        body = body[: body.index("\ndef ", 10)]
        emitted = {
            name
            for match in re.finditer(r'"classes":\s*\[([^\]]*)\]', body)
            for name in re.findall(r'"(kitchen-[a-z-]+)"', match.group(1))
        }
        alone = emitted - LINE_CLASS_ALIASES
        self.assertEqual(
            alone, set(LINE_CLASS_ORDER),
            "a kitchen line type was added or renamed without a size/bold control",
        )
        # And nothing in the control list is a ride-along marker.
        self.assertEqual(set(LINE_CLASS_ORDER) & LINE_CLASS_ALIASES, set())

    def test_a_line_type_size_overrides_the_block(self):
        template = _kitchen_template("products", font_size=3, product_font_size=3,
                                     attribute_font_size=3)
        template["line_font_sizes"]["kitchen-attribute"] = 1
        lines = apply_kitchen_template(
            [
                {"text": "PIZZA", "classes": ["kitchen-product-line"], "_template_block": "products"},
                {"text": "+ queso", "classes": ["kitchen-note", "kitchen-attribute"], "_template_block": "products"},
            ],
            template,
        )
        self.assertEqual(lines[0]["height_multiplier"], 3)
        self.assertEqual(lines[1]["height_multiplier"], 1)

    def test_a_note_marker_does_not_mask_the_specific_class(self):
        """kitchen-note rides on the attribute line; the attribute entry must win."""
        template = _kitchen_template("products", font_size=1)
        template["line_font_sizes"]["kitchen-attribute"] = 2
        template["line_font_sizes"]["kitchen-note"] = 1
        lines = apply_kitchen_template(
            [{"text": "+ queso", "classes": ["kitchen-note", "kitchen-attribute"], "_template_block": "products"}],
            template,
        )
        self.assertEqual(lines[0]["height_multiplier"], 2)

    def test_per_line_bold_overrides_the_block(self):
        template = _kitchen_template("products", font_size=1, bold=True)
        template["line_bold"] = {"kitchen-attribute": False}
        lines = apply_kitchen_template(
            [
                {"text": "PIZZA", "classes": ["kitchen-product-line"], "_template_block": "products"},
                {"text": "+ queso", "classes": ["kitchen-note", "kitchen-attribute"], "_template_block": "products"},
            ],
            template,
        )
        self.assertTrue(lines[0]["bold"])
        self.assertFalse(lines[1]["bold"])

    def test_an_absent_bold_entry_leaves_the_line_alone(self):
        """The builder bolds several lines; nothing may turn that off by default."""
        template = _kitchen_template("products", font_size=1)
        self.assertEqual(template["line_bold"], {})
        lines = apply_kitchen_template(
            [{"text": "NUEVO", "classes": ["kitchen-tracking-number"], "bold": True,
              "_template_block": "products"}],
            template,
        )
        self.assertTrue(lines[0]["bold"])

    def test_migration_reproduces_the_old_per_block_sizes(self):
        """A template carrying only the legacy fields must not change its look.

        Note the validator already defaults every legacy size key to 1, and the
        old renderer also preferred ``product_font_size`` over the block's
        ``font_size`` -- so the migration has to prefer it too.
        """
        legacy = _kitchen_template("products", font_size=3, product_font_size=2,
                                   attribute_font_size=1)
        for block in legacy["blocks"]:
            if block["id"] == "tracking":
                block["font_size"] = 2
            elif block["id"] == "order_meta":
                block["font_size"] = 1
        legacy.pop("line_font_sizes", None)
        migrated = validate_kitchen_template(legacy)
        self.assertEqual(migrated["line_font_sizes"]["kitchen-product-line"], 2)
        self.assertEqual(migrated["line_font_sizes"]["kitchen-attribute"], 1)
        self.assertEqual(migrated["line_font_sizes"]["kitchen-course-header"], 3)
        self.assertEqual(migrated["line_font_sizes"]["kitchen-tracking-number"], 2)
        self.assertEqual(migrated["line_font_sizes"]["kitchen-table-number"], 1)

    def test_a_legacy_block_bold_is_not_turned_into_an_explicit_line_bold(self):
        """'inherit' must stay inherit, or the builder's own bold would be lost."""
        legacy = _kitchen_template("products", font_size=1)
        legacy.pop("line_font_sizes", None)
        legacy.pop("line_bold", None)
        self.assertEqual(validate_kitchen_template(legacy)["line_bold"], {})

    def test_the_ui_fetches_the_line_types_rather_than_hardcoding_them(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("result.line_classes", js)
        self.assertIn("kitchenLineSettingsRows", js)

    def test_each_line_type_names_a_real_block(self):
        """The editor shows a line type under its block, so the ids must match."""
        from app.kitchen_template_store import BLOCK_IDS, LINE_CLASSES

        for name, label, block_id in LINE_CLASSES:
            with self.subTest(line_class=name):
                self.assertIn(block_id, BLOCK_IDS)
                self.assertTrue(label)

    def test_every_kitchen_block_with_text_owns_at_least_one_line_type(self):
        """Otherwise selecting that block would show no size control at all."""
        from app.kitchen_template_store import BLOCK_IDS, LINE_CLASSES

        owning = {block_id for _, _, block_id in LINE_CLASSES}
        # These blocks render a separator or a blank line rather than text.
        decoration = {"separator_before", "separator_after"}
        self.assertEqual(set(BLOCK_IDS) - owning, decoration)

    def test_the_controls_live_in_the_block_inspector_not_a_global_panel(self):
        """The user picks a block and sizes that block's lines."""
        from pathlib import Path

        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        form_start = html.index('id="receiptInspectorForm"')
        form_end = html.index("</form>", form_start)
        self.assertLess(
            form_start,
            html.index('id="kitchenLineSettings"'),
            "the per-line controls must sit inside the block inspector form",
        )
        self.assertLess(
            html.index('id="kitchenLineSettings"'), form_end,
            "the per-line controls must sit inside the block inspector form",
        )
        js = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("entry.block === selectedReceiptBlockId", js)

    def test_the_removed_toggles_are_gone(self):
        """Blank-line-after and 取餐号双倍字号 were dropped from the editor."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("receiptBlockBlankLineAfter", html)
        self.assertNotIn("取餐号双倍字号", html)

    def test_every_element_the_editor_looks_up_exists(self):
        """A removed element left referenced in app.js throws and blanks the editor.

        `el(...)` returns null, the first property access on it raises, and
        nothing after it renders -- including the preview.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        js = (root / "web" / "app.js").read_text(encoding="utf-8")
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        present = set(re.findall(r'id="([^"]+)"', html))
        missing = sorted({name for name in re.findall(r'el\("([^"]+)"\)', js)} - present)
        self.assertEqual(missing, [], f"app.js looks up elements that no longer exist: {missing}")

    def test_the_editor_markup_is_balanced(self):
        import re
        from pathlib import Path

        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        for tag in ("form", "section", "div", "label", "select"):
            with self.subTest(tag=tag):
                opened = len(re.findall(rf"<{tag}[\s>]", html))
                closed = len(re.findall(rf"</{tag}>", html))
                self.assertEqual(opened, closed, f"unbalanced <{tag}> in index.html")


if __name__ == "__main__":
    unittest.main()
