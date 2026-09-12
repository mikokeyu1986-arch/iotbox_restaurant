"""Unbinding must clear the machine configuration, not only the pairing.

The distinction is load-bearing: ``ConfigStore.reset_connection`` is also called
by the *failed* connect paths (``/api/connect``, ``/iot_drivers/connect_to_server``),
which must leave a working installation's printers, scale and VFD alone.  Only
``reset_for_unbind`` performs the full wipe.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.certificate_manager import CertificateManager
from app.config_store import ConfigStore


def _store(directory: str) -> ConfigStore:
    return ConfigStore(Path(directory) / "runtime_config.json")


def _pair(store: ConfigStore) -> None:
    store.connect_from_token_url(
        "https://resthong.oduo.es?token=secret-token&db_uuid=uuid-1&db_name=odoo_resthong"
    )


def _configure_machine(store: ConfigStore) -> None:
    store.update_local_config(
        printer_identifier="printer-main",
        primary_printer_queue="EPSON_TM_T20",
        enabled_printer_queues=["EPSON_TM_T20", "Zebra_ZD421"],
        printer_aliases={"aa:bb:cc:dd:ee:ff": "kitchen"},
        printer_language_overrides={"Zebra_ZD421": "zpl"},
        scale_port="/dev/ttyUSB0",
        scale_brand="zfoc",
        vfd_enabled=True,
        vfd_port="/dev/ttyUSB1",
        local_url="https://192.168.1.39:8398",
    )


class UnbindConfigResetTests(unittest.TestCase):
    def test_unbind_clears_the_machine_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _configure_machine(store)

            store.reset_for_unbind()

            config = store.get_local_config()
            self.assertEqual(config["printer_identifier"], "")
            self.assertEqual(config["primary_printer_queue"], "")
            self.assertEqual(config["enabled_printer_queues"], [])
            self.assertEqual(config["printer_aliases"], {})
            self.assertEqual(config["printer_language_overrides"], {})
            self.assertEqual(config["scale_port"], "")
            self.assertFalse(config["vfd_enabled"])
            self.assertEqual(config["vfd_port"], "")
            self.assertEqual(config["local_url"], "")

    def test_unbind_clears_the_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _pair(store)
            self.assertTrue(store.get_connection()["connected"])

            connection = store.reset_for_unbind()

            self.assertFalse(connection["connected"])
            self.assertEqual(connection["url"], "")
            self.assertEqual(connection["token"], "")
            self.assertEqual(store.get_connection(), connection)

    def test_unbind_preserves_the_box_identity(self):
        """Re-pairing to the same Odoo must stay the same IOTBOX device."""
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            identity = store.ensure_iot_identifier()
            self.assertTrue(identity)

            store.reset_for_unbind()

            self.assertEqual(store.get_local_config()["iot_identifier"], identity)

    def test_unbind_never_restores_the_plain_http_default(self):
        """``_default_local_config`` says plain_http, which the runtime dropped.

        Starting plain HTTP is what invalidated the certificates clients pin, so
        the reset must not be able to put a box back onto it.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            store.update_local_config(ssl_engine="plain_http", service_protocol="http")

            reset = store.reset_local_config()

            self.assertEqual(reset["ssl_engine"], "secure_https")
            self.assertEqual(reset["service_protocol"], "https")

    def test_failed_connect_reset_does_not_touch_the_machine_configuration(self):
        """A failed pairing must not wipe a working installation."""
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _configure_machine(store)

            store.reset_connection(message="Odoo /iot/setup failed")

            config = store.get_local_config()
            self.assertEqual(config["printer_identifier"], "printer-main")
            self.assertEqual(config["scale_port"], "/dev/ttyUSB0")
            self.assertEqual(config["printer_aliases"], {"aa:bb:cc:dd:ee:ff": "kitchen"})

    def test_unbind_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _configure_machine(store)
            identity = store.ensure_iot_identifier()

            store.reset_for_unbind()
            after_first = store.get_local_config()
            store.reset_for_unbind()

            self.assertEqual(store.get_local_config(), after_first)
            self.assertEqual(store.get_local_config()["iot_identifier"], identity)


