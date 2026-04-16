from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .errors import QuickControlError
from .scene_config import haiguitang_intro_video_file
from .schemas import (
    AgentAskRequest,
    AgentWarmupRequest,
    CartesianJogRequest,
    ConnectRequest,
    FollowStartRequest,
    HaiGuiTangActionRequest,
    HaiGuiTangStartRequest,
    HomeRequest,
    IdleScanStartRequest,
    JointStepRequest,
)
from .service import QuickControlService

AGENT_STREAM_TEST_PAGE = Path(__file__).resolve().parents[2] / "agent_stream_test.html"


class OpenClawChatStreamBridge:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._command: tuple[str, ...] = ()

    async def close(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._command = ()
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        except Exception:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    pass

    async def _ensure_proc_locked(self, command: list[str]) -> asyncio.subprocess.Process:
        normalized_command = tuple(str(part or "").strip() for part in (command or []) if str(part).strip())
        if not normalized_command:
            raise RuntimeError("OpenClaw chat stream bridge command is empty")

        proc = self._proc
        if (
            proc is not None
            and proc.returncode is None
            and normalized_command == self._command
            and proc.stdin is not None
            and proc.stdout is not None
        ):
            return proc

        await self._stop_locked()
        proc = await asyncio.create_subprocess_exec(
            *normalized_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parents[5]),
        )
        self._proc = proc
        self._command = normalized_command
        return proc

    async def relay(
        self,
        *,
        command: list[str],
        stdin_payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        request_id = str(stdin_payload.get("id", "") or "").strip()
        if not request_id:
            raise RuntimeError("OpenClaw chat stream bridge request id is missing")

        async with self._lock:
            proc = await self._ensure_proc_locked(command)
            if proc.stdin is None or proc.stdout is None:
                await self._stop_locked()
                raise RuntimeError("OpenClaw chat stream bridge pipes are unavailable")

            payload_bytes = (json.dumps(stdin_payload, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                proc.stdin.write(payload_bytes)
                await proc.stdin.drain()
                while True:
                    raw_line = await proc.stdout.readline()
                    if not raw_line:
                        raise RuntimeError("OpenClaw chat stream bridge exited unexpectedly")
                    text_line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not text_line:
                        continue
                    try:
                        event = json.loads(text_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if str(event.get("id", "") or "").strip() != request_id:
                        continue
                    await on_event(event)
                    event_type = str(event.get("type", "") or "").strip()
                    if event_type == "done":
                        final_payload = dict(event)
                        final_payload["ok"] = True
                        return final_payload
                    if event_type == "error":
                        return {
                            "ok": False,
                            "stage": str(event.get("stage", "") or "").strip(),
                            "error": str(
                                event.get("error", "") or "OpenClaw chat stream failed"
                            ).strip(),
                            "reply": str(event.get("reply", "") or "").strip(),
                            "session_id": str(event.get("session_id", "") or "").strip(),
                            "session_key": str(event.get("session_key", "") or "").strip(),
                            "timing": dict(event.get("timing") or {}),
                        }
            except Exception:
                await self._stop_locked()
                raise


async def _send_ws_error(
    websocket: WebSocket,
    *,
    stage: str,
    message: str,
    code: str = "ERROR",
) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "stage": str(stage or "").strip() or "unknown",
            "code": str(code or "").strip() or "ERROR",
            "message": str(message or "").strip() or "Unknown error",
        }
    )


def _build_tts_summary_from_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "requested": True,
        "ok": str(event.get("type", "")).strip() == "done",
        "session_id": str(event.get("session_id", "") or "").strip(),
        "spoken_text": str(event.get("spoken_text", "") or "").strip(),
        "sample_rate": int(event.get("sample_rate", 0) or 0),
        "audio_chunks": int(event.get("audio_chunks", 0) or 0),
        "audio_bytes": int(event.get("audio_bytes", 0) or 0),
        "finish_reason": str(event.get("finish_reason", "") or "").strip(),
        "elapsed_sec": float(event.get("elapsed_sec", 0.0) or 0.0),
        "error": "",
    }


async def _relay_remote_tts_stream(
    websocket: WebSocket,
    *,
    command: list[str],
    stdin_payload: dict[str, Any],
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[5]),
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    payload_bytes = (json.dumps(stdin_payload, ensure_ascii=False) + "\n").encode("utf-8")
    proc.stdin.write(payload_bytes)
    await proc.stdin.drain()
    proc.stdin.close()

    last_summary: dict[str, Any] = {
        "requested": True,
        "ok": False,
        "error": "Remote TTS stream did not finish",
    }

    while True:
        raw_line = await proc.stdout.readline()
        if not raw_line:
            break
        text_line = raw_line.decode("utf-8", errors="ignore").strip()
        if not text_line:
            continue
        try:
            event = json.loads(text_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        await websocket.send_json(event)
        event_type = str(event.get("type", "")).strip()
        if event_type == "done":
            last_summary = _build_tts_summary_from_event(event)
        elif event_type == "error":
            last_summary = {
                "requested": True,
                "ok": False,
                "error": str(event.get("message", "") or "Remote TTS stream failed").strip(),
            }

    return_code = await proc.wait()
    if return_code != 0 and not bool(last_summary.get("ok")) and not last_summary.get("error"):
        last_summary = {
            "requested": True,
            "ok": False,
            "error": f"Remote TTS bridge exited with code {return_code}",
        }
        await websocket.send_json(
            {
                "type": "error",
                "stage": "tts",
                "code": "TTS_BRIDGE_EXITED",
                "message": str(last_summary["error"]),
            }
        )
    return last_summary


async def _relay_openclaw_chat_stream(
    websocket: WebSocket,
    *,
    bridge: OpenClawChatStreamBridge,
    command: list[str],
    stdin_payload: dict[str, Any],
) -> dict[str, Any]:
    async def _forward_event(event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "")).strip()
        if event_type == "accepted":
            await websocket.send_json(
                {
                    "type": "agent_accepted",
                    "data": {
                        "run_id": str(event.get("run_id", "") or "").strip(),
                        "session_key": str(event.get("session_key", "") or "").strip(),
                        "status": str(event.get("status", "") or "").strip(),
                    },
                }
            )
            return

        if event_type == "delta":
            await websocket.send_json(
                {
                    "type": "agent_delta",
                    "data": {
                        "run_id": str(event.get("run_id", "") or "").strip(),
                        "session_key": str(event.get("session_key", "") or "").strip(),
                        "delta": str(event.get("delta", "") or "").strip(),
                        "reply": str(event.get("reply", "") or "").strip(),
                        "elapsed_ms": float(event.get("elapsed_ms", 0.0) or 0.0),
                    },
                }
            )
            return

    return await bridge.relay(
        command=command,
        stdin_payload=stdin_payload,
        on_event=_forward_event,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = QuickControlService()
    app.state.quick_control_service = service
    app.state.openclaw_chat_stream_bridge = OpenClawChatStreamBridge()
    try:
        yield
    finally:
        await app.state.openclaw_chat_stream_bridge.close()
        service.close()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def create_app() -> FastAPI:
    app = FastAPI(title="MomoAgent Quick Control API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(QuickControlError)
    async def quick_control_error_handler(_request: Request, exc: QuickControlError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                },
            },
        )

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        session_data, agent_data = await asyncio.gather(
            asyncio.to_thread(service.session_status),
            asyncio.to_thread(service.agent_status),
        )
        return _ok(
            {
                "status": "ok",
                "service": "momoagent-quick-control-api",
                "session": session_data,
                "agent": agent_data,
            }
        )

    @app.get("/agent-test")
    async def agent_test_page() -> FileResponse:
        return FileResponse(AGENT_STREAM_TEST_PAGE)

    @app.get("/api/v1/session/status")
    async def session_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.session_status())

    @app.post("/api/v1/session/connect")
    async def connect(payload: ConnectRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.connect(prefer_real=payload.prefer_real, allow_sim_fallback=payload.allow_sim_fallback))

    @app.post("/api/v1/session/disconnect")
    async def disconnect(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.disconnect())

    @app.get("/api/v1/robot/state")
    async def robot_state(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.robot_state_payload())

    @app.post("/api/v1/motion/joint-step")
    async def motion_joint_step(payload: JointStepRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.joint_step(
                joint_index=payload.joint_index,
                delta_deg=payload.delta_deg,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/cartesian-jog")
    async def motion_cartesian_jog(payload: CartesianJogRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.cartesian_jog(
                axis=payload.axis,
                coord_frame=payload.coord_frame,
                jog_mode=payload.jog_mode,
                step_dist_mm=payload.step_dist_mm,
                step_angle_deg=payload.step_angle_deg,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/home")
    async def motion_home(payload: HomeRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.home(source=payload.source, speed_percent=payload.speed_percent))

    @app.post("/api/v1/motion/stop")
    async def motion_stop(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.stop())

    @app.get("/api/v1/follow/status")
    async def follow_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.follow_status())

    @app.post("/api/v1/follow/start")
    async def follow_start(payload: FollowStartRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.follow_start(
                target_kind=payload.target_kind,
                latest_url=payload.latest_url,
                poll_interval=payload.poll_interval,
                http_timeout=payload.http_timeout,
                move_duration=payload.move_duration,
                pan_joint=payload.pan_joint,
                tilt_joint=payload.tilt_joint,
                pan_sign=payload.pan_sign,
                tilt_sign=payload.tilt_sign,
                pan_gain=payload.pan_gain,
                tilt_gain=payload.tilt_gain,
                pan_dead_zone=payload.pan_dead_zone,
                tilt_dead_zone=payload.tilt_dead_zone,
                pan_resume_zone=payload.pan_resume_zone,
                tilt_resume_zone=payload.tilt_resume_zone,
                min_pan_step=payload.min_pan_step,
                min_tilt_step=payload.min_tilt_step,
                pan_min_step_zone=payload.pan_min_step_zone,
                tilt_min_step_zone=payload.tilt_min_step_zone,
                max_pan_step=payload.max_pan_step,
                max_tilt_step=payload.max_tilt_step,
                command_mode=payload.command_mode,
                limit_margin_raw=payload.limit_margin_raw,
                stiction_eps_deg=payload.stiction_eps_deg,
                stiction_frames=payload.stiction_frames,
                pan_breakaway_step=payload.pan_breakaway_step,
                pan_breakaway_step_pos=payload.pan_breakaway_step_pos,
                pan_breakaway_step_neg=payload.pan_breakaway_step_neg,
                pan_negative_scale=payload.pan_negative_scale,
                tilt_breakaway_step=payload.tilt_breakaway_step,
                enable_idle_scan_fallback=payload.enable_idle_scan_fallback,
                lost_target_hold_sec=payload.lost_target_hold_sec,
                idle_scan_speed_percent=payload.idle_scan_speed_percent,
                idle_scan_pan_range_deg=payload.idle_scan_pan_range_deg,
                idle_scan_tilt_range_deg=payload.idle_scan_tilt_range_deg,
                idle_scan_move_duration_min_sec=payload.idle_scan_move_duration_min_sec,
                idle_scan_move_duration_max_sec=payload.idle_scan_move_duration_max_sec,
                idle_scan_dwell_sec_min=payload.idle_scan_dwell_sec_min,
                idle_scan_dwell_sec_max=payload.idle_scan_dwell_sec_max,
            )
        )

    @app.post("/api/v1/follow/stop")
    async def follow_stop(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.follow_stop())

    @app.get("/api/v1/idle-scan/status")
    async def idle_scan_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.idle_scan_status())

    @app.post("/api/v1/idle-scan/start")
    async def idle_scan_start(payload: IdleScanStartRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.idle_scan_start(
                pan_joint=payload.pan_joint,
                tilt_joint=payload.tilt_joint,
                speed_percent=payload.speed_percent,
                pan_range_deg=payload.pan_range_deg,
                tilt_range_deg=payload.tilt_range_deg,
                move_duration_min_sec=payload.move_duration_min_sec,
                move_duration_max_sec=payload.move_duration_max_sec,
                dwell_sec_min=payload.dwell_sec_min,
                dwell_sec_max=payload.dwell_sec_max,
            )
        )

    @app.post("/api/v1/idle-scan/stop")
    async def idle_scan_stop(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.idle_scan_stop())

    @app.get("/api/v1/haiguitang/status")
    async def haiguitang_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.haiguitang_status())

    @app.get("/api/v1/scenes/haiguitang/config")
    async def haiguitang_scene_config(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.haiguitang_scene_config())

    @app.get("/api/v1/scenes/haiguitang/intro-video")
    async def haiguitang_intro_video(request: Request) -> FileResponse:
        del request
        intro_video_file = haiguitang_intro_video_file()
        if not intro_video_file.is_file():
            raise QuickControlError(
                "HAIGUITANG_INTRO_VIDEO_NOT_FOUND",
                f"HaiGuiTang intro video not found: {intro_video_file}",
                404,
            )
        return FileResponse(
            path=intro_video_file,
            media_type="video/mp4",
            filename=intro_video_file.name,
        )

    @app.post("/api/v1/haiguitang/start")
    async def haiguitang_start(payload: HaiGuiTangStartRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.haiguitang_start(
                pan_joint=payload.pan_joint,
                tilt_joint=payload.tilt_joint,
                speed_percent=payload.speed_percent,
                nod_amplitude_deg=payload.nod_amplitude_deg,
                nod_cycles=payload.nod_cycles,
                shake_amplitude_deg=payload.shake_amplitude_deg,
                shake_cycles=payload.shake_cycles,
                beat_duration_sec=payload.beat_duration_sec,
                beat_pause_sec=payload.beat_pause_sec,
                return_duration_sec=payload.return_duration_sec,
                settle_pause_sec=payload.settle_pause_sec,
                auto_center_after_action=payload.auto_center_after_action,
                capture_anchor_on_start=payload.capture_anchor_on_start,
            )
        )

    @app.post("/api/v1/haiguitang/act")
    async def haiguitang_act(payload: HaiGuiTangActionRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.haiguitang_act(action=payload.action))

    @app.post("/api/v1/haiguitang/stop")
    async def haiguitang_stop(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.haiguitang_stop())

    @app.get("/api/v1/agent/status")
    async def agent_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(await asyncio.to_thread(service.agent_status))

    @app.get("/api/v1/agent/last-turn")
    async def agent_last_turn(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(await asyncio.to_thread(service.agent_last_turn))

    @app.post("/api/v1/agent/warmup")
    async def agent_warmup(payload: AgentWarmupRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(await asyncio.to_thread(service.agent_warmup, prompt=payload.prompt))

    @app.post("/api/v1/agent/reset-session")
    async def agent_reset_session(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(await asyncio.to_thread(service.agent_reset_session))

    @app.post("/api/v1/agent/ask")
    async def agent_ask(payload: AgentAskRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(await asyncio.to_thread(service.agent_ask, message=payload.message))

    @app.websocket("/api/v1/ws/state")
    async def ws_state(websocket: WebSocket):
        await websocket.accept()
        service: QuickControlService = websocket.app.state.quick_control_service
        try:
            while True:
                await websocket.send_json({"type": "state", "data": service.robot_state_payload()})
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/agent")
    async def ws_agent(websocket: WebSocket):
        await websocket.accept()
        service: QuickControlService = websocket.app.state.quick_control_service
        try:
            while True:
                await websocket.send_json(
                    {
                        "type": "agent",
                        "data": await asyncio.to_thread(service.agent_status),
                    }
                )
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/agent-stream")
    async def ws_agent_stream(websocket: WebSocket):
        await websocket.accept()
        service: QuickControlService = websocket.app.state.quick_control_service
        initial_status = await asyncio.to_thread(service.agent_status)
        await websocket.send_json(
            {
                "type": "ready",
                "data": {
                    "status": initial_status,
                },
            }
        )
        try:
            while True:
                payload = await websocket.receive_json()
                if not isinstance(payload, dict):
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="INVALID_MESSAGE",
                        message="WebSocket message must be a JSON object",
                    )
                    continue

                op = str(payload.get("type", "") or "").strip().lower()
                if op == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if op == "status":
                    status_payload = await asyncio.to_thread(service.agent_status)
                    await websocket.send_json(
                        {
                            "type": "status",
                            "data": status_payload,
                        }
                    )
                    continue
                if op != "ask":
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="UNSUPPORTED_OP",
                        message=f"Unsupported WebSocket op: {op or '<empty>'}",
                    )
                    continue

                message = str(payload.get("message", "") or "").strip()
                with_tts = bool(payload.get("with_tts", False))
                if not message:
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="INVALID_ARGUMENT",
                        message="Agent prompt is empty",
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "turn_started",
                        "with_tts": with_tts,
                        "message": message,
                    }
                )

                try:
                    stream_spec = await asyncio.to_thread(
                        service.agent_build_stream_turn_spec,
                        kind="ask",
                        prompt=message,
                    )
                except QuickControlError as exc:
                    await _send_ws_error(
                        websocket,
                        stage="agent",
                        code=exc.code,
                        message=exc.message,
                    )
                    continue

                if bool(stream_spec.get("ok")):
                    try:
                        stream_result = await _relay_openclaw_chat_stream(
                            websocket,
                            bridge=websocket.app.state.openclaw_chat_stream_bridge,
                            command=list(stream_spec.get("command") or []),
                            stdin_payload=dict(stream_spec.get("stdin_payload") or {}),
                        )
                    except Exception as exc:  # noqa: BLE001
                        await asyncio.to_thread(
                            service.agent_fail_stream_turn,
                            kind="ask",
                            prompt=message,
                            error=str(exc),
                            bridge_session_key=str(
                                stream_spec.get("bridge_session_key", "") or ""
                            ).strip(),
                        )
                        await _send_ws_error(
                            websocket,
                            stage="agent",
                            code="AGENT_FAILED",
                            message=str(exc),
                        )
                        continue

                    stream_timing = dict(stream_result.get("timing") or {})
                    history_error = str(stream_result.get("history_error", "") or "").strip()
                    if history_error:
                        stream_timing["history_error"] = history_error

                    if not bool(stream_result.get("ok")):
                        error_message = str(
                            stream_result.get("error", "") or "OpenClaw chat stream failed"
                        ).strip()
                        await asyncio.to_thread(
                            service.agent_fail_stream_turn,
                            kind="ask",
                            prompt=message,
                            error=error_message,
                            session_id=str(stream_result.get("session_id", "") or "").strip(),
                            bridge_session_key=str(
                                stream_result.get("session_key", "")
                                or stream_spec.get("bridge_session_key", "")
                                or ""
                            ).strip(),
                            openclaw_elapsed_sec=float(stream_timing.get("total_ms", 0.0) or 0.0)
                            / 1000.0,
                            bridge_timing=stream_timing,
                        )
                        await _send_ws_error(
                            websocket,
                            stage="agent",
                            code="AGENT_FAILED",
                            message=error_message,
                        )
                        continue

                    result = await asyncio.to_thread(
                        service.agent_complete_stream_turn,
                        kind="ask",
                        prompt=message,
                        reply=str(stream_result.get("reply", "") or "").strip(),
                        session_id=str(stream_result.get("session_id", "") or "").strip(),
                        bridge_session_key=str(
                            stream_result.get("session_key", "")
                            or stream_spec.get("bridge_session_key", "")
                            or ""
                        ).strip(),
                        openclaw_elapsed_sec=float(stream_timing.get("total_ms", 0.0) or 0.0)
                        / 1000.0,
                        bridge_timing=stream_timing,
                    )
                else:
                    try:
                        result = await asyncio.to_thread(service.agent_ask, message=message)
                    except QuickControlError as exc:
                        await _send_ws_error(
                            websocket,
                            stage="agent",
                            code=exc.code,
                            message=exc.message,
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001
                        await _send_ws_error(
                            websocket,
                            stage="agent",
                            code="AGENT_FAILED",
                            message=str(exc),
                        )
                        continue

                turn = dict(result.get("turn") or {})
                reply = str(turn.get("reply", "") or "").strip()
                tts_summary: dict[str, Any] = {"requested": False}

                await websocket.send_json(
                    {
                        "type": "agent_reply",
                        "data": turn,
                    }
                )

                if with_tts:
                    tts_spec = service.agent_build_tts_stream_spec(text=reply)
                    tts_summary = dict(tts_spec.get("summary") or {"requested": True})
                    if not bool(tts_spec.get("ok")):
                        await websocket.send_json(
                            {
                                "type": "tts_unavailable",
                                "data": tts_summary,
                            }
                        )
                    else:
                        await websocket.send_json(
                            {
                                "type": "tts_started",
                                "data": tts_summary,
                            }
                        )
                        tts_summary = await _relay_remote_tts_stream(
                            websocket,
                            command=list(tts_spec.get("command") or []),
                            stdin_payload=dict(tts_spec.get("stdin_payload") or {}),
                        )

                service.agent_set_last_turn_tts_summary(summary=tts_summary)
                turn["tts"] = dict(tts_summary)
                await websocket.send_json(
                    {
                        "type": "turn_done",
                        "data": {
                            "turn": turn,
                            "status": await asyncio.to_thread(service.agent_status),
                        },
                    }
                )
        except WebSocketDisconnect:
            return

    return app


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="MomoAgent Quick Control API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "quick_control_api.app:create_app",
        host=str(args.host),
        port=int(args.port),
        reload=bool(args.reload),
        factory=True,
    )
