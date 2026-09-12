"""Printer command-language detection (ESC/POS vs ZPL) and ZPL extraction.

Odoo never tells this IoT Box which command language the attached printer
understands.  Zebra-compatible label printers only accept ZPL: an ESC/POS byte
stream sent to them is silently discarded, so the receipt never appears.

The helpers in this module

* probe a raw TCP printer (port 9100) to find out whether it speaks ZPL,
* detect ZPL printers from their Windows driver / queue names,
* extract ready-to-send ZPL instructions from an Odoo print payload so the
  payload can be forwarded byte for byte.

Nothing here generates label content: the IoT Box only identifies the printer
language and passes Odoo's own data through.
"""

from __future__ import annotations

import json
import logging
import re
import socket
from time import time
from typing import Any

try:  # pragma: no cover - only importable on Windows with pywin32 installed
    import win32print  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    win32print = None

_logger = logging.getLogger(__name__)

PRINTER_LANGUAGE_ESCPOS = "escpos"
PRINTER_LANGUAGE_ZPL = "zpl"
PRINTER_LANGUAGES = (PRINTER_LANGUAGE_ESCPOS, PRINTER_LANGUAGE_ZPL)

# ``iot.device.subtype`` values Odoo accepts.  Odoo's own IoT printer driver
# reports a ZPL / ESC-Label printer as "label_printer"
# (addons/iot_drivers/iot_handlers/drivers/printer_driver_L.py), so a detected
# ZPL device is published with the same vocabulary: "zpl" is a command
# language, not a device subtype, and Odoo would reject it.
PRINTER_SUBTYPE_RECEIPT = "receipt_printer"
PRINTER_SUBTYPE_LABEL = "label_printer"

# Zebra ships its Windows drivers as "ZDesigner <model>" and names its label
# printers after model families (ZD/ZT/ZQ/GK/GX/...).  The families are matched
# with a regex so a bare two letter prefix cannot be mistaken for a Zebra model.
ZEBRA_BRAND_MARKERS = ("zebra", "zdesigner", "link-os", "link os")
_ZEBRA_MODEL_RE = re.compile(
    r"\b(?:zd|zt|zq|zr|zx|gk|gx|gz)\d{2,4}[a-z]?\b"
    r"|\bqln?\d{2,4}[a-z]?\b"
    r"|\b(?:110xi|140xi|170xi|220xi|105sl|xi3|xi4|r110xi|r170xi|r2844|t402|s4m|s6m|z4m|z6m)\b"
)

# Other ZPL label-printer vendors.  They are recognised so that a detected
# device can be reported with its brand as well.
_ZPL_VENDOR_MARKERS = (
    ("GODEX", ("godex",)),
    ("TSC", ("tsc",)),
    ("SATO", ("sato",)),
    ("ARGOX", ("argox",)),
    ("BIXOLON", ("bixolon",)),
    ("POSTEK", ("postek",)),
    ("CITIZEN", ("citizen cl", "citizen ct")),
)

# Driver / model / queue names that identify a ZPL label printer.  Windows
# queues cannot be probed reliably, so the driver name is the best signal.
ZPL_NAME_MARKERS = (
    "zpl",
    "label",
    "etiqueta",
    "etiketa",
    "标签",
    *ZEBRA_BRAND_MARKERS,
    *(marker for _, markers in _ZPL_VENDOR_MARKERS for marker in markers),
)

# ZPL label delimiters.
ZPL_LABEL_START = "^XA"
ZPL_LABEL_END = "^XZ"
# ``~HI`` (host identification) answers with "<model>,<version>,<link>,<dpi>".
ZPL_PROBE_COMMAND = b"~HI"
# ESC/POS real-time status request (DLE EOT 1).  A receipt printer answers with
# a single status byte and - unlike a plain ASCII probe - prints nothing, so
# probing an ESC/POS printer has no visible side effect.
ESCPOS_STATUS_QUERY = b"\x10\x04\x01"

