from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import tempfile
from time import time
import unittest

from app.device_manager import DeviceManager
from app.event_bus import EventBus
from app.models import Device


ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "app" / "device_manager.py",
    ROOT / "app" / "devices" / "discovery.py",
    ROOT / "app" / "receipts" / "processing.py",
    ROOT / "app" / "receipts" / "structured.py",
    ROOT / "app" / "printing" / "network_printer.py",
    ROOT / "app" / "printing" / "windows_printer.py",
    ROOT / "app" / "printing" / "barcode.py",
    ROOT / "app" / "printing" / "escpos.py",
    ROOT / "app" / "printing" / "image_renderer.py",
    ROOT / "app" / "printing" / "normalization.py",
    ROOT / "app" / "printing" / "product_parser.py",
    ROOT / "app" / "printing" / "receipt_metadata.py",
    ROOT / "app" / "printing" / "section_consumers.py",
    ROOT / "app" / "printing" / "text_layout.py",
]


class DeviceManagerArchitectureTests(unittest.TestCase):
    def test_device_manager_remains_a_small_orchestrator(self):
        manager = ROOT / "app" / "device_manager.py"
        self.assertLessEqual(len(manager.read_text(encoding="utf-8").splitlines()), 700)

    def test_mixin_methods_are_unique_and_core_contract_is_present(self):
        owners: dict[str, list[str]] = defaultdict(list)
        for path in MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
                for method in class_node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owners[method.name].append(f"{path.name}:{method.lineno}")

        duplicates = {name: locations for name, locations in owners.items() if len(locations) > 1}
        self.assertEqual(duplicates, {})
        required = {
            "execute",
            "_process_receipt_escpos",
            "_build_structured_receipt_lines",
            "_build_escpos_bytes",
            "_build_kitchen_escpos_bytes",
            "_send_raw_to_printer",
            "_send_raw_to_windows_printer",
            "_build_escpos_image",
            "_render_escpos_lines",
            "_encode_code128_values",
        }
        self.assertEqual(required - owners.keys(), set())

    def test_templated_receipts_skip_legacy_whitespace_normalization(self):
        path = ROOT / "app" / "receipts" / "processing.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_build_escpos_bytes"
        ]
        self.assertEqual(len(calls), 1)
        keyword = next(
            (item for item in calls[0].keywords if item.arg == "normalize_lines"),
            None,
        )
        self.assertIsNotNone(keyword)
        self.assertIsInstance(keyword.value, ast.UnaryOp)
        self.assertIsInstance(keyword.value.op, ast.Not)
        self.assertIsInstance(keyword.value.operand, ast.Name)
        self.assertEqual(keyword.value.operand.id, "skip_normalize")

    def test_odoo_device_identifiers_are_namespaced_per_box_and_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DeviceManager(
                EventBus(),
                Path(directory),
                iot_identifier="custom-iot-box-box1",
            )
            manager.devices = {
                "printer_rp_12n": Device(
                    identifier="printer_rp_12n",
                    name="RP-12N",
                    type="printer",
                    connection="direct",
                )
            }
            manager._devices_cached_at = time()

            payload = manager.as_odoo_devices_payload()
            external = "custom-iot-box-box1__printer_rp_12n"
            self.assertIn(external, payload)
            self.assertEqual(payload[external]["device_identifier"], external)
            self.assertEqual(manager.local_device_identifier(external), "printer_rp_12n")
            self.assertEqual(manager.local_device_identifier("printer_main"), "printer_main")


class LabelActionRoutingTests(unittest.TestCase):
    """A ``print_zpl`` action must reach the ZPL backend, not the default branch.

    The label/wristband client sends this action name; while no handler existed
    the request ended in the default branch, which reported success without
    printing anything.
    """

    ZPL = "^XA^CI28^PW406^LL300^FO16,16^A0N,32,32^FDLABEL^FS^XZ"

    def _manager(self, directory: str) -> DeviceManager:
        manager = DeviceManager(EventBus(), Path(directory), iot_identifier="custom-iot-box-box1")
        manager.devices = {
            "netprinter_aabbccddeeff": Device(
                identifier="netprinter_aabbccddeeff",
                name="ZEBRA GK420D (192.168.1.51:9100)",
                type="printer",
                connection="network",
                subtype="label_printer",
                metadata={
                    "raw_tcp_host": "192.168.1.51",
                    "raw_tcp_port": 9100,
                    "printer_protocol": "zpl",
                },
            )
        }
        return manager

    def test_print_zpl_is_routed_to_the_zpl_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            device = manager.devices["netprinter_aabbccddeeff"]

            normalized = manager._normalize_label_action(
                "owner", device, {"action": "print_zpl", "zpl": self.ZPL}
            )

            self.assertIsNotNone(normalized)
            self.assertEqual(normalized["action"], "print_receipt")
            self.assertEqual(normalized["receipt"], self.ZPL)
            self.assertEqual(
                manager._printer_receipt_handler(device, normalized), "_print_receipt_zpl"
            )

    def test_print_zpl_without_a_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            device = manager.devices["netprinter_aabbccddeeff"]

            for data in ({"action": "print_zpl"}, {"action": "print_zpl", "zpl": ""}):
                self.assertIsNone(manager._normalize_label_action("owner", device, data))

    def test_other_actions_pass_through_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            device = manager.devices["netprinter_aabbccddeeff"]
            data = {"action": "print_receipt_escpos", "receipt": {"order": {}}}

            self.assertIs(manager._normalize_label_action("owner", device, data), data)


if __name__ == "__main__":
    unittest.main()
