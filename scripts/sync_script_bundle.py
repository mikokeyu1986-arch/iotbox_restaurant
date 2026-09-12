"""Refresh the Windows deployment overlay from canonical project files."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "script_bundle"


def main() -> None:
    shutil.copytree(ROOT / "web", BUNDLE / "web", dirs_exist_ok=True)
    shutil.copy2(ROOT / "run_https.py", BUNDLE / "run_https.py")
    shutil.copy2(ROOT / "pyproject.toml", BUNDLE / "pyproject.toml")


if __name__ == "__main__":
    main()
