#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from soarmmoce_sdk import Robot


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
DEFAULT_SHARED_STATE = "/tmp/soarmmoce_mock_shared_state.json"


def _extract_reply(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        texts = [_extract_reply(x) for x in payload]
        texts = [x for x in texts if x]
        return "\n".join(texts).strip()
    if isinstance(payload, dict):
        if "result" in payload:
            return _extract_reply(payload.get("result"))
        items = payload.get("payloads")
        if isinstance(items, list):
            texts = []
            for item in items:
                if isinstance(item, dict):
                    t = str(item.get("text", "")).strip()
                    if t:
                        texts.append(t)
            if texts:
                return texts[-1]
        for key in ("text", "message", "content", "reply", "answer"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _build_message(user_text: str, skill_name: str) -> str:
    return f"请使用 ${skill_name} 处理机械臂控制请求。\n{str(user_text or '').strip()}"


def _state_xyz(robot: Robot) -> np.ndarray:
    state = robot.get_state()
    return np.asarray(state.tcp_pose.xyz, dtype=float).reshape(3)


def _run_openclaw(
    message: str,
    *,
    skill_name: str,
    agent_id: str,
    session_id: str,
    thinking: str,
    timeout_sec: float,
    local: bool,
    env: dict,
) -> tuple[int, str, str]:
    cmd = [
        "openclaw",
        "--no-color",
        "agent",
        "--json",
        "--message",
        message,
        "--thinking",
        thinking,
        "--timeout",
        str(float(timeout_sec)),
    ]
    if local:
        cmd.append("--local")
    if session_id:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--agent", agent_id])

    cwd = None
    skill_dir = Path.home() / ".openclaw" / "skills" / str(skill_name or "").strip()
    if skill_dir.exists() and skill_dir.is_dir():
        cwd = str(skill_dir)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(5.0, float(timeout_sec) + 5.0),
        check=False,
        env=env,
        cwd=cwd,
    )
    return int(proc.returncode), str(proc.stdout or "").strip(), str(proc.stderr or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenClaw->skill->SDK chain.")
    parser.add_argument("--message", default="把机械臂抬高一点", help="User message text.")
    parser.add_argument("--skill", default="soarmmoce-control", help="Skill name for prompt prefix.")
    parser.add_argument("--agent-id", default="main")
    parser.add_argument("--session-id", default="", help="Optional fixed session id.")
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Force a fresh random session id (default uses --agent).",
    )
    parser.add_argument("--thinking", default="minimal")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--local", action="store_true", help="Use openclaw --local.")
    parser.add_argument("--config", default=None, help="Optional SDK config path.")
    parser.add_argument("--shared-state", default=DEFAULT_SHARED_STATE)
    args = parser.parse_args()

    env = os.environ.copy()
    env["SOARMMOCE_MOCK_SHARED_STATE_FILE"] = str(args.shared_state)
    os.environ["SOARMMOCE_MOCK_SHARED_STATE_FILE"] = str(args.shared_state)
    sdk_src_str = str(SDK_SRC.resolve())
    existing_pp = str(env.get("PYTHONPATH", "")).strip()
    env["PYTHONPATH"] = f"{sdk_src_str}:{existing_pp}" if existing_pp else sdk_src_str

    robot = Robot.from_config(args.config) if args.config else Robot()
    robot.connect()
    try:
        before = _state_xyz(robot)
        sid = str(args.session_id or "").strip()
        if bool(args.new_session) and not sid:
            sid = uuid.uuid4().hex
        full_message = _build_message(args.message, args.skill)
        code, stdout, stderr = _run_openclaw(
            full_message,
            skill_name=str(args.skill),
            agent_id=str(args.agent_id),
            session_id=sid,
            thinking=str(args.thinking),
            timeout_sec=float(args.timeout),
            local=bool(args.local),
            env=env,
        )

        print(f"[openclaw] returncode={code}")
        if stderr:
            print(f"[openclaw] stderr={stderr}")

        payload: Any = {}
        if stdout:
            try:
                payload = json.loads(stdout)
            except Exception:
                payload = {"text": stdout}
        reply = _extract_reply(payload)
        if not reply and stdout:
            print("[openclaw] raw_stdout=%s" % stdout[:500].replace("\n", "\\n"))
        print(f"[openclaw] reply={reply}")

        after = _state_xyz(robot)
        delta = after - before
        print(
            "[state] before_xyz=(%.6f, %.6f, %.6f)"
            % (float(before[0]), float(before[1]), float(before[2]))
        )
        print(
            "[state] after_xyz =(%.6f, %.6f, %.6f)"
            % (float(after[0]), float(after[1]), float(after[2]))
        )
        print(
            "[state] delta_xyz =(%.6f, %.6f, %.6f) norm=%.6f"
            % (float(delta[0]), float(delta[1]), float(delta[2]), float(np.linalg.norm(delta)))
        )

        if code != 0:
            return 2
        return 0
    finally:
        try:
            robot.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
