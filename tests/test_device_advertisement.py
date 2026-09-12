"""A device must be advertised once, even when it is reachable under aliases.

``_discover_printer_devices`` registers a discovered network printer under its
own identifier *and* under the ``printer_main`` alias, both pointing at the same
Device.  ``device_list`` used to emit one entry per dict key, so ``/api/status``
-- which is what the POS polls to enumerate devices -- showed a single printer
twice and every kitchen ticket was printed twice.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.device_manager import DeviceManager
from app.event_bus import EventBus
from app.models import Device


def _manager() -> DeviceManager:
    return DeviceManager(EventBus(), Path(tempfile.mkdtemp()), iot_identifier="box-test")


def _network_printer() -> Device:
    return Device(
        identifier="netprinter_aabbccddeeff",
        name="Epson Network Printer (192.168.1.45:9100)",
        type="printer",
        connection="network",
        subtype="receipt_printer",
        manufacturer="EPSON",
    )


def _display() -> Device:
    return Device(
        identifier="customer_display",
        name="Customer Display",
        type="display",
        connection="hdmi",
    )


class DeviceAdvertisementTests(unittest.TestCase):
    def _manager_with(self, devices) -> DeviceManager:
        manager = _manager()
        manager.devices = devices
        # Keep _refresh_devices from re-running real discovery over the fixture.
        manager._devices_cached_at = time.time()
        return manager

    def test_a_device_reachable_under_two_keys_is_listed_once(self):
        printer = _network_printer()
        manager = self._manager_with(
            {
                "netprinter_aabbccddeeff": printer,
                "printer_main": printer,  # the alias that caused the duplicate
                "customer_display": _display(),
            }
        )
        identifiers = [entry["device_identifier"] for entry in manager.device_list()]
        self.assertEqual(identifiers, ["netprinter_aabbccddeeff", "customer_display"])

    def test_the_alias_still_resolves_for_printing(self):
        """Deduplicating the advertisement must not break device lookup."""
        printer = _network_printer()
        manager = self._manager_with(
            {"netprinter_aabbccddeeff": printer, "printer_main": printer}
        )
        manager.device_list()
        self.assertIs(manager.devices.get("printer_main"), printer)
        self.assertIs(manager.devices.get("netprinter_aabbccddeeff"), printer)

    def test_independent_devices_are_all_kept(self):
        manager = self._manager_with(
            {
                "netprinter_aabbccddeeff": _network_printer(),
                "customer_display": _display(),
            }
        )
        identifiers = [entry["device_identifier"] for entry in manager.device_list()]
        self.assertEqual(sorted(identifiers), ["customer_display", "netprinter_aabbccddeeff"])

    def test_odoo_payload_also_has_one_entry_per_device(self):
        printer = _network_printer()
        manager = self._manager_with(
            {"netprinter_aabbccddeeff": printer, "printer_main": printer}
        )
        payload = manager.as_odoo_devices_payload()
        self.assertEqual(len(payload), 1)


if __name__ == "__main__":
    unittest.main()
