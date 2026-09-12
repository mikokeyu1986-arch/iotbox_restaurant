from decimal import Decimal, InvalidOperation
import time
from typing import Any


def _price(value: Any) -> str:
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError, TypeError):
        return "0.00"


def _line(payload: dict[str, Any]) -> dict[str, Any] | None:
    focus = payload.get("focus_line")
    if isinstance(focus, dict):
        return focus
    lines = payload.get("lines")
    if isinstance(lines, list):
        values = [line for line in lines if isinstance(line, dict)]
        return values[-1] if values else None
    return None


def build_lines(payload: dict[str, Any], width: int = 20, rows: int = 2) -> list[str]:
    if payload.get("action") == "clear":
        return []
    currency = str(payload.get("currency") or "EUR").upper()
    if payload.get("status") == "paid":
        try:
            change_value = abs(Decimal(str(payload.get("change") or 0)))
        except (InvalidOperation, ValueError, TypeError):
            change_value = Decimal("0")
        return ["CAMBIO:".ljust(width), f"-{_price(change_value)} {currency}"[:width].rjust(width)][:max(rows, 1)]
    line = _line(payload)
    if not line and not payload.get("lines") and payload.get("status") in {None, "idle", "cart"}:
        return ["Bienvenido"[:width].ljust(width), "".ljust(width)][:max(rows, 1)]
    if payload.get("status") == "payment" and not payload.get("payments"):
        total = f"{_price(payload.get('total'))} {currency}"
        return ["TOTAL:".ljust(width), total[:width].rjust(width)][:max(rows, 1)]
    if line:
        if payload.get("status") == "payment":
            payments = payload.get("payments")
            if isinstance(payments, list) and payments:
                payment = payments[-1] if isinstance(payments[-1], dict) else {}
                method = str(payment.get("method") or "PAGO").strip()
                amount = f"{_price(payment.get('amount'))} {currency}"
                return [method[:width].ljust(width), amount[:width].rjust(width)][:max(rows, 1)]
        name = str(line.get("name") or "").strip()
        first = name[:width].ljust(width)
        # Show unit price while selecting products; show the order total on
        # payment/receipt screens.
        amount = line.get("unit_price", line.get("price_unit"))
        second = f"{_price(amount)} {currency}"[:width].rjust(width)
        return [first, second][:max(rows, 1)]
    lines = payload.get("lines")
    if isinstance(lines, list) and lines:
        return [str(value or "")[:width].ljust(width) for value in lines[:max(rows, 1)]]
    name = str(payload.get("name") or "").strip()
    return [f"{name}"[:width].ljust(width),
            f"{_price(payload.get('price_unit'))} EUR"[:width].rjust(width)][:max(rows, 1)]


def write_serial(payload: dict[str, Any], *, port: str, baudrate: int = 9600,
                 width: int = 20, rows: int = 2, protocol: str = "cd5220",
                 encoding: str = "ascii", clear_hex: str = "0C", line2_hex: str = "") -> list[str]:
    import serial

    lines = build_lines(payload, width, rows)
    clear = bytes.fromhex(str(clear_hex or "").replace(" ", "")) if clear_hex else b""
    if protocol == "cd5220":
        message = b""
        if lines:
            message = b"\x1BQA" + lines[0].encode("latin1", errors="replace") + b"\x0D"
            if len(lines) > 1:
                message += b"\x1BQB" + lines[1].encode("latin1", errors="replace") + b"\x0D"
    else:
        separator = bytes.fromhex(str(line2_hex or "").replace(" ", "")) if line2_hex else b"\n"
        message = separator.join(line.encode(encoding, errors="replace") for line in lines)
    with serial.Serial(port=port, baudrate=baudrate, timeout=1, write_timeout=1) as serial_port:
        if clear:
            serial_port.write(clear)
            serial_port.flush()
            time.sleep(0.1)
        if message:
            serial_port.write(message)
            serial_port.flush()
    return lines
