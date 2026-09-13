from __future__ import annotations

import asyncio
from typing import Any


def reject_queued_jobs(queues: dict[str, asyncio.Queue[dict[str, Any]]]) -> None:
    """Resolve jobs left behind when printer workers are stopped."""
    for queue in queues.values():
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            future = job.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result(False)
            queue.task_done()