class UnbindCertificateTests(unittest.TestCase):
    def test_unbind_removes_the_server_certificate_but_keeps_the_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")
            manager.ensure()
            for path in (
                manager.key_path,
                manager.crt_path,
                manager.p12_path,
                manager.ca_crt_path,
                manager.ca_key_path,
            ):
                self.assertTrue(path.exists(), f"{path} should exist before the unbind")

            removed = manager.clear_server_certificate()

            self.assertCountEqual(removed, ["iotbox.key", "iotbox.crt", "iotbox.p12"])
            self.assertFalse(manager.crt_path.exists())
            self.assertFalse(manager.key_path.exists())
            self.assertFalse(manager.p12_path.exists())
            # Clients pin the CA; replacing it invalidates every one of them.
            self.assertTrue(manager.ca_crt_path.exists())
            self.assertTrue(manager.ca_key_path.exists())

    def test_re_issuing_after_an_unbind_keeps_the_pinned_ca(self):
        """The point of keeping the CA: the pin survives the unbind."""
        with tempfile.TemporaryDirectory() as directory:
            certs = Path(directory)
            first = CertificateManager(certs, iot_ip="192.168.1.39:8398")
            first.ensure()
            ca_before = first.ca_crt_path.read_bytes()

            first.clear_server_certificate()

            second = CertificateManager(certs, iot_ip="192.168.1.39:8398")
            second.ensure()

            self.assertTrue(second.crt_path.exists(), "the server certificate should be re-issued")
            self.assertEqual(second.ca_crt_path.read_bytes(), ca_before)

    def test_clearing_tolerates_an_already_empty_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")
            self.assertEqual(manager.clear_server_certificate(), [])


class UnbindEndpointTests(unittest.IsolatedAsyncioTestCase):
    """``/api/disconnect`` is what both the web UI and the GUI actually call."""

    async def test_api_disconnect_clears_config_and_keeps_the_templates(self):
        import app.kitchen_template_store as kitchen_store
        import app.main as main
        import app.receipt_template_store as receipt_store

        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _pair(store)
            _configure_machine(store)
            identity = store.ensure_iot_identifier()

            display_state = dict(main._customer_display_state)
            main._set_customer_display_state(
                {"action": "set", "data": {"lines": [{"productName": "Café solo"}]}}
            )
            self.addCleanup(main._customer_display_state.update, display_state)
            revision_before = main._customer_display_state["revision"]

            with (
                patch.object(main, "config_store", store),
                patch.object(main.cloud_bridge, "config_store", store),
                # Replace the whole manager so the test cannot touch the real
                # certificates directory.
                patch.object(main, "certificate_manager") as certificate_manager,
                patch.object(receipt_store, "reset_template") as reset_receipt,
                patch.object(kitchen_store, "reset_kitchen_template") as reset_kitchen,
            ):
                certificate_manager.clear_server_certificate.return_value = ["iotbox.crt"]
                result = await main.api_disconnect()

            self.assertEqual(result["status"], "success")
            self.assertFalse(result["server_connection"]["connected"])
            self.assertEqual(result["local_config"]["printer_identifier"], "")
            self.assertEqual(result["local_config"]["scale_port"], "")
            self.assertTrue(result["local_config"]["iot_identifier"])
            self.assertEqual(result["local_config"]["iot_identifier"], identity)
            certificate_manager.clear_server_certificate.assert_called_once()
            # The receipt layouts are artwork, not machine configuration.
            reset_receipt.assert_not_called()
            reset_kitchen.assert_not_called()
            # A previous customer's order must not stay on the display.
            self.assertEqual(main._customer_display_state["payload"], {})
            self.assertGreater(main._customer_display_state["revision"], revision_before)


if __name__ == "__main__":
    unittest.main()
