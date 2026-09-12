from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
import zipfile

from app.config_store import ConfigStore
from app.certificate_manager import CertificateManager
from app.updater import UpdateManager, VersionInfo


class RuntimeSafetyTests(unittest.TestCase):
    def test_p12_password_is_random_and_persisted_per_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            first = CertificateManager(Path(directory), iot_ip="127.0.0.1:8398")
            self.assertTrue(first._ensure_p12_password())
            self.assertGreaterEqual(len(first.p12_password), 20)

            second = CertificateManager(Path(directory), iot_ip="127.0.0.1:8398")
            self.assertFalse(second._ensure_p12_password())
            self.assertEqual(second.p12_password, first.p12_password)

    def test_certificate_is_reused_then_renewed_for_changed_lan_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            certs = Path(directory)
            first = CertificateManager(certs, iot_ip="192.168.1.20:8398")
            first.ensure()
            original_certificate = first.crt_path.read_bytes()

            same_ip = CertificateManager(certs, iot_ip="192.168.1.20:8398")
            same_ip.ensure()
            self.assertEqual(same_ip.crt_path.read_bytes(), original_certificate)

            changed_ip = CertificateManager(certs, iot_ip="192.168.1.21:8398")
            changed_ip.ensure()
            self.assertNotEqual(changed_ip.crt_path.read_bytes(), original_certificate)
            decoded = ssl._ssl._test_decode_cert(os.fspath(changed_ip.crt_path))
            self.assertIn(
                ("IP Address", "192.168.1.21"),
                decoded.get("subjectAltName", ()),
            )

    def test_public_connection_never_exposes_pairing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps({
                "server_connection": {
                    "connected": True,
                    "url": "https://odoo.example",
                    "token": "secret-token",
                    "db_uuid": "db-uuid",
                }
            }), encoding="utf-8")
            store = ConfigStore(path)

            self.assertEqual(store.get_connection()["token"], "secret-token")
            self.assertNotIn("token", store.get_public_connection())

    def test_blank_installations_generate_distinct_iot_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ConfigStore(root / "first.json").ensure_iot_identifier()
            second = ConfigStore(root / "second.json").ensure_iot_identifier()

            self.assertRegex(first, r"^custom-iot-box-[0-9a-f]{12}$")
            self.assertRegex(second, r"^custom-iot-box-[0-9a-f]{12}$")
            self.assertNotEqual(first, second)

    def test_historical_packaged_identifier_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps({
                "server_connection": {"connected": False},
                "local_config": {"iot_identifier": "custom-iot-box-a9f549a11170"},
            }), encoding="utf-8")

            identifier = ConfigStore(path).ensure_iot_identifier()

            self.assertNotEqual(identifier, "custom-iot-box-a9f549a11170")
            self.assertRegex(identifier, r"^custom-iot-box-[0-9a-f]{12}$")

    def test_updater_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.zip"
            destination = root / "extract"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "unsafe")

            manager = UpdateManager("1.0", root / "application")
            with self.assertRaisesRegex(ValueError, "越界路径"):
                manager._extract(archive, destination)
            self.assertFalse((root / "outside.txt").exists())

    def test_updater_extracts_normal_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "normal.zip"
            destination = root / "extract"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("release/README.txt", "ok")

            manager = UpdateManager("1.0", root / "application")
            extracted = manager._extract(archive, destination)
            self.assertEqual(extracted, destination / "release")
            self.assertEqual((extracted / "README.txt").read_text(encoding="utf-8"), "ok")

    def test_updater_rejects_unsigned_manifest_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = UpdateManager("1.0", Path(directory))
            version = VersionInfo({
                "version": "2.0",
                "download_url": "https://example.invalid/release.zip",
                "checksum": "",
            })
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                manager.download_update(version)

    def test_backup_collection_prunes_excluded_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "app" / "main.py").write_text("ok", encoding="utf-8")
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "secret").write_text("no", encoding="utf-8")
            (root / "spool").mkdir()
            (root / "spool" / "receipt.png").write_bytes(b"no")

            files = UpdateManager("1.0", root)._collect_source_files()
            self.assertEqual(files, [(root / "app" / "main.py").resolve()])


class LocalAddressWatchdogTests(unittest.TestCase):
    """A DHCP address change must be republished, not silently kept.

    ``IOT_IP`` is resolved once at import, so before the watchdog existed the
    box kept telling Odoo (and every POS client) the address it happened to
    start with until somebody restarted it.
    """

    def test_address_change_is_republished_to_odoo_and_the_certificate(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as directory:
            # Keep the import from touching the runtime's real configuration.
            with patch.dict(
                os.environ,
                {"IOT_CONFIG_PATH": str(Path(directory) / "runtime_config.json")},
            ):
                import app.main as main

                async def scenario():
                    main.IOT_IP = "192.168.10.1:8398"
                    main.cloud_bridge.iot_ip = "192.168.10.1:8398"
                    main.certificate_manager.iot_ip = "192.168.10.1:8398"
                    main.config_store.update_local_config(
                        local_url="https://192.168.10.1:8398"
                    )
                    with patch.object(
                        main, "_sync_registered_iot_box", new=AsyncMock()
                    ) as synced, patch.object(
                        main.cloud_bridge, "request_reconnect", new=AsyncMock()
                    ) as reconnected, patch.object(
                        main.certificate_manager, "ensure"
                    ) as ensure:
                        await main._republish_local_address(
                            "192.168.10.1:8398", "192.168.10.9:8398"
                        )
                    return synced, reconnected, ensure

                synced, reconnected, ensure = asyncio.run(scenario())
                published_url = main.config_store.get_local_config().get("local_url")

        self.assertEqual(main.IOT_IP, "192.168.10.9:8398")
        self.assertEqual(main.cloud_bridge.iot_ip, "192.168.10.9:8398")
        self.assertEqual(main.certificate_manager.iot_ip, "192.168.10.9:8398")
        # The advertised address must follow too, keeping the runtime's scheme.
        self.assertEqual(published_url, "https://192.168.10.9:8398")
        synced.assert_awaited_once()
        reconnected.assert_awaited_once()
        ensure.assert_called_once()

    def test_loopback_is_never_adopted_as_the_published_address(self):
        """The interface being down must not publish an unreachable address."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"IOT_CONFIG_PATH": str(Path(directory) / "runtime_config.json")},
            ):
                import app.main as main

                async def scenario():
                    main.IOT_IP = "192.168.10.1:8398"
                    with patch.object(
                        main, "_detect_local_ip", return_value="127.0.0.1:8398"
                    ), patch.object(
                        main, "_republish_local_address", new=AsyncMock()
                    ) as republished, patch.object(
                        main.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
                    ):
                        try:
                            await main._local_ip_watchdog()
                        except asyncio.CancelledError:
                            pass
                    return republished, main.IOT_IP

                republished, current = asyncio.run(scenario())

        republished.assert_not_awaited()
        self.assertEqual(current, "192.168.10.1:8398")


if __name__ == "__main__":
    unittest.main()
