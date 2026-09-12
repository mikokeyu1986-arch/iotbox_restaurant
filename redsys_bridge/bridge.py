from __future__ import annotations

import base64
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from pathlib import Path


def _load_request() -> dict[str, str]:
    encoded = ""
    if len(sys.argv) >= 2:
        encoded = sys.argv[-1]
    if not encoded:
        raise SystemExit("Missing base64 payload.")
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


class _BridgeConfig:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    def get(self, key: str, default: object = "") -> object:
        if key not in self._values:
            return default
        return self._wrap(self._values[key])

    def __getitem__(self, key: str) -> object:
        return self._wrap(self._values[key])

    def __getattr__(self, name: str) -> object:
        if name in self._values:
            return self._wrap(self._values[name])
        raise AttributeError(name)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def items(self):
        for key, value in self._values.items():
            yield key, self._wrap(value)

    @staticmethod
    def _wrap(value: object) -> object:
        if isinstance(value, dict):
            return _BridgeConfig(value)
        return value


def _build_runtime_config(request: dict[str, str]) -> _BridgeConfig:
    return _BridgeConfig(
        {
            "connect": {
                "comercio": str(request.get("merchant_code", "")),
                "terminal": str(request.get("terminal", "")),
                "clave_firma": str(request.get("signature_key", "")),
                "puerto": str(request.get("port_config", "")).split(":", 1)[0],
                "version": str(request.get("tpv_version", "")),
            }
        }
    )


def _load_util_module(runtime_dir: Path, request: dict[str, str]):
    os.chdir(runtime_dir)
    lib_dir = runtime_dir / "lib"
    os.add_dll_directory(str(lib_dir))
    cfg = _build_runtime_config(request)

    app_mod = types.ModuleType("app")
    app_mod.CustomerExpection = Exception
    app_mod.dllTpvpcLatente = ctypes.cdll.LoadLibrary(str(lib_dir / "dllTpvpcLatente.dll"))

    base_mod = types.ModuleType("base")

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    base_mod.logger = _Logger()

    config_mod = types.ModuleType("config")
    config_mod.Config = _BridgeConfig
    config_mod.config = cfg

    sys.modules["app"] = app_mod
    sys.modules["base"] = base_mod
    sys.modules["config"] = config_mod

    util_loader = importlib.machinery.SourcelessFileLoader(
        "redsys_runtime_util", str(lib_dir / "service" / "util.pyc")
    )
    util_spec = importlib.util.spec_from_loader("redsys_runtime_util", util_loader)
    utilmod = importlib.util.module_from_spec(util_spec)
    util_loader.exec_module(utilmod)
    return utilmod


def _normalize_result(result: dict[str, object]) -> dict[str, object]:
    encode_data = result.get("encode_data", b"")
    decode_data = result.get("decode_data", "")
    if isinstance(encode_data, (bytes, bytearray)):
        encode_data = encode_data.decode("ISO-8859-1", errors="ignore")
    return {
        "return_code": int(result.get("success", -1)),
        "init_code": int(result.get("success", -1)),
        "stop_code": 0,
        "xml": str(decode_data or encode_data or ""),
        "error": str(result.get("msg", "") or ""),
    }


def main() -> int:
    request = _load_request()
    runtime_dir = Path(os.environ["PY310_RUNTIME_DIR"]).resolve()
    utilmod = _load_util_module(runtime_dir, request)
    action = str(request.get("action", "")).lower()

    if action == "connect":
        result = utilmod.connect()
    elif action == "pay":
        result = utilmod._OperPinPad(
            str(request.get("amount", "")),
            str(request.get("invoice", "")),
            str(request.get("operation_type", "")),
        )
    elif action == "refund":
        result = utilmod._ComContableTrj(
            str(request.get("amount", "")),
            str(request.get("invoice", "")),
            str(request.get("order", "")),
        )
    elif action == "query":
        result = utilmod._OperConsulta(
            str(request.get("invoice", "")),
            str(request.get("page", "0")),
        )
    else:
        result = {
            "success": -1,
            "encode_data": "",
            "decode_data": "",
            "msg": f"Unsupported bridge action: {request.get('action', '')}",
        }

    print(json.dumps(_normalize_result(result), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
