from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass(slots=True)
class Device:
    identifier: str
    name: str
    type: str
    connection: str
    subtype: str = ""
    manufacturer: str | None = None
    status: str = "connected"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        return cls(
            identifier=str(data.get("identifier", "")),
            name=str(data.get("name", "")),
            type=str(data.get("type", "")),
            connection=str(data.get("connection", "")),
            subtype=str(data.get("subtype", "")),
            manufacturer=str(data.get("manufacturer") or "") if data.get("manufacturer") else None,
            status=str(data.get("status", "connected")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class IoTEvent:
    device_identifier: str
    owner: str
    status: str
    result: Any = None
    message: str | None = None
    when: float = field(default_factory=time)
    # 额外字段（Odoo 19 Driver.data 包含 'value' 字段，POS 前端可能读取它）
    extra: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "device_identifier": self.device_identifier,
            "owner": self.owner,
            "status": self.status,
            "time": self.when,
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.message is not None:
            payload["message"] = self.message
        # 合并额外字段（如 value、action_args 等 Odoo 19 协议要求的字段）
        for key, val in self.extra.items():
            if key not in payload:
                payload[key] = val
        return payload
