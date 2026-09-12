from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RedsysServerConfig:
    host: str = "127.0.0.1"
    port: int = 6969
    merchant_code: str = ""
    merchant_terminal: str = "1"
    merchant_signature_key: str = ""
    serial_port: str = ""
    serial_port_config: str = ""
    tpv_version: str = ""
    odoo_create_url: str = "http://127.0.0.1:8069/pos_dataphone_operation/create"
    storage_path: str = ""
    allow_origin: str = "*"
    simulate: bool = True
    success_code: str = "0000"
    cancel_code: str = "9915"
    error_code: str = "9999"
    bridge_powershell_path: str = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    bridge_script_path: str = ""
    bridge_dll_path: str = ""
    bridge_runtime_dir: str = ""
    bridge_dependency_dirs: tuple[str, ...] = ()
    bridge_timeout_seconds: int = 120
    bridge_log_path: str = ""
    legacy_discovery_log_path: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], base_dir: Path) -> "RedsysServerConfig":
        app_section = _as_mapping(raw.get("app"))
        http_section = _as_mapping(app_section.get("http"))
        connect_section = _as_mapping(raw.get("connect"))
        odoo_section = _as_mapping(raw.get("odoo"))
        storage_section = _as_mapping(raw.get("storage"))
        bridge_section = _as_mapping(raw.get("bridge"))

        storage_path = str(
            (base_dir / storage_section.get("path", "data/operations.json")).resolve()
        )
        serial_port = str(connect_section.get("puerto", "")).strip()
        serial_port_config = _normalize_serial_port_config(serial_port)
        bridge_script_path = _resolve_bridge_path(
            base_dir,
            bridge_section.get("script_path", "server/redsys_server/bridge/redsys_x86_bridge.ps1"),
        )
        bridge_dll_path = _resolve_existing_file_path(
            base_dir,
            bridge_section.get("dll_path", "lib/dllTpvpcLatente.dll"),
            _default_bridge_dll_candidates(base_dir),
        )
        bridge_runtime_dir = _resolve_runtime_dir(
            base_dir,
            bridge_section.get("runtime_dir", ""),
            bridge_dll_path,
        )
        bridge_log_path = _resolve_bridge_path(base_dir, bridge_section.get("log_path", "data/bridge.log"))
        bridge_dependency_dirs = _resolve_dependency_dirs(
            base_dir,
            str(bridge_section.get("dependency_dirs", "")),
        )
        legacy_discovery_log_path = _resolve_bridge_path(
            base_dir,
            bridge_section.get("legacy_discovery_log_path", "../redsys/logDllImplantado.txt"),
        )
        bridge_powershell_path = _resolve_bridge_launcher_path(
            base_dir,
            str(
                bridge_section.get(
                    "powershell_path",
                    r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
                )
            ).strip(),
            bridge_script_path,
        )
        return cls(
            host=str(http_section.get("host", "127.0.0.1")).strip() or "127.0.0.1",
            port=_as_int(http_section.get("port"), 6969),
            merchant_code=str(connect_section.get("comercio", "")).strip(),
            merchant_terminal=str(connect_section.get("terminal", "1")).strip() or "1",
            merchant_signature_key=str(connect_section.get("clave_firma", "")).strip(),
            serial_port=serial_port,
            serial_port_config=serial_port_config,
            tpv_version=str(connect_section.get("version", "")).strip(),
            odoo_create_url=str(
                odoo_section.get(
                    "create_url", "http://127.0.0.1:8069/pos_dataphone_operation/create"
                )
            ).strip(),
            storage_path=storage_path,
            allow_origin=str(http_section.get("allow_origin", "*")).strip() or "*",
            simulate=_as_bool(app_section.get("simulate"), True),
            success_code=str(app_section.get("success_code", "0000")).strip() or "0000",
            cancel_code=str(app_section.get("cancel_code", "9915")).strip() or "9915",
            error_code=str(app_section.get("error_code", "9999")).strip() or "9999",
            bridge_powershell_path=bridge_powershell_path,
            bridge_script_path=bridge_script_path,
            bridge_dll_path=bridge_dll_path,
            bridge_runtime_dir=bridge_runtime_dir,
            bridge_dependency_dirs=bridge_dependency_dirs,
            bridge_timeout_seconds=_as_int(bridge_section.get("timeout_seconds"), 120),
            bridge_log_path=bridge_log_path,
            legacy_discovery_log_path=legacy_discovery_log_path,
        )


def load_config(config_path: Path) -> RedsysServerConfig:
    raw_text = config_path.read_text(encoding="utf-8")
    raw_mapping = _parse_simple_yaml(raw_text)
    return RedsysServerConfig.from_mapping(raw_mapping, config_path.parent)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]

        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if not value:
            child: dict[str, Any] = {}
            current[key] = child
            stack.append((indent, child))
            continue

        current[key] = _coerce_scalar(value)

    return root


def _coerce_scalar(value: str) -> Any:
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1]

    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if lower in {"null", "none", "~"}:
        return ""
    if value.isdigit():
        return int(value)
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    return default


