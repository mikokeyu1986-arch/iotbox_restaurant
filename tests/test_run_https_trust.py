import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch


_SPEC = importlib.util.spec_from_file_location(
    "iotbox_run_https_test_target",
    Path(__file__).resolve().parents[1] / "run_https.py",
)
assert _SPEC is not None and _SPEC.loader is not None
run_https = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_https)


def test_macos_runtime_never_installs_certificate_trust_automatically():
    certificates = Mock()

    with patch.object(run_https.sys, "platform", "darwin"):
        run_https._trust_the_certificate_in_the_background(certificates)

    certificates.install_for_current_user.assert_not_called()


def test_non_macos_runtime_keeps_existing_automatic_trust_behavior():
    certificates = Mock()

    with patch.object(run_https.sys, "platform", "win32"):
        run_https._trust_the_certificate_in_the_background(certificates)

    certificates.install_for_current_user.assert_called_once_with()
