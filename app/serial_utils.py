from __future__ import annotations

import logging
import re
import threading
from typing import Any

_logger = logging.getLogger(__name__)


def list_serial_ports() -> list[dict[str, str]]:
    try:
        import serial.tools.list_ports
    except ImportError:
        return []

    ports = serial.tools.list_ports.comports()
    result: list[dict[str, str]] = []
    for port in sorted(ports, key=lambda p: _port_sort_key(p.device)):
        # pyserial 3.x splits USB info into separate ``vid`` and ``pid``
        # attributes (ints). ``vid_pid`` only exists on newer pyserial forks,
        # so we synthesize a "VID:PID" string defensively to avoid breaking
        # the whole port enumeration when one attribute is missing.
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        if vid is not None and pid is not None:
            vid_pid = f"{vid:04X}:{pid:04X}"
        else:
            vid_pid = ""

        entry = {
            "device": port.device,
            "description": port.description or "",
            "manufacturer": port.manufacturer or "",
            "vid_pid": vid_pid,
            "hwid": getattr(port, "hwid", "") or "",
            "serial_number": getattr(port, "serial_number", "") or "",
        }
        result.append(entry)
    return result


def _port_sort_key(device: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", device)
    num = int(match.group(1)) if match else 9999
    return (num, device)


class SharedSerialPort:
    def __init__(
        self,
        device: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        timeout: float = 1.0,
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self._lock = threading.Lock()
        self._serial: Any = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and self._serial.is_open

    def open(self) -> None:
        with self._lock:
            if self._connected:
                return
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError("pyserial is required for serial port support") from exc
            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self._connected = True
            _logger.info("Serial port opened: %s @ %d baud", self.device, self.baudrate)

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._connected = False
            _logger.info("Serial port closed: %s", self.device)

    def write(self, data: bytes) -> int:
        with self._lock:
            if not self._connected or self._serial is None:
                raise IOError("Serial port not open")
            return self._serial.write(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            if not self._connected or self._serial is None:
                raise IOError("Serial port not open")
            data = self._serial.read(size)
            return bytes(data)

    def read_all(self) -> bytes:
        with self._lock:
            if not self._connected or self._serial is None:
                raise IOError("Serial port not open")
            waiting = self._serial.in_waiting
            if waiting <= 0:
                return b""
            data = self._serial.read(waiting)
            return bytes(data)

    def flush_input(self) -> None:
        with self._lock:
            if self._connected and self._serial is not None:
                self._serial.reset_input_buffer()

    def flush_output(self) -> None:
        with self._lock:
            if self._connected and self._serial is not None:
                self._serial.reset_output_buffer()

    def __enter__(self) -> "SharedSerialPort":
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