_PROBE_RESPONSE_MAX_BYTES = 256
_ZPL_QUERY_MARKERS = ("~HI", "~HS", "~HM")
_IMAGE_SOURCE_PREFIXES = ("data:image/", "http://", "https://")

# Payload keys that explicitly carry ZPL instructions.
_ZPL_TEXT_KEYS = (
    "zpl",
    "zpl_data",
    "zpl_payload",
    "zpl_text",
    "zpl_label",
    "label_zpl",
    "raw_zpl",
)
# Payload keys that may carry an already-rendered byte stream.
_RAW_PAYLOAD_KEYS = ("receipt", "raw", "raw_data", "raw_receipt")


def normalize_language(value: Any) -> str:
    """Return a canonical printer language for *value*, or "" when unknown."""
    text = str(value or "").strip().lower()
    if text in {"zpl", "zpl2", "zplii", "zpl-ii", "zebra"}:
        return PRINTER_LANGUAGE_ZPL
    if text in {"escpos", "esc/pos", "esc_pos", "pos"}:
        return PRINTER_LANGUAGE_ESCPOS
    return ""


def printer_subtype(language: Any) -> str:
    """Return the Odoo ``iot.device.subtype`` matching *language*.

    A ZPL label printer is reported as "label_printer" so Odoo stops treating
    it as a receipt printer; every other printer keeps "receipt_printer".
    """
    if normalize_language(language) == PRINTER_LANGUAGE_ZPL:
        return PRINTER_SUBTYPE_LABEL
    return PRINTER_SUBTYPE_RECEIPT


def looks_like_zpl(value: Any) -> bool:
    """Return whether *value* carries ZPL label instructions."""
    if value is None:
        return False
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="ignore")
    elif isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        return any(looks_like_zpl(item) for item in value.values())
    elif isinstance(value, (list, tuple)):
        return any(looks_like_zpl(item) for item in value)
    else:
        text = str(value)
    upper = text[:8192].upper()
    if ZPL_LABEL_START in upper or ZPL_LABEL_END in upper:
        return True
    return any(marker in upper for marker in _ZPL_QUERY_MARKERS)


def zebra_matches(*names: Any) -> bool:
    """Return whether any of *names* identifies a Zebra label printer."""
    haystack = " ".join(str(name or "") for name in names).strip().lower()
    if not haystack:
        return False
    if any(marker in haystack for marker in ZEBRA_BRAND_MARKERS):
        return True
    return bool(_ZEBRA_MODEL_RE.search(haystack))


def zebra_model_name(*names: Any) -> str:
    """Return the Zebra model found in *names* (e.g. "ZD220"), or ""."""
    match = _ZEBRA_MODEL_RE.search(" ".join(str(name or "") for name in names).lower())
    return match.group(0).upper() if match else ""


def printer_brand(*names: Any) -> str:
    """Return the label-printer brand implied by *names* ("" when unknown)."""
    if zebra_matches(*names):
        return "ZEBRA"
    haystack = " ".join(str(name or "") for name in names).strip().lower()
    if not haystack:
        return ""
    for brand, markers in _ZPL_VENDOR_MARKERS:
        if any(marker in haystack for marker in markers):
            return brand
    return ""


def marker_matches_language(*names: Any) -> str:
    """Return "zpl" when any of *names* looks like a ZPL printer name."""
    haystack = " ".join(str(name or "") for name in names).strip().lower()
    if not haystack:
        return ""
    if any(marker in haystack for marker in ZPL_NAME_MARKERS):
        return PRINTER_LANGUAGE_ZPL
    return PRINTER_LANGUAGE_ZPL if zebra_matches(haystack) else ""


def window_driver_language(queue_name: str, driver_name: str = "", port_name: str = "") -> str:
    """Detect the language of a Windows print queue from its names."""
    return marker_matches_language(driver_name, queue_name, port_name)


