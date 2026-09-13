"""A kitchen ticket lists what to cook, never what an attribute costs.

Odoo sends the same attribute payload to both printers: the structured receipt
describes an option as ``{"name": "Extra queso", "unit_price": "1,50 €"}`` so
the customer receipt can print ``+ Extra queso (+1,50 €)``.  The kitchen ticket
rendered that with ``str()``, which printed the whole dict -- price included --
as ``{'name': 'Extra queso', 'unit_price': '1,50 €'}`` on the paper.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.device_manager import DeviceManager
from app.event_bus import EventBus
from app.printing.product_options import format_option, option_label
from app.receipt_builder import build_kitchen_ticket_lines


class OptionLabelTests(unittest.TestCase):
    def test_a_structured_option_keeps_its_name_and_drops_its_price(self):
        self.assertEqual(
            option_label({"name": "Extra queso", "qty": 1, "unit_price": "1,50 €"}),
            "Extra queso",
        )

    def test_a_quantity_is_kept_only_when_it_says_something(self):
        self.assertEqual(
            option_label({"name": "Extra queso", "qty": 2, "unit_price": "3,00 €"}),
            "2 X Extra queso",
        )

    def test_a_price_suffix_is_removed_from_a_label(self):
        self.assertEqual(option_label("Extra queso (+1,50 €)"), "Extra queso")
        self.assertEqual(option_label("Extra queso (1,50)"), "Extra queso")

    def test_a_qualifier_that_is_not_a_price_survives(self):
        self.assertEqual(option_label("Extra queso (sin gluten)"), "Extra queso (sin gluten)")

    def test_a_label_without_a_price_is_returned_unchanged(self):
        self.assertEqual(option_label("Lunch Salmon 20pc x2"), "Lunch Salmon 20pc x2")

    def test_the_receipt_still_prints_the_price(self):
        """Only the kitchen changed -- the customer receipt keeps its amount."""
        self.assertEqual(
            format_option({"name": "Extra queso", "qty": 1, "unit_price": "1,50 €"}),
            "+ Extra queso (+1,50 €)",
        )


class KitchenTicketAttributeTests(unittest.TestCase):
    def _manager(self, directory: str) -> DeviceManager:
        return DeviceManager(EventBus(), Path(directory), iot_identifier="box-test")

    def _kitchen_line(self, **overrides) -> dict:
        line = {
            "type": "product_line",
            "qty": "2",
            "name": "Pizza Margherita",
            "total": "",
            "combo_items": [
                {"name": "Grande", "qty": 1, "unit_price": "2,00 €"},
                {"name": "Extra queso", "qty": 1, "unit_price": "1,50 €"},
            ],
            "classes": ["kitchen-product-line"],
        }
        line.update(overrides)
        return line

    def _render(self, line: dict) -> str:
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            return manager._build_kitchen_escpos_bytes([line]).decode("cp858", errors="replace")

    def test_attribute_prices_never_reach_the_kitchen_paper(self):
        rendered = self._render(self._kitchen_line())

        self.assertNotIn("€", rendered)
        self.assertNotIn("unit_price", rendered)
        self.assertNotIn("{", rendered, "a structured option must not print as a dict repr")
        self.assertIn("Grande", rendered)
        self.assertIn("Extra queso", rendered)

    def test_attribute_lines_still_look_like_attribute_lines(self):
        rendered = self._render(self._kitchen_line())

        self.assertIn("    + Grande", rendered)
        self.assertIn("    + Extra queso", rendered)

    def test_a_price_embedded_in_a_label_is_dropped_too(self):
        line = self._kitchen_line(combo_items=["Extra bacon (+2,00 €)"])

        rendered = self._render(line)

        self.assertIn("Extra bacon", rendered)
        self.assertNotIn("2,00", rendered)

    def test_multiline_attribute_string_drops_price_and_duplicates(self):
        order = {
            "config": {"name": "Restaurante"},
            "changes": {"title": "NUEVO", "data": [{
                "quantity": 1,
                "basic_name": "Funghi",
                "orderDisplayProductName": {
                    "name": "Funghi",
                    "attributeString": "23432 (+€120.00)\n2 x Black olives",
                },
                "attribute_value_names": ["23432", "Black olives"],
            }]},
        }

        texts = [str(line.get("text") or "") for line in build_kitchen_ticket_lines(order)]

        self.assertIn("    + 23432", texts)
        self.assertIn("    + 2 X Black olives", texts)
        self.assertFalse(any("120.00" in text or "€" in text for text in texts), texts)
        self.assertEqual(sum("Black olives" in text for text in texts), 1)
        self.assertTrue(any(text.startswith("    +") for text in texts), texts)

    def test_the_native_order_data_builder_drops_attribute_prices(self):
        order = {
            "config": {"name": "Restaurante"},
            "time": "12:30",
            "tracking_number": "42",
            "table_number": "5",
            "changes": {
                "title": "NUEVO",
                "data": [{
                    "name": "Pizza Margherita (Grande, Extra queso (+1,50 €))",
                    "basic_name": "Pizza Margherita",
                    "attribute_value_names": ["Grande", "Extra queso (+1,50 €)"],
                    "quantity": 2,
                }],
            },
        }

        texts = [
            str(line.get("text") or "") for line in build_kitchen_ticket_lines(order)
        ]

        self.assertIn("    + Grande", texts)
        self.assertIn("    + Extra queso", texts)
        self.assertFalse(any("€" in text for text in texts), texts)


if __name__ == "__main__":
    unittest.main()
