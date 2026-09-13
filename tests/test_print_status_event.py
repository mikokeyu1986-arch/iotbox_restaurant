"""A finished print must answer the POS event poll, success included.

The POS posts a print to ``/iot_drivers/action``, which returns as soon as the
job is queued, and then reads the outcome from ``/iot_drivers/event``: it
registers a listener for the device and waits for an event whose ``owner`` is
its action id.  Only failures were ever published, so a kitchen ticket that
printed perfectly left the POS waiting the full 50 s poll and showing the order
as not sent.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app.main as main
from app.event_bus import EventBus
from app.models import IoTEvent


SESSION = "36f4dedcf1acfbe8"
DEVICE = "custom-iot-box-6334aa11bb22__netprinter_50579cd0b80e"


def _listener() -> dict:
    """The listener payload Odoo's long polling sends for one printer."""
    return {
        "session_id": SESSION,
        "last_event": 0,
        "devices": {DEVICE: {"listener_id": SESSION, "device_identifier": DEVICE}},
    }


class PrintStatusEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self._original_bus = main.event_bus
        main.event_bus = self.bus

    def tearDown(self) -> None:
        main.event_bus = self._original_bus

    async def test_a_finished_kitchen_print_answers_the_event_poll(self):
        with patch.object(
            main.device_manager, "execute", AsyncMock(return_value=True)
        ):
            await main._execute_iot_action_background(
                SESSION, DEVICE, {"action": "print_receipt_escpos"}
            )

        event = await self.bus.poll(_listener(), timeout_seconds=0)

        self.assertIsNotNone(event, "the POS is still waiting for a print status")
        self.assertEqual(event["status"], "success")
        # The POS matches the event on the identifier it registered, which is
        # the one the action carried -- not the resolved printer identifier.
        self.assertEqual(event["device_identifier"], DEVICE)
        self.assertEqual(event["owner"], SESSION)

    async def test_the_status_waits_for_the_paper_not_just_the_queue(self):
        """Reporting success before the print finished would be a false receipt."""
        seen_during_print: list = []

        async def execute(*_args, **_kwargs):
            seen_during_print.append(await self.bus.poll(_listener(), timeout_seconds=0))
            return True

        with patch.object(main.device_manager, "execute", AsyncMock(side_effect=execute)):
            await main._execute_iot_action_background(
                SESSION, DEVICE, {"action": "print_receipt_escpos"}
            )

        self.assertEqual(seen_during_print, [None])
        self.assertIsNotNone(await self.bus.poll(_listener(), timeout_seconds=0))

    async def test_a_failed_print_still_reports_an_error(self):
        with patch.object(
            main.device_manager, "execute", AsyncMock(return_value=False)
        ):
            await main._execute_iot_action_background(
                SESSION, DEVICE, {"action": "print_receipt_escpos"}
            )

        event = await self.bus.poll(_listener(), timeout_seconds=0)

        self.assertIsNotNone(event)
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["message"], "ERROR_PRINTER")

    async def test_a_listener_for_another_action_does_not_consume_the_status(self):
        with patch.object(
            main.device_manager, "execute", AsyncMock(return_value=True)
        ):
            await main._execute_iot_action_background(
                SESSION, DEVICE, {"action": "print_receipt_escpos"}
            )

        other = _listener()
        other["devices"][DEVICE]["listener_id"] = "another-action"

        self.assertIsNone(await self.bus.poll(other, timeout_seconds=0))

    async def test_fast_publish_cannot_be_lost_before_poll_waits(self):
        """A sub-millisecond print completion must wake the pending POS poll."""
        poll = asyncio.create_task(self.bus.poll(_listener(), timeout_seconds=1))
        await asyncio.sleep(0)
        await self.bus.publish(
            IoTEvent(
                device_identifier=DEVICE,
                owner=SESSION,
                status="success",
            )
        )

        event = await asyncio.wait_for(poll, timeout=0.2)

        self.assertIsNotNone(event)
        self.assertEqual(event["owner"], SESSION)
        self.assertEqual(event["status"], "success")


if __name__ == "__main__":
    unittest.main()
