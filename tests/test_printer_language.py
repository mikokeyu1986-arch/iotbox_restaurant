from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.device_manager import DeviceManager
from app.devices.discovery import DeviceDiscoveryMixin
from app.models import Device
from app.printing.printer_language import (
    PRINTER_LANGUAGE_ESCPOS,
    PRINTER_LANGUAGE_ZPL,
    PRINTER_SUBTYPE_LABEL,
    PRINTER_SUBTYPE_RECEIPT,
    extract_zpl_text,
    looks_like_zpl,
    marker_matches_language,
    normalize_language,
    printer_brand,
    printer_subtype,
    probe_printer_identity_over_tcp,
    probe_printer_language_over_tcp,
    response_is_zpl,
    window_driver_language,
    windows_queue_identity,
    zebra_matches,
    zebra_model_name,
)


class _FakeSocket:
    """Minimal socket stub: each recv() pops the next canned response."""

    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.sent: list[bytes] = []

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def settimeout(self, _value) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _size: int) -> bytes:
        if self._responses:
            return self._responses.pop(0)
        return b""


class DiscoveryHarness(DeviceDiscoveryMixin):
    def __init__(self, config: dict) -> None:
        self._config = config

    def local_config_getter(self) -> dict:
        return self._config

    @staticmethod
    def _is_windows_native_printing_available() -> bool:
        return False


class WindowsQueueDiscoveryHarness(DeviceDiscoveryMixin):
    def __init__(self, config: dict, queues: list[str]) -> None:
        self._config = config
        self._queues = queues

    def local_config_getter(self) -> dict:
        return self._config

    def _windows_printer_queues(self) -> list[str]:
        return list(self._queues)

    @staticmethod
    def _is_windows_native_printing_available() -> bool:
        return False


class PrinterLanguageHelperTests(unittest.TestCase):
    def test_normalize_language_accepts_common_aliases(self):
        self.assertEqual(normalize_language("ZPL"), PRINTER_LANGUAGE_ZPL)
        self.assertEqual(normalize_language("zpl2"), PRINTER_LANGUAGE_ZPL)
        self.assertEqual(normalize_language("ESC/POS"), PRINTER_LANGUAGE_ESCPOS)
        self.assertEqual(normalize_language("unknown"), "")

    def test_looks_like_zpl_detects_labels_and_queries(self):
        self.assertTrue(looks_like_zpl("^XA^FDHello^XZ"))
        self.assertTrue(looks_like_zpl("~HI"))
        self.assertFalse(looks_like_zpl("Gracias por su compra"))
        self.assertFalse(looks_like_zpl(""))

    def test_driver_names_identify_zpl_label_printers(self):
        self.assertEqual(marker_matches_language("ZDesigner ZD220-203dpi ZPL"), PRINTER_LANGUAGE_ZPL)
        self.assertEqual(marker_matches_language("Zebra Technologies"), PRINTER_LANGUAGE_ZPL)
        self.assertEqual(marker_matches_language("RP-12N"), "")
        self.assertEqual(window_driver_language("Zebra ZD421", "ZDesigner", "USB001"), PRINTER_LANGUAGE_ZPL)
        self.assertEqual(window_driver_language("POS-80", "Generic / Text Only", "USB002"), "")

    def test_zebra_model_names_are_recognised_without_brand_keywords(self):
        # A queue may only expose the model, without "Zebra" or "ZPL" in it.
        for name in ("ZD220", "ZT411", "GK420d", "GX430t", "ZQ520", "Zebra ZD421"):
            with self.subTest(name=name):
                self.assertTrue(zebra_matches(name))
                self.assertEqual(marker_matches_language(name), PRINTER_LANGUAGE_ZPL)
        self.assertFalse(zebra_matches("RP-12N"))
        self.assertFalse(zebra_matches("Epson TM-T20III"))

    def test_zpl_printers_are_published_as_label_printers(self):
        # Odoo's iot.device.subtype vocabulary is receipt_printer /
        # label_printer / office_printer: a ZPL printer is a label printer.
        self.assertEqual(printer_subtype(PRINTER_LANGUAGE_ZPL), PRINTER_SUBTYPE_LABEL)
        self.assertEqual(printer_subtype("ZPL"), PRINTER_SUBTYPE_LABEL)
        self.assertEqual(printer_subtype(PRINTER_LANGUAGE_ESCPOS), PRINTER_SUBTYPE_RECEIPT)
        self.assertEqual(printer_subtype(""), PRINTER_SUBTYPE_RECEIPT)
        self.assertEqual(printer_subtype(None), PRINTER_SUBTYPE_RECEIPT)

    def test_zebra_model_name_is_extracted_for_device_labels(self):
        self.assertEqual(zebra_model_name("ZTC ZD220-203dpi ZPL"), "ZD220")
        self.assertEqual(zebra_model_name("ZDesigner ZD421-203dpi ZPL"), "ZD421")
        self.assertEqual(zebra_model_name("Epson TM-T20III"), "")

    def test_brand_is_reported_for_label_printer_vendors(self):
        self.assertEqual(printer_brand("ZDesigner ZD220-203dpi ZPL"), "ZEBRA")
        self.assertEqual(printer_brand("ZTC ZD220-203dpi ZPL"), "ZEBRA")
        self.assertEqual(printer_brand("Godex EZ120"), "GODEX")
        self.assertEqual(printer_brand("Epson TM-T20III"), "")

    def test_probe_response_classification(self):
        self.assertTrue(response_is_zpl(b"ZTC ZD220-203dpi ZPL,3.10.0,14,203"))
        # Zebra firmware may omit the trailing numeric fields.
        self.assertTrue(response_is_zpl(b"ZTC ZD220-203dpi ZPL,V92.21.02Z"))
        self.assertTrue(response_is_zpl(b"Zebra ZD421"))
        self.assertFalse(response_is_zpl(b"\x12"))
        self.assertFalse(response_is_zpl(b"~HI"))
        self.assertFalse(response_is_zpl(b""))


