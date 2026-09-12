from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from app.devices.discovery import DeviceDiscoveryMixin
from app.models import Device
from app.printing.network_printer import NetworkPrinterMixin


class DiscoveryHarness(DeviceDiscoveryMixin):
    def __init__(self, config: dict) -> None:
        self._config = config

    def local_config_getter(self) -> dict:
        return self._config

    @staticmethod
    def _is_windows_native_printing_available() -> bool:
        return False


class NetworkPrinterHarness(NetworkPrinterMixin):
    def __init__(self, config: dict) -> None:
        self._config = config

    def local_config_getter(self) -> dict:
        return self._config

    @staticmethod
    def _configured_printer_identifier() -> str:
        return ""


class EpsonNetworkDiscoveryTests(unittest.TestCase):
    def test_reachable_tcp_9100_host_is_registered_as_raw_network_printer(self):
        config = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": ["192.168.10.25"],
            "epson_discovery_subnets": [],
        }
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ), patch.object(
            DiscoveryHarness, "_arp_mac_address", return_value=""
        ), patch.object(DiscoveryHarness, "_detect_tcp_printer_language", return_value={}):
            manager = DiscoveryHarness(config)
            devices = manager._discover_printer_devices()

        device = devices["epson_tcp_192_168_10_25"]
        self.assertEqual(device.connection, "network")
        self.assertEqual(device.manufacturer, "EPSON")
        self.assertEqual(device.metadata["raw_tcp_host"], "192.168.10.25")
        self.assertEqual(device.metadata["raw_tcp_port"], 9100)
        self.assertIs(devices["printer_main"], device)

    def test_zpl_host_is_registered_as_zebra_label_printer(self):
        config = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": ["192.168.10.25"],
            "epson_discovery_subnets": [],
        }
        identity = {
            "language": "zpl",
            "model": "GK420d-200dpi",
            "brand": "ZEBRA",
            "source": "probe",
        }
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ), patch.object(
            DiscoveryHarness, "_arp_mac_address", return_value=""
        ), patch.object(DiscoveryHarness, "_detect_tcp_printer_language", return_value=identity):
            manager = DiscoveryHarness(config)
            devices = manager._discover_printer_devices()

        device = devices["epson_tcp_192_168_10_25"]
        self.assertEqual(device.name, "ZEBRA GK420D (192.168.10.25:9100)")
        self.assertEqual(device.manufacturer, "ZEBRA")
        # The "type" Odoo stores for this device: a label printer, not an
        # Epson-compatible receipt printer.
        self.assertEqual(device.subtype, "label_printer")
        self.assertEqual(device.metadata["printer_protocol"], "zpl")
        self.assertEqual(device.metadata["printer_model"], "GK420d-200dpi")
        self.assertIs(devices["printer_main"], device)

    def test_device_specific_tcp_port_is_used_for_printing(self):
        config = {"epson_discovery_enabled": False, "raw_printer_port": 9999}
        manager = NetworkPrinterHarness(config)
        device = Device(
            identifier="epson_tcp_192_168_10_25",
            name="Epson Network Printer",
            type="printer",
            connection="network",
            metadata={"raw_tcp_host": "192.168.10.25", "raw_tcp_port": 9100},
        )
        self.assertEqual(manager._raw_tcp_endpoint(device), ("192.168.10.25", 9100))


class StablePrinterIdentityTests(unittest.TestCase):
    """A printer keeps its identifier when DHCP moves it to another address."""

    MAC = "00074d622ac8"

    def _discover(
        self, hosts: list[str], mac_by_host: dict[str, str], config: dict | None = None
    ) -> dict:
        base = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": hosts,
            "epson_discovery_subnets": [],
        }
        base.update(config or {})
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ), patch.object(
            DiscoveryHarness,
            "_arp_mac_address",
            side_effect=lambda host: mac_by_host.get(host, ""),
        ), patch.object(DiscoveryHarness, "_detect_tcp_printer_language", return_value={}):
            return DiscoveryHarness(base)._discover_printer_devices()

    def test_identifier_follows_the_hardware_not_the_address(self):
        first = self._discover(["192.168.10.25"], {"192.168.10.25": self.MAC})
        moved = self._discover(["192.168.10.77"], {"192.168.10.77": self.MAC})

        self.assertIn(f"netprinter_{self.MAC}", first)
        self.assertEqual(sorted(first), sorted(moved), "a new lease must not rename the printer")

    def test_identifier_falls_back_to_the_address_without_a_mac(self):
        devices = self._discover(["192.168.10.25"], {})
        self.assertIn("epson_tcp_192_168_10_25", devices)

    def test_alias_overrides_the_mac_identifier_and_survives_a_move(self):
        alias = {"printer_aliases": {self.MAC: "Zebra Kitchen"}}
        for host in ("192.168.10.25", "192.168.10.77"):
            devices = self._discover([host], {host: self.MAC}, alias)
            self.assertIn("zebra_kitchen", devices)

    def test_alias_accepts_any_mac_spelling(self):
        for spelling in ("00:07:4d:62:2a:c8", "00-07-4D-62-2A-C8", "0:7:4d:62:2a:c8"):
            devices = self._discover(
                ["192.168.10.25"],
                {"192.168.10.25": self.MAC},
                {"printer_aliases": {spelling: "zebra_kitchen"}},
            )
            self.assertIn("zebra_kitchen", devices, spelling)

    def test_mac_shared_by_several_hosts_falls_back_to_the_address(self):
        # Reached through a router, every host resolves to the router's MAC, so
        # the MAC identifies none of them.
        shared = {"192.168.10.25": self.MAC, "192.168.10.26": self.MAC}
        devices = self._discover(["192.168.10.25", "192.168.10.26"], shared)

        self.assertIn("epson_tcp_192_168_10_25", devices)
        self.assertIn("epson_tcp_192_168_10_26", devices)
        self.assertNotIn(f"netprinter_{self.MAC}", devices)

    def test_arp_output_is_parsed_on_posix_and_windows(self):
        layouts = [
            "? (192.168.10.25) at 0:7:4d:62:2a:c8 on en0 ifscope [ethernet]\n",  # macOS
            "? (192.168.10.25) at 00:07:4d:62:2a:c8 [ether] on eth0\n",  # Linux
            "  192.168.10.25          00-07-4d-62-2a-c8     dynamic\n",  # Windows
        ]
        for output in layouts:
            with patch(
                "app.devices.discovery.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
            ):
                self.assertEqual(
                    DiscoveryHarness({})._arp_mac_address("192.168.10.25"), self.MAC, output
                )

    def test_unresolved_arp_entries_yield_no_mac(self):
        layouts = [
            "? (192.168.10.25) at (incomplete) on en0 ifscope [ethernet]\n",  # macOS
            "192.168.10.25 (192.168.10.25) -- no entry\n",  # no neighbour
            "",  # arp unavailable
        ]
        for output in layouts:
            with patch(
                "app.devices.discovery.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
            ):
                self.assertEqual(
                    DiscoveryHarness({})._arp_mac_address("192.168.10.25"), "", output
                )

    def test_unreadable_config_never_raises(self):
        for value in (None, [], "x", {"": ""}, {"00:07:4d:62:2a:c8": ""}):
            self.assertEqual(
                DiscoveryHarness({"printer_aliases": value})._configured_printer_aliases(),
                {},
            )


if __name__ == "__main__":
    unittest.main()
