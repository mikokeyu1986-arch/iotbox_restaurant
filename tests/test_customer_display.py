import tempfile
import unittest
from pathlib import Path

from app.device_manager import DeviceManager
from app.event_bus import EventBus
from app.models import Device


class CustomerDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_display_set_action_is_forwarded_to_renderer(self):
        received = []
        with tempfile.TemporaryDirectory() as directory:
            manager = DeviceManager(
                EventBus(),
                Path(directory),
                customer_display_handler=received.append,
            )
            manager.devices["customer_display"] = Device(
                identifier="customer_display",
                name="Customer Display",
                type="display",
                connection="hdmi",
            )
            ok = await manager.execute(
                "pos-request-1",
                "customer_display",
                {"action": "set", "data": {"total": "12.50"}},
            )
        self.assertTrue(ok)
        self.assertEqual(received, [{"action": "set", "data": {"total": "12.50"}}])


if __name__ == "__main__":
    unittest.main()