class ExtractZplPayloadTests(unittest.TestCase):
    def test_explicit_zpl_key_is_returned(self):
        payload = {"zpl": "^XA^FDOrder 12^XZ"}
        self.assertEqual(extract_zpl_text(payload), "^XA^FDOrder 12^XZ")

    def test_receipt_string_is_returned_for_zpl_printers(self):
        self.assertEqual(extract_zpl_text("^XA^FDHi^XZ"), "^XA^FDHi^XZ")
        # A ZPL printer forwards whatever Odoo sent, even plain text.
        self.assertEqual(extract_zpl_text("mesa 4", allow_plain=True), "mesa 4")
        self.assertEqual(extract_zpl_text("mesa 4"), "")

    def test_lines_payload_with_zpl_text_is_returned(self):
        payload = {"lines": [{"text": "^XA^FDTable 4^XZ"}]}
        self.assertEqual(extract_zpl_text(payload), "^XA^FDTable 4^XZ")

    def test_regular_receipt_lines_are_not_treated_as_zpl(self):
        payload = {"lines": [{"text": "Total 12,50 EUR"}, {"text": "Gracias"}]}
        self.assertEqual(extract_zpl_text(payload), "")
        self.assertEqual(extract_zpl_text(payload, allow_plain=True), "")

    def test_receipt_image_data_url_is_not_treated_as_zpl(self):
        payload = {"receipt": "data:image/png;base64,iVBORw0KGgo="}
        self.assertEqual(extract_zpl_text(payload, allow_plain=True), "")


