"""A byte-identical kitchen print repeat must not reach the printer twice.

The Movi app posts the same payload twice about 100 ms apart, and only after the
first request was already acknowledged, so the box printed the ticket twice.
``/printer_iot/printer`` now drops an identical repeat inside a short window.

A receipt flagged as a reprint is exempt -- that flag is the caller explicitly
asking for a deliberate second copy, and suppressing it would be wrong.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import app.main as main


def _body(**overrides) -> dict:
    body = {
        "device_identifier": "netprinter_aabbccddeeff",
        "lang": "es_ES",
        "language": "es_ES",
        "receipts": [
            {
                "pos_reference": "Order 00042",
                "tracking_number": "888",
                "reprint": False,
                "lines": [{"qty": 1, "full_product_name": "Pizza"}],
            }
        ],
    }
    body.update(overrides)
    return body


class KitchenPrintDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        main._recent_kitchen_prints.clear()
        self.addCleanup(main._recent_kitchen_prints.clear)

    def test_an_identical_repeat_is_a_duplicate(self):
        body = _body()
        self.assertFalse(main._is_duplicate_kitchen_print(body))
        self.assertTrue(main._is_duplicate_kitchen_print(body))

    def test_a_different_order_is_not_a_duplicate(self):
        self.assertFalse(main._is_duplicate_kitchen_print(_body()))
        other = _body()
        other["receipts"][0]["tracking_number"] = "889"
        self.assertFalse(main._is_duplicate_kitchen_print(other))

    def test_the_same_order_to_another_device_is_not_a_duplicate(self):
        self.assertFalse(main._is_duplicate_kitchen_print(_body()))
        self.assertFalse(
            main._is_duplicate_kitchen_print(_body(device_identifier="netprinter_112233445566"))
        )

    def test_an_explicit_reprint_is_never_suppressed(self):
        body = _body()
        body["receipts"][0]["reprint"] = True
        self.assertFalse(main._is_duplicate_kitchen_print(body))
        self.assertFalse(main._is_duplicate_kitchen_print(body))

    def test_a_repeat_outside_the_window_is_not_a_duplicate(self):
        body = _body()
        with patch.object(main, "_KITCHEN_PRINT_DEDUPE_SECONDS", 0.05):
            self.assertFalse(main._is_duplicate_kitchen_print(body))
            # Age the recorded entry past the window.
            main._recent_kitchen_prints[list(main._recent_kitchen_prints)[0]] -= 1.0
            self.assertFalse(main._is_duplicate_kitchen_print(body))

    def test_the_guard_can_be_switched_off(self):
        body = _body()
        with patch.object(main, "_KITCHEN_PRINT_DEDUPE_SECONDS", 0):
            self.assertFalse(main._is_duplicate_kitchen_print(body))
            self.assertFalse(main._is_duplicate_kitchen_print(body))

    def test_stale_entries_are_pruned(self):
        with patch.object(main, "_KITCHEN_PRINT_DEDUPE_SECONDS", 0.05):
            main._is_duplicate_kitchen_print(_body())
            main._recent_kitchen_prints[list(main._recent_kitchen_prints)[0]] -= 1.0
            main._is_duplicate_kitchen_print(_body())
            self.assertEqual(len(main._recent_kitchen_prints), 1)


if __name__ == "__main__":
    unittest.main()
