import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingPathTests(unittest.TestCase):
    def test_runtime_sources_compile(self):
        for name in ("run_https.py",):
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)

    def test_https_uses_persistent_paths_and_unique_p12_password(self):
        source = (ROOT / "run_https.py").read_text(encoding="utf-8")
        self.assertIn('IOT_CONFIG_PATH', source)
        self.assertIn('IOT_CERTS_DIR', source)
        self.assertIn('p12_password=os.getenv("IOT_P12_PASSWORD", "")', source)

    def test_https_publishes_lan_url_instead_of_loopback(self):
        source = (ROOT / "run_https.py").read_text(encoding="utf-8")
        self.assertIn("def _advertised_https_url", source)
        self.assertIn("local_url=advertised_url", source)


if __name__ == "__main__":
    unittest.main()