def _normalize_serial_port_config(value: str) -> str:
    port = str(value or "").strip()
    if not port:
        return ""
    upper = port.upper()
    if upper.startswith("COM") and "," not in upper:
        return f"{upper}:,19200,N,8,1"
    return port


def _resolve_bridge_path(base_dir: Path, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


def _resolve_existing_file_path(base_dir: Path, value: str, fallback_candidates: list[Path]) -> str:
    candidates = _path_candidates(base_dir, value)
    candidates.extend(fallback_candidates)
    return _first_existing_path(candidates, fallback=str(_path_candidates(base_dir, value)[0]) if value else "")


def _resolve_existing_dir_path(base_dir: Path, value: str, fallback_candidates: list[Path]) -> str:
    candidates = _path_candidates(base_dir, value)
    candidates.extend(fallback_candidates)
    return _first_existing_path(candidates, fallback=str(_path_candidates(base_dir, value)[0]) if value else "")


def _resolve_runtime_dir(base_dir: Path, value: str, bridge_dll_path: str) -> str:
    configured_candidates = _path_candidates(base_dir, value)
    runtime_candidates = [
        *configured_candidates,
        *([Path(bridge_dll_path).resolve().parent.parent] if bridge_dll_path else []),
        *_default_runtime_candidates(base_dir),
    ]
    return _first_existing_path(
        runtime_candidates,
        validator=_is_usable_runtime_dir,
        fallback=str(configured_candidates[0]) if configured_candidates else "",
    )


def _resolve_dependency_dirs(base_dir: Path, configured: str) -> tuple[str, ...]:
    candidates: list[Path] = []
    for item in str(configured or "").split(";"):
        candidates.extend(_path_candidates(base_dir, item))
    candidates.extend(_default_dependency_dir_candidates(base_dir))

    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).lower()
        if normalized in seen or not candidate.exists() or not candidate.is_dir():
            continue
        seen.add(normalized)
        resolved.append(str(candidate))
    return tuple(resolved)


def _default_bridge_dll_candidates(base_dir: Path) -> list[Path]:
    local_appdata = Path(os.getenv("LOCALAPPDATA", "")).resolve() if os.getenv("LOCALAPPDATA") else None
    candidates = [
        (base_dir / "lib" / "dllTpvpcLatente.dll").resolve(),
        (base_dir / "_internal" / "lib" / "dllTpvpcLatente.dll").resolve(),
    ]
    if local_appdata:
        candidates.append((local_appdata / "Programs" / "Redsys" / "lib" / "dllTpvpcLatente.dll").resolve())
    return candidates


def _default_runtime_candidates(base_dir: Path) -> list[Path]:
    local_appdata = Path(os.getenv("LOCALAPPDATA", "")).resolve() if os.getenv("LOCALAPPDATA") else None
    candidates = [
        (base_dir / "runtime").resolve(),
        (base_dir / "_internal" / "runtime").resolve(),
    ]
    if local_appdata:
        candidates.append((local_appdata / "Programs" / "Redsys").resolve())
    return candidates


def _default_dependency_dir_candidates(base_dir: Path) -> list[Path]:
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    return [
        (base_dir / "vendor" / "TpvpcWinService").resolve(),
        (base_dir / "_internal" / "vendor" / "TpvpcWinService").resolve(),
        (base_dir / "vendor" / "TpvpcImplantado").resolve(),
        (base_dir / "_internal" / "vendor" / "TpvpcImplantado").resolve(),
        (program_files_x86 / "REDSYS" / "TpvpcWinService").resolve(),
        (program_files_x86 / "TpvpcImplantado").resolve(),
    ]


def _path_candidates(base_dir: Path, value: Any) -> list[Path]:
    text = str(value or "").strip()
    if not text:
        return []
    path = Path(text)
    if path.is_absolute():
        return [path]
    return [
        (base_dir / path).resolve(),
        (base_dir / path.name).resolve(),
    ]


def _first_existing_path(
    candidates: list[Path],
    *,
    validator: Any = None,
    fallback: str = "",
) -> str:
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        if not candidate.exists():
            continue
        if validator and not validator(candidate):
            continue
        return str(candidate)
    return fallback


def _is_usable_runtime_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    required = [
        path / "python310.dll",
        path / "Redsys.exe",
        path / "lib" / "library.zip",
    ]
    return all(item.exists() for item in required)


def _resolve_bridge_launcher_path(base_dir: Path, configured: str, script_path: str) -> str:
    script_suffix = Path(script_path).suffix.lower()
    candidates: list[Path] = []

    configured = str(configured or "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.is_absolute():
            candidates.append(configured_path)
        else:
            candidates.append((base_dir / configured_path).resolve())
        candidates.append((base_dir / configured_path.name).resolve())

    if script_suffix == ".py":
        candidates.extend(
            [
                (base_dir / "Py310Host.exe").resolve(),
                (base_dir / "_internal" / "Py310Host.exe").resolve(),
            ]
        )
    else:
        candidates.extend(
            [
                Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
                Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
            ]
        )

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.exists():
            return str(candidate)

    if configured:
        return configured
    if script_suffix == ".py":
        return str((base_dir / "Py310Host.exe").resolve())
    return r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
