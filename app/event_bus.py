from __future__ import annotations

import asyncio
import logging
import os
from time import time
from typing import Any

from .models import IoTEvent

_logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()
        # Odoo 19 event_manager 只保留 5 秒内的事件（oldest_time = time.time() - 5）。
        # 保留太久会导致 POS 拿到过期的旧事件（如 owner 为空的旧 scale 事件覆盖新事件）。
        self._retention_seconds = max(5.0, float(os.getenv("IOT_EVENT_RETENTION_SECONDS", "5")))
        self._max_events = max(100, int(os.getenv("IOT_EVENT_MAX_EVENTS", "1000")))

    async def publish(self, event: IoTEvent) -> None:
        payload = event.as_payload()
        async with self._condition:
            self._events.append(payload)
            self._prune_old_events()
            # 热路径：电子秤流式数据每帧都会 publish，INFO 日志的格式化 + handler I/O
            # 会拖延下面的 notify_all()，直接增加 POS 收到重量的延迟。降到 debug。
            _logger.debug(
                "EventBus publish owner=%s device=%s status=%s retained_events=%s",
                payload.get("owner", ""),
                payload.get("device_identifier", ""),
                payload.get("status", ""),
                len(self._events),
            )
            self._condition.notify_all()

    async def poll(self, listener: dict[str, Any], timeout_seconds: int = 50) -> dict[str, Any] | None:
        session_id = str(listener.get("session_id", ""))
        last_event = float(listener.get("last_event", 0))
        raw_devices = listener.get("devices") or {}
        devices = set(raw_devices.keys()) if isinstance(raw_devices, dict) else set(raw_devices or [])
        expected_owners = self._extract_expected_owners(raw_devices)
        # 注意：不要用 session_id 作为 expected_owners 的回退！
        # POS 重新注册 onMessage 时 listener_id=null，但 session_id 是 POS 全局 ID，
        # 与事件中的 owner（action 的 messageId）不同，会导致所有事件被过滤掉。
        _logger.info(
            "EventBus poll start session_id=%s devices=%s expected_owners=%s last_event=%s retained_events=%s",
            session_id,
            sorted(devices),
            sorted(expected_owners),
            last_event,
            len(self._events),
        )

        deadline = time() + timeout_seconds
        async with self._condition:
            while True:
                # Check while holding the same condition lock used by publish().
                # Previously the first check happened before acquiring this
                # lock.  A fast printer could publish between that check and
                # condition.wait(), losing the notification and leaving the
                # POS blocked for the full 50-second poll timeout.
                found = self._find_matching(devices, last_event, expected_owners)
                if found:
                    found["session_id"] = session_id
                    _logger.info(
                        "EventBus poll hit session_id=%s device=%s owner=%s status=%s retained_events=%s",
                        session_id,
                        found.get("device_identifier", ""),
                        found.get("owner", ""),
                        found.get("status", ""),
                        len(self._events),
                    )
                    return found
                remaining = deadline - time()
                if remaining <= 0:
                    _logger.info(
                        "EventBus poll timeout session_id=%s devices=%s expected_owners=%s retained_events=%s",
                        session_id,
                        sorted(devices),
                        sorted(expected_owners),
                        len(self._events),
                    )
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    _logger.info(
                        "EventBus poll wait timeout session_id=%s devices=%s expected_owners=%s retained_events=%s",
                        session_id,
                        sorted(devices),
                        sorted(expected_owners),
                        len(self._events),
                    )
                    return None

    def _find_matching(
        self, devices: set[str], last_event: float, expected_owners: set[str] | None = None
    ) -> dict[str, Any] | None:
        # 第一轮：优先返回 owner 匹配的事件（Odoo 19 POS onMessage 检查 message.owner === requestId）
        for event in reversed(self._events):
            if event["time"] <= last_event:
                continue
            if event["device_identifier"] in devices:
                event_owner = str(event.get("owner", ""))
                if expected_owners and event_owner and event_owner in expected_owners:
                    return dict(event)
        # 第二轮：返回空 owner 的广播事件（匹配所有 listener）
        for event in reversed(self._events):
            if event["time"] <= last_event:
                continue
            if event["device_identifier"] in devices:
                event_owner = str(event.get("owner", ""))
                if not event_owner:
                    return dict(event)
        # 第三轮：如果没有 expected_owners，返回任何匹配的事件
        if not expected_owners:
            for event in reversed(self._events):
                if event["time"] <= last_event:
                    continue
                if event["device_identifier"] in devices:
                    return dict(event)
        return None

    def _extract_expected_owners(self, raw_devices: Any) -> set[str]:
        if not isinstance(raw_devices, dict):
            return set()
        owners: set[str] = set()
        for value in raw_devices.values():
            if isinstance(value, dict):
                raw_listener_id = value.get("listener_id")
                if raw_listener_id is None:
                    continue
                listener_id = str(raw_listener_id).strip()
                if listener_id:
                    owners.add(listener_id)
        return owners

    def _prune_old_events(self) -> None:
        oldest = time() - self._retention_seconds
        self._events = [ev for ev in self._events if ev["time"] >= oldest]
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