def windows_queue_identity(queue_name: str) -> dict[str, str]:
    """Read a Windows print queue and identify the attached printer.

    Keys: ``language``, ``brand``, ``model`` and ``driver``.  An empty dict is
    returned when nothing conclusive could be read, so a regular ESC/POS queue
    is never reported as a positive ZPL detection.
    """
    name = str(queue_name or "").strip()
    if not name:
        return {}
    driver_name = ""
    port_name = ""
    if win32print is not None:
        info = _windows_queue_info(name)
        driver_name = str(info.get("pDriverName") or "")
        port_name = str(info.get("pPortName") or "")
    language = window_driver_language(name, driver_name, port_name)
    if not language:
        return {}
    return {
        "language": language,
        "brand": printer_brand(driver_name, name, port_name),
        "model": zebra_model_name(driver_name, name) or driver_name or name,
        "driver": driver_name,
    }


def windows_queue_language(queue_name: str) -> str:
    """Return the command language of a Windows print queue ("zpl" or "")."""
    return str(windows_queue_identity(queue_name).get("language") or "")


def _windows_queue_info(queue_name: str) -> dict[str, Any]:
    try:
        handle = win32print.OpenPrinter(queue_name)
    except Exception:
        _logger.debug("Unable to open Windows printer queue=%s", queue_name, exc_info=True)
        return {}
    try:
        info = win32print.GetPrinter(handle, 2)
    except Exception:
        _logger.debug("Unable to read Windows printer info queue=%s", queue_name, exc_info=True)
        return {}
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass
    return info if isinstance(info, dict) else {}


def probe_printer_identity_over_tcp(host: str, port: int = 9100, timeout: float = 0.6) -> dict[str, str]:
    """Probe a raw TCP printer (port 9100) and return its identity.

    Keys: ``language`` ("zpl" / "escpos"), ``model`` (the model string the
    printer reported, "" when unknown) and ``brand`` ("ZEBRA" for Zebra label
    printers).  An unreachable endpoint returns ``{}`` so that it is never
    cached as a positive ESC/POS detection.
    """
    host = str(host or "").strip()
    if not host:
        return {}
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 9100
    timeout = max(0.1, float(timeout or 0.0))
    deadline = time() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            # Step 1: ESC/POS real-time status.  A receipt printer answers a
            # real-time status request within milliseconds, so only part of the
            # budget is spent waiting for it.  ZPL printers stay silent, and the
            # rest of the budget must stay available for the answer to ``~HI``
            # below - otherwise a Zebra would be misdetected as ESC/POS.
            sock.settimeout(max(0.05, min(timeout * 0.5, 0.4)))
            try:
                sock.sendall(ESCPOS_STATUS_QUERY)
                response = sock.recv(64)
            except (socket.timeout, OSError):
                response = b""
            if response:
                return _identity_from_probe_response(response)
            # Step 2: ZPL host identification.  ZPL printers never answer the
            # ESC/POS status query above, so a model string here - Zebra
            # answers "<model>,<firmware>,<link>,<dpi>" - is a reliable
            # label-printer signal.
            remaining = max(0.15, deadline - time())
            sock.settimeout(remaining)
            sock.sendall(ZPL_PROBE_COMMAND)
            response = _read_probe_response(sock)
    except (OSError, ValueError):
        _logger.debug("Printer language probe failed host=%s port=%s", host, port, exc_info=True)
        return {}
    if not response:
        return {"language": PRINTER_LANGUAGE_ESCPOS}
    return _identity_from_probe_response(response)


def probe_printer_language_over_tcp(host: str, port: int = 9100, timeout: float = 0.6) -> str:
    """Probe a raw TCP printer and return its command language.

    Returns "zpl" for a ZPL label printer, "escpos" for everything else and ""
    when the printer could not be reached at all (an unreachable endpoint must
    never be cached as a positive ESC/POS result).
    """
    return str(probe_printer_identity_over_tcp(host, port, timeout).get("language") or "")


def _identity_from_probe_response(response: bytes) -> dict[str, str]:
    if not response_is_zpl(response):
        return {"language": PRINTER_LANGUAGE_ESCPOS}
    model = _probe_model(response)
    return {
        "language": PRINTER_LANGUAGE_ZPL,
        "model": model,
        "brand": printer_brand(model) if model else "",
    }


