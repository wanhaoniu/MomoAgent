#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
MASTER_ROOT = ROOT.parent
if str(MASTER_ROOT) not in sys.path:
    sys.path.insert(0, str(MASTER_ROOT))

from soarmmoce_sdk import SoArmMoceController


LOGGER = logging.getLogger("gesture_action_runner")


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config must be a YAML object: {path}")
    return payload


def _fetch_json(url: str, timeout_sec: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} when requesting {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to connect to {url}: {exc}") from exc
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected payload from {url}")
    return data


class GestureActionRunner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.service_endpoint = str(config.get("service", {}).get("endpoint", "http://127.0.0.1:8012/latest")).strip()
        self.poll_interval_sec = float(config.get("service", {}).get("poll_interval_sec", 0.15))
        self.request_timeout_sec = float(config.get("service", {}).get("request_timeout_sec", 2.0))
        self.dry_run = bool(config.get("runner", {}).get("dry_run", False))
        self.actions = dict(config.get("actions", {}))
        self._controller: SoArmMoceController | None = None
        self._last_trigger_time: dict[str, float] = {}
        self._last_stable_gesture: str | None = None

    def run(self) -> None:
        LOGGER.info("Polling gesture endpoint: %s", self.service_endpoint)
        while True:
            try:
                payload = _fetch_json(self.service_endpoint, self.request_timeout_sec)
                self._handle_payload(payload)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                LOGGER.warning("Gesture runner poll failed: %s", exc)
            time.sleep(self.poll_interval_sec)

    def close(self) -> None:
        if self._controller is not None:
            try:
                self._controller.close()
            finally:
                self._controller = None

    def _get_controller(self) -> SoArmMoceController:
        if self._controller is None:
            self._controller = SoArmMoceController()
        return self._controller

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        gesture = payload.get("stable_gesture_name")
        if not gesture:
            self._last_stable_gesture = None
            return

        action_cfg = self.actions.get(str(gesture))
        if not isinstance(action_cfg, dict):
            return

        cooldown_sec = float(action_cfg.get("cooldown_sec", 1.0))
        now = time.time()
        should_trigger = False
        if gesture != self._last_stable_gesture:
            should_trigger = True
        elif bool(action_cfg.get("repeat_while_held", False)) and now - self._last_trigger_time.get(str(gesture), 0.0) >= cooldown_sec:
            should_trigger = True

        if not should_trigger:
            return
        if now - self._last_trigger_time.get(str(gesture), 0.0) < cooldown_sec:
            self._last_stable_gesture = str(gesture)
            return

        LOGGER.info("Trigger gesture action: %s -> %s", gesture, action_cfg.get("action"))
        self._execute_action(action_cfg)
        self._last_trigger_time[str(gesture)] = now
        self._last_stable_gesture = str(gesture)

    def _execute_action(self, action_cfg: dict[str, Any]) -> None:
        action = str(action_cfg.get("action", "")).strip().lower()
        if not action:
            raise RuntimeError("gesture action config is missing action")
        if self.dry_run:
            LOGGER.info("Dry-run action: %s config=%s", action, action_cfg)
            return

        controller = self._get_controller()
        wait = bool(action_cfg.get("wait", True))
        duration = float(action_cfg.get("duration", 1.0))

        if action == "stop":
            controller.stop()
            return
        if action == "home":
            controller.home(duration=duration, wait=wait)
            return
        if action == "open_gripper":
            controller.open_gripper(duration=duration, wait=wait)
            return
        if action == "close_gripper":
            controller.close_gripper(duration=duration, wait=wait)
            return
        if action == "set_gripper":
            controller.set_gripper(open_ratio=float(action_cfg["open_ratio"]), duration=duration, wait=wait)
            return
        if action == "move_joint":
            controller.move_joint(
                joint=str(action_cfg["joint_name"]),
                target_deg=action_cfg.get("target_deg"),
                delta_deg=action_cfg.get("delta_deg"),
                duration=duration,
                wait=wait,
            )
            return
        if action == "move_delta":
            controller.move_delta(
                dx=float(action_cfg.get("dx", 0.0)),
                dy=float(action_cfg.get("dy", 0.0)),
                dz=float(action_cfg.get("dz", 0.0)),
                frame=str(action_cfg.get("frame", "base")),
                duration=duration,
                wait=wait,
            )
            return
        raise RuntimeError(f"Unsupported gesture action: {action}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Map stable gestures to robot actions")
    parser.add_argument(
        "--config",
        default=str(ROOT / "configs" / "actions.default.yaml"),
        help="Path to gesture action YAML config",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    config = _load_yaml(Path(args.config).expanduser().resolve())
    runner = GestureActionRunner(config)
    try:
        runner.run()
    finally:
        runner.close()


if __name__ == "__main__":
    main()
