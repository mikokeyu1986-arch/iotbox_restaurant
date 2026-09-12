from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import Operation


class OperationStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._operations = self._load()

    def add(self, operation: Operation) -> Operation:
        with self._lock:
            self._operations.append(operation)
            self._save()
        return operation

    def list(self) -> list[Operation]:
        with self._lock:
            return list(self._operations)

    def latest(self) -> Operation | None:
        with self._lock:
            return self._operations[-1] if self._operations else None

    def find(self, operation_id: str) -> Operation | None:
        with self._lock:
            for operation in reversed(self._operations):
                if operation.id == operation_id:
                    return operation
        return None

    def find_by_request(self, request_value: str) -> Operation | None:
        request_value = str(request_value or "").strip()
        if not request_value:
            return None
        with self._lock:
            for operation in reversed(self._operations):
                if operation.request == request_value or operation.invoice == request_value:
                    return operation
        return None

    def _load(self) -> list[Operation]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        operations: list[Operation] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                try:
                    operations.append(Operation(**item))
                except TypeError:
                    continue
        return operations

    def _save(self) -> None:
        payload = [operation.to_dict() for operation in self._operations]
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)
