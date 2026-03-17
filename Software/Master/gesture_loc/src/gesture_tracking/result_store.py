from __future__ import annotations

import copy
import threading
from typing import Any


class ResultStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._latest: dict[str, Any] | None = None
        self._version = 0

    def publish(self, payload: dict[str, Any]) -> int:
        with self._condition:
            self._latest = copy.deepcopy(payload)
            self._version += 1
            self._condition.notify_all()
            return self._version

    def get_latest(self) -> tuple[dict[str, Any] | None, int]:
        with self._lock:
            payload = copy.deepcopy(self._latest)
            return payload, self._version
