import asyncio
import tempfile
import unittest
from pathlib import Path

from app.cloud_bridge import OdooCloudBridge


class _ConfigStoreStub:
    def __init__(self, directory: str) -> None:
        self.config_path = Path(directory) / "runtime_config.json"


class _BlockingDeviceManager:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, owner, device_identifier, data):
        self.started.set()
        await self.release.wait()
        return True


class CloudBridgeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_print_is_confirmed_before_physical_queue_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _BlockingDeviceManager()
            bridge = OdooCloudBridge(
                _ConfigStoreStub(directory),
                manager,
                "iot-box",
                "127.0.0.1:8398",
                "test",
                verify_ssl=False,
            )
            confirmation_sent = asyncio.Event()

            def confirm(*args):
                confirmation_sent.set()

            bridge._send_operation_confirmation = confirm
            task = asyncio.create_task(
                bridge._execute_and_confirm(
                    "https://odoo.example",
                    "session-1",
                    "printer-1",
                    {"action": "print_receipt_escpos", "receipt": {"lines": [{"text": "A"}]}},
                )
            )

            await asyncio.wait_for(manager.started.wait(), timeout=1)
            await asyncio.wait_for(confirmation_sent.wait(), timeout=1)
            self.assertFalse(task.done(), "physical printing should still be waiting in its queue")

            manager.release.set()
            await asyncio.wait_for(task, timeout=1)


if __name__ == "__main__":
    unittest.main()
