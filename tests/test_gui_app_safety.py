import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GuiAppSafetyTests(unittest.TestCase):
    def test_gui_sources_compile(self):
        for source_path in (ROOT / "gui_app.py", ROOT / "customer_display_app.py"):
            ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    def test_windows_gui_dependencies_are_declared(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"pystray>=', project)
        self.assertIn('"pywebview>=', project)

    def test_launcher_has_no_unreachable_legacy_body(self):
        launcher_lines = [
            line.strip()
            for line in (ROOT / "start_gui.bat").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().upper().startswith("REM ")
        ]
        self.assertEqual(
            launcher_lines,
            ["@echo off", 'wscript.exe "%~dp0start_gui_hidden.vbs"', "exit /b 0"],
        )

    def test_removed_gui_symbols_do_not_return(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_ODOO_URL", source)
        self.assertNotIn("DEFAULT_TOKEN_URL", source)
        self.assertNotIn("_run_customer_display_webview", source)

    def test_gui_can_recover_when_the_system_tray_is_unavailable(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn("def _recover_from_tray_failure", source)
        self.assertIn("Always display it, even when Windows launched the app at login.", source)
        self.assertIn('"Local\\\\IOTBOX_GUI_SINGLE_INSTANCE"', source)
        self.assertIn('"Local\\\\IOTBOX_GUI_SHOW_SETTINGS"', source)

    def test_packaged_customer_display_uses_a_dedicated_executable(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        build_script = (ROOT / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('customer_display_app.exe', source)
        self.assertIn('--onefile --windowed --name customer_display_app', build_script)

    def test_gui_exposes_the_local_redsys_card_terminal(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('REDSYS_PORT = 6971', source)
        self.assertIn('💳 REDSYS 刷卡', source)
        self.assertIn('web" / "nfc_override.png', source)
        for method in (
            '_build_redsys_tab', '_start_redsys_automatically',
            '_test_redsys_reader', '_test_redsys_payment',
            '_ensure_redsys_service',
        ):
            self.assertIn(f'def {method}', source)



if __name__ == "__main__":
    unittest.main()
