from __future__ import annotations

import argparse
import sys
from pathlib import Path

from redsys_server import create_server
from redsys_server.config import load_config


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Redsys dataphone server")
    parser.add_argument(
        "--config",
        default=str((_runtime_base_dir() / "config.yaml").resolve()),
        help="Path to the YAML config file.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    server = create_server(config, _runtime_base_dir() / "server")
    print(f"Serving Redsys local server at http://{config.host}:{config.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
