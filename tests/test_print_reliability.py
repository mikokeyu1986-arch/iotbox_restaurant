"""A print that did not reach the paper must not be reported as printed.

Two independent failures produced the same silent loss (observed 23:29:48 on the
production box): the printer fell back to a backend-less placeholder when a
discovery probe failed, and the print handlers reported success regardless of
the outcome.  The kitchen never received the ticket and the caller was told it
had printed, so nothing retried.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.device_manager import DeviceManager
from app.event_bus import EventBus
from app.models import Device


class PrintResultReportingTests(unittest.IsolatedAsyncioTestCase):
    """The handlers must return the real outcome."""

    def _manager(self, directory: str) -> DeviceManager:
        return DeviceManager(EventBus(), Path(directory), iot_identifier="box-test")

    def _device(self) -> Device:
        return Device(
            identifier="netprinter_aabbccddeeff",
            name="Epson",
            type="printer",
            connection="network",
            subtype="receipt_printer",
        )

    async def _run(self, handler_name: str, process_name: str, payload: dict):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            setattr(manager, process_name, lambda *a, **k: payload)
            manager.event_bus.publish = AsyncMock()
            return await getattr(manager, handler_name)("owner", self._device(), {})

    async def test_escpos_failure_is_reported(self):
        result = await self._run(
            "_print_receipt_escpos",
            "_process_receipt_escpos",
            {"ok": False, "error": "ERROR_PRINTER", "target": "/tmp/x.bin"},
        )
        self.assertFalse(result, "a failed ESC/POS print must not report success")

    async def test_escpos_success_is_reported(self):
        result = await self._run(
            "_print_receipt_escpos",
            "_process_receipt_escpos",
            {"ok": True, "error": None, "target": "/tmp/x.bin"},
        )
        self.assertTrue(result)

    async def test_zpl_failure_is_reported(self):
        result = await self._run(
            "_print_receipt_zpl",
            "_process_receipt_zpl",
            {"ok": False, "error": "ERROR_PRINTER", "target": "/tmp/x.zpl"},
        )
        self.assertFalse(result)

    async def test_native_image_failure_is_reported(self):
        result = await self._run(
            "_print_receipt_native_image",
            "_process_receipt_native_image",
            {"ok": False, "error": "ERROR_PRINTER", "target": "/tmp/x.png"},
        )
        self.assertFalse(result)

    async def test_an_error_event_is_still_published(self):
        """Reporting the failure must not remove the event the UI listens for."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager._process_receipt_escpos = lambda *a, **k: {
                "ok": False, "error": "ERROR_PRINTER", "target": "/tmp/x.bin",
            }
            published = []

            async def capture(event):
                published.append(event)

            manager.event_bus.publish = capture
            await manager._print_receipt_escpos("owner", self._device(), {})
            self.assertEqual([e.status for e in published], ["error"])


class RememberedPrinterTests(unittest.TestCase):
    """A failed discovery probe must not take a working printer away."""

    def _manager(self, directory: str) -> DeviceManager:
        return DeviceManager(EventBus(), Path(directory), iot_identifier="box-test")

    def _network_printer(self) -> Device:
        return Device(
            identifier="netprinter_aabbccddeeff",
            name="Epson Network Printer (192.168.1.45:9100)",
            type="printer",
            connection="network",
            subtype="receipt_printer",
        )

    def test_printer_main_keeps_the_last_discovered_printer(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager._is_windows_native_printing_available = lambda: False
            found = self._network_printer()

            manager._discover_epson_network_printers = lambda: {found.identifier: found}
            first = manager._discover_printer_devices()
            self.assertIs(first["printer_main"], found)
            self.assertIs(manager._last_known_network_printer, found)

            # The next probe finds nothing: the printer must not become the
            # backend-less placeholder.
            manager._discover_epson_network_printers = lambda: {}
            second = manager._discover_printer_devices()
            self.assertIs(second["printer_main"], found)
            self.assertEqual(second["printer_main"].connection, "network")

    def test_without_any_discovery_the_placeholder_is_still_used(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager._is_windows_native_printing_available = lambda: False
            manager._discover_epson_network_printers = lambda: {}
            printers = manager._discover_printer_devices()
            self.assertEqual(printers["printer_main"].identifier, "printer_main")


if __name__ == "__main__":
    unittest.main()