class ProbeLanguageTests(unittest.TestCase):
    def test_escpos_status_byte_marks_printer_as_escpos(self):
        fake = _FakeSocket([b"\x12"])
        with patch("app.printing.printer_language.socket.create_connection", return_value=fake):
            language = probe_printer_language_over_tcp("192.168.1.50", 9100, 0.5)
        self.assertEqual(language, PRINTER_LANGUAGE_ESCPOS)
        # The silent ESC/POS status query is sent first; ~HI is never sent.
        self.assertEqual(len(fake.sent), 1)

    def test_zpl_host_identification_marks_printer_as_zpl(self):
        fake = _FakeSocket([b"", b"ZTC ZD220-203dpi ZPL,3.10.0,14,203"])
        with patch("app.printing.printer_language.socket.create_connection", return_value=fake):
            language = probe_printer_language_over_tcp("192.168.1.51", 9100, 0.5)
        self.assertEqual(language, PRINTER_LANGUAGE_ZPL)
        self.assertEqual(fake.sent[-1], b"~HI")

    def test_zebra_probe_reports_brand_and_model(self):
        fake = _FakeSocket([b"", b"ZTC ZD220-203dpi ZPL,V92.21.02Z,9600,203"])
        with patch("app.printing.printer_language.socket.create_connection", return_value=fake):
            identity = probe_printer_identity_over_tcp("192.168.1.53", 9100, 0.5)
        self.assertEqual(identity["language"], PRINTER_LANGUAGE_ZPL)
        self.assertEqual(identity["brand"], "ZEBRA")
        self.assertEqual(identity["model"], "ZTC ZD220-203dpi ZPL")

    def test_escpos_probe_reports_no_model(self):
        fake = _FakeSocket([b"\x12"])
        with patch("app.printing.printer_language.socket.create_connection", return_value=fake):
            identity = probe_printer_identity_over_tcp("192.168.1.54", 9100, 0.5)
        self.assertEqual(identity, {"language": PRINTER_LANGUAGE_ESCPOS})

    def test_unreachable_printer_is_not_classified(self):
        with patch(
            "app.printing.printer_language.socket.create_connection",
            side_effect=OSError("unreachable"),
        ):
            self.assertEqual(probe_printer_language_over_tcp("192.168.1.52", 9100, 0.5), "")
            self.assertEqual(probe_printer_identity_over_tcp("192.168.1.52", 9100, 0.5), {})


class WindowsQueueIdentityTests(unittest.TestCase):
    def test_zebra_queue_is_identified_from_its_name(self):
        identity = windows_queue_identity("ZD421")
        self.assertEqual(identity["language"], PRINTER_LANGUAGE_ZPL)
        self.assertEqual(identity["brand"], "ZEBRA")
        self.assertEqual(identity["model"], "ZD421")

    def test_receipt_queue_is_not_reported_as_zpl(self):
        self.assertEqual(windows_queue_identity("POS-80"), {})

    def test_blank_queue_name_is_ignored(self):
        self.assertEqual(windows_queue_identity("   "), {})


