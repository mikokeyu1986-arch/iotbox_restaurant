import asyncio
import tempfile
import unittest
from pathlib import Path

from app.cloud_bridge import OdooCloudBridge


class _ConfigStoreStub:
    def __init__(self, directory: str) -> None:
        self.config_path = Path(directory) / "runtime_config.json"
        self.committed_message_ids = []

    def update_last_websocket_message_id(self, message_id, **kwargs):
        self.committed_message_ids.append(message_id)


class _BlockingDeviceManager:
    def __init__(self, result: bool = True) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.result = result

    async def execute(self, owner, device_identifier, data):
        self.started.set()
        await self.release.wait()
        return self.result


class CloudBridgeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_print_is_confirmed_only_after_physical_queue_finishes(self):
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
            await asyncio.sleep(0)
            self.assertFalse(confirmation_sent.is_set())
            self.assertFalse(task.done(), "confirmation must wait for the physical print result")

            manager.release.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(confirmation_sent.is_set())

    async def test_failed_print_is_confirmed_as_disconnected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _BlockingDeviceManager(result=False)
            bridge = OdooCloudBridge(
                _ConfigStoreStub(directory), manager, "iot-box", "127.0.0.1:8398", "test",
                verify_ssl=False,
            )
            statuses = []
            bridge._send_operation_confirmation = lambda *args: statuses.append(args[3])

            task = asyncio.create_task(
                bridge._execute_and_confirm(
                    "https://odoo.example", "session-1", "printer-1",
                    {"action": "print_receipt_escpos", "receipt": {"lines": [{"text": "A"}]}},
                )
            )
            await manager.started.wait()
            manager.release.set()
            await task

            self.assertEqual(statuses, ["disconnected"])

    async def test_confirmation_failure_propagates_for_message_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _BlockingDeviceManager()
            manager.release.set()
            bridge = OdooCloudBridge(
                _ConfigStoreStub(directory), manager, "iot-box", "127.0.0.1:8398", "test",
                verify_ssl=False,
            )

            def fail_confirmation(*args):
                raise OSError("odoo unavailable")

            bridge._send_operation_confirmation = fail_confirmation
            with self.assertRaisesRegex(OSError, "odoo unavailable"):
                await bridge._execute_and_confirm(
                    "https://odoo.example", "session-1", "printer-1",
                    {"action": "print_receipt_escpos", "receipt": {"lines": [{"text": "A"}]}},
                )

    async def test_message_cursor_is_committed_after_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _ConfigStoreStub(directory)
            bridge = OdooCloudBridge(
                store, _BlockingDeviceManager(), "iot-box", "127.0.0.1:8398", "test",
                verify_ssl=False,
            )
            handling_started = asyncio.Event()
            release_handling = asyncio.Event()

            async def delayed_handler(*args):
                handling_started.set()
                await release_handling.wait()
                return False

            bridge._handle_message = delayed_handler
            task = asyncio.create_task(
                bridge._handle_and_commit_message("https://odoo.example", {"id": 42})
            )
            await handling_started.wait()
            self.assertEqual(store.committed_message_ids, [])

            release_handling.set()
            await task
            self.assertEqual(store.committed_message_ids, [42])


if __name__ == "__main__":
    unittest.main()
