# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Sequence
import time
import numpy as np

from .base import TransportBase

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-posix fallback
    fcntl = None


_MOCK_SHARED_FILE_ENV = "SOARMMOCE_MOCK_SHARED_STATE_FILE"


class MockTransport(TransportBase):
    """Mock transport for tests and examples (no hardware)."""

    def __init__(self, dof: int):
        super().__init__(dof)
        self._connected = False
        self._q = np.zeros(self.dof, dtype=float)
        self._gripper_open_ratio = 1.0
        self._motion_end_time = 0.0
        raw_shared = str(os.getenv(_MOCK_SHARED_FILE_ENV, "")).strip()
        self._shared_state_path = Path(raw_shared).expanduser() if raw_shared else None

    def connect(self) -> None:
        self._connected = True
        state = self._shared_read_state()
        if state is not None:
            self._apply_state(state)

    def disconnect(self) -> None:
        self._connected = False

    def get_q(self) -> np.ndarray:
        if not self._connected:
            raise RuntimeError("MockTransport not connected")
        state = self._shared_read_state()
        if state is not None:
            self._apply_state(state)
        return self._q.copy()

    def send_movej(
        self,
        q: Sequence[float],
        duration: float,
        speed: Optional[float] = None,
        accel: Optional[float] = None,
    ) -> None:
        if not self._connected:
            raise RuntimeError("MockTransport not connected")
        q = np.asarray(q, dtype=float).reshape(-1)
        if q.shape[0] != self.dof:
            raise ValueError(f"Expected {self.dof} joints, got {q.shape[0]}")
        motion_end = time.monotonic() + max(0.0, float(duration))
        self._q = q.copy()
        self._motion_end_time = motion_end
        self._shared_write_state(
            q=self._q,
            gripper_open_ratio=self._gripper_open_ratio,
            motion_end_time=self._motion_end_time,
        )

    def stop(self) -> None:
        self._motion_end_time = time.monotonic()
        self._shared_write_state(
            q=self._q,
            gripper_open_ratio=self._gripper_open_ratio,
            motion_end_time=self._motion_end_time,
        )

    def wait_until_stopped(self, timeout: Optional[float] = None) -> bool:
        if not self._connected:
            raise RuntimeError("MockTransport not connected")
        state = self._shared_read_state()
        if state is not None:
            self._apply_state(state)
        remaining = max(0.0, self._motion_end_time - time.monotonic())
        if timeout is not None:
            timeout = max(0.0, float(timeout))
            if remaining > timeout:
                time.sleep(timeout)
                return False
        if remaining > 0.0:
            time.sleep(remaining)
        return True

    def set_gripper(self, open_ratio: float, wait: bool = True, timeout: Optional[float] = None) -> None:
        if not self._connected:
            raise RuntimeError("MockTransport not connected")
        ratio = float(open_ratio)
        if ratio < 0.0 or ratio > 1.0:
            raise ValueError("open_ratio must be within [0.0, 1.0]")
        self._gripper_open_ratio = ratio
        self._motion_end_time = time.monotonic() + 0.05
        self._shared_write_state(
            q=self._q,
            gripper_open_ratio=self._gripper_open_ratio,
            motion_end_time=self._motion_end_time,
        )
        if wait:
            self.wait_until_stopped(timeout=timeout)

    def get_gripper_open_ratio(self) -> Optional[float]:
        if not self._connected:
            raise RuntimeError("MockTransport not connected")
        state = self._shared_read_state()
        if state is not None:
            self._apply_state(state)
        return float(self._gripper_open_ratio)

    # ---- shared mock state helpers ----

    def _default_state(self) -> dict:
        return {
            "dof": int(self.dof),
            "q": [0.0 for _ in range(self.dof)],
            "gripper_open_ratio": 1.0,
            "motion_end_time": 0.0,
        }

    def _normalize_state(self, raw: object) -> dict:
        data = raw if isinstance(raw, dict) else {}
        q_raw = data.get("q", [])
        try:
            q = np.asarray(q_raw, dtype=float).reshape(-1)
        except Exception:
            q = np.zeros(self.dof, dtype=float)
        if q.shape[0] != self.dof:
            q = np.zeros(self.dof, dtype=float)

        try:
            ratio = float(data.get("gripper_open_ratio", 1.0))
        except Exception:
            ratio = 1.0
        ratio = float(min(1.0, max(0.0, ratio)))

        try:
            motion_end = float(data.get("motion_end_time", 0.0))
        except Exception:
            motion_end = 0.0
        motion_end = max(0.0, motion_end)

        return {
            "dof": int(self.dof),
            "q": [float(v) for v in q.tolist()],
            "gripper_open_ratio": ratio,
            "motion_end_time": motion_end,
        }

    def _apply_state(self, state: dict) -> None:
        self._q = np.asarray(state.get("q", []), dtype=float).reshape(-1)
        if self._q.shape[0] != self.dof:
            self._q = np.zeros(self.dof, dtype=float)
        self._gripper_open_ratio = float(state.get("gripper_open_ratio", 1.0))
        self._motion_end_time = float(state.get("motion_end_time", 0.0))

    def _shared_read_state(self) -> Optional[dict]:
        if self._shared_state_path is None:
            return None
        return self._shared_load_or_update(None)

    def _shared_write_state(self, q: np.ndarray, gripper_open_ratio: float, motion_end_time: float) -> None:
        if self._shared_state_path is None:
            return

        def _updater(current: dict) -> dict:
            current["q"] = [float(v) for v in np.asarray(q, dtype=float).reshape(-1).tolist()]
            current["gripper_open_ratio"] = float(min(1.0, max(0.0, gripper_open_ratio)))
            current["motion_end_time"] = float(max(0.0, motion_end_time))
            return current

        self._shared_load_or_update(_updater)

    def _shared_load_or_update(self, updater) -> dict:
        assert self._shared_state_path is not None
        path = self._shared_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")

        with path.open("r+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                if raw:
                    try:
                        state = self._normalize_state(json.loads(raw))
                    except Exception:
                        state = self._default_state()
                else:
                    state = self._default_state()

                if updater is not None:
                    state = self._normalize_state(updater(dict(state)))
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(state, ensure_ascii=False))
                    f.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return state