class DiscoveryLanguageDetectionTests(unittest.TestCase):
    def test_zebra_network_printer_is_registered_with_brand_and_model(self):
        config = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": ["192.168.10.25"],
            "epson_discovery_subnets": [],
        }
        identity = {
            "language": PRINTER_LANGUAGE_ZPL,
            "source": "probe",
            "model": "ZTC ZD220-203dpi ZPL",
            "brand": "ZEBRA",
        }
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ), patch.object(DiscoveryHarness, "_detect_tcp_printer_language", return_value=identity):
            manager = DiscoveryHarness(config)
            devices = manager._discover_printer_devices()

        device = devices["epson_tcp_192_168_10_25"]
        self.assertEqual(device.metadata["printer_protocol"], PRINTER_LANGUAGE_ZPL)
        self.assertEqual(device.metadata["printer_protocol_source"], "probe")
        self.assertEqual(device.metadata["printer_brand"], "ZEBRA")
        self.assertEqual(device.metadata["printer_model"], "ZTC ZD220-203dpi ZPL")
        self.assertEqual(device.manufacturer, "ZEBRA")
        self.assertEqual(device.name, "ZEBRA ZD220 (192.168.10.25:9100)")

    def test_network_printer_defaults_to_escpos_when_probe_is_inconclusive(self):
        config = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": ["192.168.10.26"],
            "epson_discovery_subnets": [],
        }
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ), patch.object(DiscoveryHarness, "_detect_tcp_printer_language", return_value={}):
            manager = DiscoveryHarness(config)
            devices = manager._discover_printer_devices()

        device = devices["epson_tcp_192_168_10_26"]
        self.assertEqual(device.metadata["printer_protocol"], PRINTER_LANGUAGE_ESCPOS)
        self.assertEqual(device.metadata["printer_protocol_source"], "default")
        self.assertNotIn("printer_brand", device.metadata)
        self.assertEqual(device.manufacturer, "EPSON")
        self.assertEqual(device.name, "Epson Network Printer (192.168.10.26:9100)")

    def test_windows_queue_uses_driver_detection(self):
        config = {"enabled_printer_queues": ["ZDesigner ZD220"]}
        identity = {
            "language": PRINTER_LANGUAGE_ZPL,
            "brand": "ZEBRA",
            "model": "ZD220",
            "driver": "ZDesigner ZD220-203dpi ZPL",
        }
        manager = WindowsQueueDiscoveryHarness(config, ["ZDesigner ZD220"])
        with patch("app.devices.discovery.windows_queue_identity", return_value=identity) as detector:
            devices = manager._discover_windows_printer_devices()

        detector.assert_called_once()
        main = devices["printer_main"]
        self.assertEqual(main.metadata["printer_protocol"], PRINTER_LANGUAGE_ZPL)
        self.assertEqual(main.metadata["printer_protocol_source"], "driver")
        self.assertEqual(main.metadata["printer_brand"], "ZEBRA")
        self.assertEqual(main.metadata["printer_model"], "ZD220")
        self.assertEqual(main.manufacturer, "ZEBRA")

    def test_manual_override_wins_over_detection(self):
        config = {"printer_language_overrides": {"ZDesigner ZD220": "escpos"}}
        manager = WindowsQueueDiscoveryHarness(config, ["ZDesigner ZD220"])
        with patch("app.devices.discovery.windows_queue_identity") as detector:
            devices = manager._discover_windows_printer_devices()

        detector.assert_not_called()
        main = devices["printer_main"]
        self.assertEqual(main.metadata["printer_protocol"], PRINTER_LANGUAGE_ESCPOS)
        self.assertEqual(main.metadata["printer_protocol_source"], "manual")
        self.assertNotIn("printer_brand", main.metadata)
        self.assertIsNone(main.manufacturer)


class ReceiptHandlerSelectionTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.spool_dir = Path(self._tempdir.name)

    def _manager(self, config: dict) -> DeviceManager:
        return DeviceManager(
            event_bus=MagicMock(),
            spool_dir=self.spool_dir,
            local_config_getter=lambda: config,
        )

    @staticmethod
    def _printer(protocol: str) -> Device:
        return Device(
            identifier="printer_main",
            name="ZDesigner ZD220",
            type="printer",
            connection="direct",
            subtype="receipt_printer",
            metadata={"windows_printer": "ZDesigner ZD220", "printer_protocol": protocol},
        )

    def test_zpl_printer_with_zpl_payload_uses_zpl_handler(self):
        manager = self._manager({})
        handler = manager._printer_receipt_handler(
            self._printer(PRINTER_LANGUAGE_ZPL), {"receipt": "^XA^FDTable 4^XZ"}
        )
        self.assertEqual(handler, "_print_receipt_zpl")

    def test_zpl_printer_without_zpl_payload_falls_back_to_escpos(self):
        manager = self._manager({})
        handler = manager._printer_receipt_handler(
            self._printer(PRINTER_LANGUAGE_ZPL), {"receipt": {"lines": [{"text": "Total 12,50"}]}}
        )
        self.assertEqual(handler, "_print_receipt_escpos")

    def test_escpos_printer_keeps_escpos_handler(self):
        manager = self._manager({})
        handler = manager._printer_receipt_handler(
            self._printer(PRINTER_LANGUAGE_ESCPOS), {"receipt": "^XA^FDTable 4^XZ"}
        )
        self.assertEqual(handler, "_print_receipt_escpos")

    def test_configuration_override_forces_zpl_handler(self):
        manager = self._manager({"printer_language_overrides": {"printer_main": "zpl"}})
        device = self._printer(PRINTER_LANGUAGE_ESCPOS)
        self.assertEqual(manager._device_printer_language(device), PRINTER_LANGUAGE_ZPL)
        handler = manager._printer_receipt_handler(device, {"receipt": "^XA^FDTable 4^XZ"})
        self.assertEqual(handler, "_print_receipt_zpl")


if __name__ == "__main__":
    unittest.main()
