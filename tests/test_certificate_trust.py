"""A client that does not trust the box's CA cannot print, and cannot say why.

Windows gets the CA installed into the user's Root store at startup.  macOS had
no equivalent, so its browsers kept a per-certificate exception instead -- and
the box re-issues its server certificate whenever its address changes, which
invalidates that exception.  The POS then reports a printer error while the box
never sees a single request, so nothing in the box log explains the failure.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.certificate_manager import CertificateManager, current_trust_platform


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _trusted_dump(fingerprint: str) -> str:
    """``security dump-trust-settings`` as it reads for a trusted root."""
    return (
        "Number of trusted certs = 1\n"
        "Cert 0: Custom IoT Box CA\n"
        "   Number of trust settings : 1\n"
        "      Trust Setting 0:\n"
        "         Policy OID : 2.5.29.32.0\n"
        "         Result Type : kSecTrustSettingsResultTrustRoot\n"
        f"         SHA-1 hash: {fingerprint}\n"
    )


_REAL_SUBPROCESS_RUN = subprocess.run


class _MacOsSecurity:
    """Stands in for the ``security`` CLI so no real keychain is touched.

    Everything else -- the ``openssl`` calls that generate the certificates --
    runs for real, so the tests exercise the genuine CA and leaf.
    """

    def __init__(
        self,
        *,
        add_returncode: int = 0,
        trusted_dump: str | None = None,
        store_without_setting: bool = False,
    ):
        self.add_returncode = add_returncode
        self.trusted_dump = trusted_dump
        # macOS can store the certificate yet keep no trust setting.
        self.store_without_setting = store_without_setting
        self.trusted_cert: Path | None = None
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        if Path(str(command[0])).name != "security":
            return _REAL_SUBPROCESS_RUN(command, **kwargs)
        self.commands.append([str(part) for part in command])
        if command[1] == "default-keychain":
            return _completed(stdout='"/Users/someone/Library/Keychains/login.keychain-db"\n')
        if command[1] == "add-trusted-cert":
            if self.add_returncode == 0 and not self.store_without_setting:
                self.trusted_cert = Path(str(command[-1]))
            return _completed(returncode=self.add_returncode, stderr="" if self.add_returncode == 0 else "denied")
        if command[1] == "dump-trust-settings":
            if self.trusted_dump is not None:
                return _completed(stdout=self.trusted_dump)
            if self.trusted_cert is not None:
                return _completed(stdout=_trusted_dump(CertificateManager._sha1_fingerprint(self.trusted_cert)))
            return _completed(stdout="No Trust Settings were found.\n")
        raise AssertionError(f"unexpected command: {command}")

    def add_calls(self) -> list[list[str]]:
        return [command for command in self.commands if command[1] == "add-trusted-cert"]


class MacOsTrustTests(unittest.TestCase):
    def _manager(self, directory: str) -> CertificateManager:
        return CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")

    def test_the_trusted_entry_is_the_ca_not_the_server_certificate(self):
        """The server certificate is re-issued on every address change; the CA is not."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            security = _MacOsSecurity()
            with patch("app.certificate_manager.subprocess.run", security):
                self.assertTrue(manager.install_for_current_macos_user())

            add = security.add_calls()
            self.assertEqual(len(add), 1)
            self.assertEqual(add[0][-1], str(manager.ca_crt_path))
            self.assertIn("trustRoot", add[0])
            self.assertTrue(manager.macos_trust_marker_path.exists())

    def test_a_second_start_does_not_prompt_again(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            security = _MacOsSecurity()
            with patch("app.certificate_manager.subprocess.run", security):
                manager.install_for_current_macos_user()
                manager.install_for_current_macos_user()

            self.assertEqual(len(security.add_calls()), 1)

    def test_a_new_ca_is_trusted_again(self):
        """A re-paired box gets a new CA, so the marker must not hide it."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            security = _MacOsSecurity()
            with patch("app.certificate_manager.subprocess.run", security):
                manager.install_for_current_macos_user()
                manager.ca_crt_path.unlink()
                manager.install_for_current_macos_user()

            self.assertEqual(len(security.add_calls()), 2)

    def test_a_refused_prompt_is_reported_and_retried_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            security = _MacOsSecurity(add_returncode=1)
            with patch("app.certificate_manager.subprocess.run", security):
                with self.assertRaises(RuntimeError) as raised:
                    manager.install_for_current_macos_user()

            self.assertIn("denied", str(raised.exception))
            self.assertFalse(
                manager.macos_trust_marker_path.exists(),
                "an unanswered prompt must not look like a trusted CA",
            )

    def test_an_unanswered_dialog_does_not_hold_up_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)

            def timeout(command, **kwargs):
                if Path(str(command[0])).name != "security":
                    return _REAL_SUBPROCESS_RUN(command, **kwargs)
                raise subprocess.TimeoutExpired(cmd="security", timeout=120)

            with patch("app.certificate_manager.subprocess.run", timeout):
                with self.assertRaises(RuntimeError) as raised:
                    manager.install_for_current_macos_user()

            self.assertIn("approve the macOS prompt", str(raised.exception))

    def test_the_trust_probe_reads_the_platform_store(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager.ensure()
            fingerprint = manager._sha1_fingerprint(manager.ca_crt_path)
            self.assertTrue(fingerprint)

            trusted = _MacOsSecurity(trusted_dump=_trusted_dump(fingerprint))
            with patch("app.certificate_manager.subprocess.run", trusted):
                self.assertTrue(manager.is_trusted_for_current_user())

            untrusted = _MacOsSecurity(trusted_dump="No Trust Settings were found.\n")
            with patch("app.certificate_manager.subprocess.run", untrusted):
                self.assertFalse(manager.is_trusted_for_current_user())

    def test_a_certificate_stored_without_a_trust_setting_is_not_trusted(self):
        """This is the state macOS left behind here: installed, trusting nothing.

        ``security add-trusted-cert`` exits 0 even when it stored no setting, so
        the box would otherwise report a trusted CA that every client rejects.
        """
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager.ensure()
            stored_only = _MacOsSecurity(
                store_without_setting=True,
                trusted_dump="Number of trusted certs = 1\n"
                "Cert 0: Custom IoT Box CA\n"
                "   Number of trust settings : 0\n",
            )

            with patch("app.certificate_manager.subprocess.run", stored_only):
                self.assertFalse(manager.is_trusted_for_current_user())
                # The install must not claim success either.
                with self.assertRaises(RuntimeError) as raised:
                    manager.install_for_current_macos_user()

            self.assertIn("kept no trust setting", str(raised.exception))
            self.assertFalse(manager.macos_trust_marker_path.exists())

    def test_status_reports_whether_this_machine_trusts_the_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            manager.ensure()
            fingerprint = manager._sha1_fingerprint(manager.ca_crt_path)
            security = _MacOsSecurity(trusted_dump=_trusted_dump(fingerprint))
            with patch("app.certificate_manager.subprocess.run", security):
                status = manager.status()

            self.assertIs(status["ca_trusted"], True)


class TrustPlatformTests(unittest.TestCase):
    def test_each_platform_installs_into_its_own_store(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")

            with patch("app.certificate_manager.current_trust_platform", return_value="macos"), \
                    patch.object(manager, "install_for_current_macos_user", return_value=True) as macos:
                self.assertTrue(manager.install_for_current_user())
            macos.assert_called_once()

            with patch("app.certificate_manager.current_trust_platform", return_value="windows"), \
                    patch.object(manager, "install_for_current_windows_user", return_value=True) as windows:
                self.assertTrue(manager.install_for_current_user())
            windows.assert_called_once()

    def test_a_platform_without_a_trust_store_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")
            with patch("app.certificate_manager.current_trust_platform", return_value=""):
                self.assertFalse(manager.install_for_current_user())
                self.assertIsNone(manager.is_trusted_for_current_user())

    def test_the_running_platform_is_detected(self):
        self.assertIn(current_trust_platform(), {"windows", "macos", ""})


class ReissueReasonTests(unittest.TestCase):
    """The reason a certificate was re-issued has to be in the log.

    "The POS stopped printing after a restart" cost an afternoon of tracing
    because nothing recorded that the address had changed.
    """

    def test_a_moved_box_is_named_as_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.42:8398")
            manager.ensure()
            self.assertIsNone(manager._bundle_problem(), "an unchanged address must not re-issue")

            manager.iot_ip = "192.168.1.39:8398"
            reason = manager._bundle_problem()

            self.assertIn("192.168.1.39", reason)
            self.assertIn("192.168.1.42", reason, "the reason should name what the certificate covers")

    def test_a_missing_bundle_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")
            manager.ensure()
            manager.p12_path.unlink()

            self.assertIn("iotbox.p12", manager._bundle_problem())

    def test_a_re_issue_is_logged_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            CertificateManager(Path(directory), iot_ip="192.168.1.42:8398").ensure()
            moved = CertificateManager(Path(directory), iot_ip="192.168.1.39:8398")

            with self.assertLogs("app.certificate_manager", level="INFO") as captured:
                moved.ensure()

            self.assertTrue(
                any("Re-issued the IoT Box HTTPS server certificate" in line for line in captured.output),
                captured.output,
            )


if __name__ == "__main__":
    unittest.main()
