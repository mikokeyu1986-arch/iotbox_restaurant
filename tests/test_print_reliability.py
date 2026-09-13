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

    async def test_full_printer_queue_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            queue = asyncio.Queue(maxsize=1)
            await queue.put({"already": "queued"})
            manager._ensure_printer_action_queue = lambda identifier: queue
            manager.event_bus.publish = AsyncMock()

            result = await manager._queue_printer_action(
                "owner", self._device(), {"action": "print_receipt_escpos"},
                "_print_receipt_escpos",
            )

            self.assertFalse(result)
            self.assertEqual(manager.event_bus.publish.await_args.args[0].message, "ERROR_QUEUE_FULL")

    async def test_worker_exception_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)

            async def broken_handler(*args):
                raise RuntimeError("broken printer")

            manager._broken_handler = broken_handler
            manager.event_bus.publish = AsyncMock()
            result = await manager._queue_printer_action(
                "owner", self._device(), {"action": "print_receipt_escpos"},
                "_broken_handler",
            )
            await manager.shutdown()

            self.assertFalse(result)

    async def test_shutdown_rejects_jobs_that_never_reached_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            queue = asyncio.Queue()
            future = asyncio.get_running_loop().create_future()
            await queue.put({"future": future})
            manager._printer_action_queues["printer-1"] = queue

            await manager.shutdown()

            self.assertFalse(await future)
            self.assertEqual(queue.qsize(), 0)

    def test_missing_portal_qr_is_generated_without_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            lines = manager._ensure_receipt_qr_line([
                {"text": "https://example.com/pos/ticket/123"},
            ])

            self.assertEqual(lines[0]["image_kind"], "qr")
            self.assertIn("barcode_type=QR", lines[0]["src"])


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
