"""The kitchen ticket must honour every font size the template offers.

``apply_kitchen_template`` writes the configured size into ``width_multiplier``
and ``height_multiplier``, but ``_build_kitchen_escpos_bytes`` used to read the
``double_width``/``double_height`` booleans instead.  Those collapse anything
above 1 into "double", so a block set to 3 printed exactly like one set to 2 and
adjusting the size in the visual editor appeared to do nothing.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.device_manager import DeviceManager
from app.event_bus import EventBus

# ESC/POS "GS ! n": the high nibble scales the width, the low nibble the height,
# each stored as (multiplier - 1).
SIZE_COMMAND = b"\x1d\x21"


def _size_command(payload: bytes) -> bytes:
    index = payload.find(SIZE_COMMAND)
    return payload[index:index + 3] if index >= 0 else b""


class KitchenTicketSizeTests(unittest.TestCase):
    def _manager(self, directory: str) -> DeviceManager:
        return DeviceManager(EventBus(), Path(directory), iot_identifier="custom-iot-box-test")

    def _render(self, manager: DeviceManager, line: dict) -> bytes:
        return manager._build_kitchen_escpos_bytes([line])

    def test_each_size_above_one_encodes_a_different_multiplier(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            rendered = {}
            for size in (1, 2, 3, 4):
                rendered[size] = _size_command(self._render(manager, {
                    "type": "header_meta_line",
                    "left_text": "TIENDA",
                    "right_text": "20:00",
                    "width_multiplier": size,
                    "height_multiplier": size,
                }))

        self.assertEqual(rendered[1], b"", "size 1 must stay at the default size")
        # (2-1)<<4 | (2-1) == 0x11, (3-1)<<4 | (3-1) == 0x22, (4-1)<<4 | (4-1) == 0x33
        self.assertEqual(rendered[2], SIZE_COMMAND + b"\x11")
        self.assertEqual(rendered[3], SIZE_COMMAND + b"\x22")
        self.assertEqual(rendered[4], SIZE_COMMAND + b"\x33")
        self.assertEqual(
            len({rendered[2], rendered[3], rendered[4]}), 3,
            "sizes above 1 must not all collapse to the same command",
        )

    def test_product_lines_keep_their_multiplier(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)

            def render(size: int) -> bytes:
                return _size_command(self._render(manager, {
                    "type": "product_line",
                    "qty": "1",
                    "name": "Pizza",
                    "width_multiplier": size,
                    "height_multiplier": size,
                }))

            self.assertEqual(render(1), b"")
            self.assertEqual(render(2), SIZE_COMMAND + b"\x11")
            self.assertEqual(render(3), SIZE_COMMAND + b"\x22")

    def test_the_size_is_reset_after_a_scaled_line(self):
        """A scaled line must not leak its size into whatever follows it."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            payload = self._render(manager, {
                "type": "header_meta_line",
                "left_text": "GRANDE",
                "right_text": "",
                "width_multiplier": 3,
                "height_multiplier": 3,
            })
            commands = [
                payload[i:i + 3]
                for i in range(len(payload) - 2)
                if payload[i:i + 2] == SIZE_COMMAND
            ]

        self.assertEqual(commands[0], SIZE_COMMAND + b"\x22")
        self.assertEqual(commands[-1], SIZE_COMMAND + b"\x00", "size must be reset to 1x1")


if __name__ == "__main__":
    unittest.main()