def _probe_model(response: bytes) -> str:
    """Return the model string a label printer reported for ``~HI``.

    Zebra wraps the answer in STX/ETX control characters
    ("\\x02GK420d-200dpi,V61.17.17Z,8,2104KB\\x03"), so they are stripped before
    the model is reported.
    """
    text = response.decode("ascii", errors="ignore").strip()
    if not text:
        return ""
    first_field = text.splitlines()[0].split(",")[0]
    model = "".join(char for char in first_field if char.isprintable()).strip()
    if not model or len(model) > 48:
        return ""
    return model if re.search(r"[A-Za-z0-9]", model) else ""


def _read_probe_response(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < _PROBE_RESPONSE_MAX_BYTES:
        try:
            chunk = sock.recv(64)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def response_is_zpl(response: bytes) -> bool:
    """Return whether a probe response looks like a ZPL host identification."""
    if not response:
        return False
    if b"\x1b" in response or b"\x00" in response:
        return False
    text = response.decode("ascii", errors="replace").strip()
    if not text or "^" in text:
        # An echoed ^XA/^XZ command is not proof that the printer speaks ZPL,
        # and neither is a garbled binary answer.
        return False
    if _is_zpl_identification(text):
        return True
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        if any(re.fullmatch(r"[0-9]{1,6}", part) for part in parts[1:]):
            return True
    return False


def _is_zpl_identification(text: str) -> bool:
    """Return whether a probe answer is a ZPL host identification.

    Zebra answers ``~HI`` with "<model>,<firmware>,<link>,<dpi>".  The model
    field either carries the "ZPL" token ("ZTC ZD220-203dpi ZPL") or a Zebra
    model family, and some firmware drops the trailing numeric fields, so the
    text itself is matched instead of counting comma separated fields.
    """
    lowered = text.lower()
    return "zpl" in lowered or zebra_matches(lowered)


def extract_zpl_text(payload: Any, *, allow_plain: bool = False, depth: int = 0) -> str:
    """Extract ready-to-send printer text from an Odoo print payload.

    ``allow_plain`` is used when the target printer is known to speak ZPL: the
    IoT Box then forwards whatever Odoo sent without generating any content of
    its own.  Without it only explicit ZPL payloads are returned.
    """
    if payload is None or depth > 4:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        return text if allow_plain or looks_like_zpl(text) else ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{"):
            parsed = _try_json(text)
            if isinstance(parsed, dict):
                return extract_zpl_text(parsed, allow_plain=allow_plain, depth=depth + 1)
        if _looks_like_image_source(text):
            return ""
        if allow_plain or looks_like_zpl(text):
            return text
        return ""
    if isinstance(payload, dict):
        for key in _ZPL_TEXT_KEYS:
            nested = extract_zpl_text(payload.get(key), allow_plain=False, depth=depth + 1)
            if nested:
                return nested
        for key in _RAW_PAYLOAD_KEYS:
            nested = extract_zpl_text(payload.get(key), allow_plain=False, depth=depth + 1)
            if nested:
                return nested
        for value in payload.values():
            # Any string that already contains ZPL instructions is forwarded
            # as-is, whatever key Odoo used to carry it.
            if isinstance(value, str) and looks_like_zpl(value):
                return value.strip()
        lines = payload.get("lines")
        if isinstance(lines, (list, tuple)):
            joined = "\n".join(
                str(line.get("text") or "").strip()
                for line in lines
                if isinstance(line, dict) and str(line.get("text") or "").strip()
            ).strip()
            if looks_like_zpl(joined):
                return joined
        return ""
    if isinstance(payload, (list, tuple)):
        parts = [extract_zpl_text(item, depth=depth + 1) for item in payload]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _looks_like_image_source(text: str) -> bool:
    lowered = text.strip()[:128].lower()
    return lowered.startswith(_IMAGE_SOURCE_PREFIXES)
