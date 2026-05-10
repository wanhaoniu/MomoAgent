from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .aws_transcribe_realtime import AwsRealtimeSttSession, status_payload as aws_stt_status_payload
from .errors import MomoRobotError
from .haiguitang_agent import (
    build_haiguitang_agent_prompt,
    build_haiguitang_round_start_prompt,
    parse_haiguitang_agent_reply,
)
from .scene_config import haiguitang_intro_video_file, haiguitang_media_file
from .schemas import (
    AgentAskRequest,
    AgentWarmupRequest,
    CartesianJogRequest,
    ConnectRequest,
    FollowStartRequest,
    HaiGuiTangActionRequest,
    HaiGuiTangAgentTurnRequest,
    HaiGuiTangRoundStartRequest,
    HaiGuiTangSceneStateRequest,
    HaiGuiTangStartRequest,
    HomeRequest,
    IdleScanStartRequest,
    JointStepRequest,
    JointTargetRequest,
    ToolDispatchRequest,
)
from .service import MomoRobotService

AGENT_STREAM_TEST_PAGE = Path(__file__).resolve().parents[2] / "agent_stream_test.html"
WEB_ROOT = Path(__file__).resolve().parents[5] / "Software" / "Web"
WEB_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
HAIGUITANG_AGENT_MOTION_START_PAYLOAD = {
    "pan_joint": "shoulder_pan",
    "tilt_joint": "elbow_flex",
    "speed_percent": 30,
    "nod_amplitude_deg": 7.0,
    "nod_cycles": 2,
    "shake_amplitude_deg": 10.0,
    "shake_cycles": 2,
    "beat_duration_sec": 0.26,
    "beat_pause_sec": 0.08,
    "return_duration_sec": 0.24,
    "settle_pause_sec": 0.10,
    "auto_center_after_action": True,
    "capture_anchor_on_start": True,
}


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers.update(WEB_NO_CACHE_HEADERS)
        return response


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = MomoRobotService()
    app.state.robot_service = service
    try:
        yield
    finally:
        service.close()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


async def _run_agent_prompt(
    *,
    service: MomoRobotService,
    kind: str,
    prompt: str,
) -> dict[str, Any]:
    result = await asyncio.to_thread(service.agent_ask, message=prompt)
    turn = dict(result.get("turn") or {})
    turn["kind"] = str(kind or turn.get("kind") or "ask")
    return {
        "reply": str(turn.get("reply", "") or "").strip(),
        "session_id": str(turn.get("session_id", "") or "").strip(),
        "agent_session_key": str(turn.get("agent_session_key", "") or "").strip(),
        "agent_elapsed_sec": float(turn.get("agent_elapsed_sec", 0.0) or 0.0),
        "turn": turn,
    }


async def _ensure_haiguitang_motion_ready(service: MomoRobotService) -> dict[str, Any]:
    session_status = await asyncio.to_thread(service.session_status)
    if not bool(session_status.get("connected")):
        await asyncio.to_thread(
            service.connect,
            prefer_real=True,
            allow_sim_fallback=False,
        )

    haiguitang_status = await asyncio.to_thread(service.haiguitang_status)
    worker_payload = dict(haiguitang_status.get("haiguitang") or {})
    if bool(worker_payload.get("enabled")) and bool(worker_payload.get("running")):
        return haiguitang_status

    return await asyncio.to_thread(
        service.haiguitang_start,
        **HAIGUITANG_AGENT_MOTION_START_PAYLOAD,
    )


async def _apply_haiguitang_agent_directive(
    *,
    service: MomoRobotService,
    directive: dict[str, Any],
) -> dict[str, Any]:
    scene_subtitle_text = str(
        directive.get("spoken_text")
        or directive.get("subtitle_text")
        or ""
    ).strip()
    scene_state = await asyncio.to_thread(
        service.haiguitang_scene_present,
        clip=str(directive.get("clip", "default") or "default"),
        subtitle_text=scene_subtitle_text,
        video_url="",
        loop_playback=bool(directive.get("loop_playback", True)),
    )

    action = str(directive.get("action", "none") or "none").strip().lower()
    hardware_result: dict[str, Any] = {}
    control_error = ""
    if action in {"nod", "shake"}:
        try:
            await _ensure_haiguitang_motion_ready(service)
            hardware_result = await asyncio.to_thread(service.haiguitang_act, action=action)
        except MomoRobotError as exc:
            control_error = exc.message
        except Exception as exc:  # noqa: BLE001
            control_error = str(exc)

    return {
        "directive": dict(directive),
        "state": scene_state,
        "hardware": hardware_result,
        "control_error": control_error,
    }


async def _run_haiguitang_agent_turn(
    *,
    service: MomoRobotService,
    message: str,
) -> dict[str, Any]:
    user_message = str(message or "").strip()
    if not user_message:
        raise MomoRobotError("INVALID_ARGUMENT", "Agent prompt is empty", 400)

    upstream_turn = await _run_agent_prompt(
        service=service,
        kind="haiguitang",
        prompt=build_haiguitang_agent_prompt(user_message),
    )
    directive = parse_haiguitang_agent_reply(str(upstream_turn.get("reply", "") or ""))
    scene_result = await _apply_haiguitang_agent_directive(
        service=service,
        directive=directive.payload(),
    )
    turn = dict(upstream_turn.get("turn") or {})
    turn["kind"] = "haiguitang"
    turn["prompt"] = user_message
    turn["reply"] = directive.spoken_text
    turn["raw_reply"] = directive.raw_reply
    turn["parse_mode"] = directive.parse_mode
    return {
        "turn": turn,
        "scene": scene_result,
    }


async def _run_haiguitang_round_start(
    *,
    service: MomoRobotService,
    difficulty: str,
) -> dict[str, Any]:
    normalized_difficulty = str(difficulty or "").strip().lower() or "medium"
    prompt = build_haiguitang_round_start_prompt(normalized_difficulty)
    upstream_turn = await _run_agent_prompt(
        service=service,
        kind="haiguitang",
        prompt=prompt,
    )
    directive = parse_haiguitang_agent_reply(str(upstream_turn.get("reply", "") or ""))
    scene_result = await _apply_haiguitang_agent_directive(
        service=service,
        directive=directive.payload(),
    )
    turn = dict(upstream_turn.get("turn") or {})
    turn["kind"] = "haiguitang"
    turn["prompt"] = f"[start_round] difficulty={normalized_difficulty}"
    turn["reply"] = directive.spoken_text
    turn["raw_reply"] = directive.raw_reply
    turn["parse_mode"] = directive.parse_mode
    return {
        "difficulty": normalized_difficulty,
        "turn": turn,
        "scene": scene_result,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Momo Robot Service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if WEB_ROOT.is_dir():
        app.mount("/web", NoCacheStaticFiles(directory=str(WEB_ROOT), html=True), name="web")

    @app.exception_handler(MomoRobotError)
    async def momo_robot_error_handler(_request: Request, exc: MomoRobotError):
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

    @app.exception_handler(Exception)
    async def generic_error_handler(_request: Request, exc: Exception):
        message = str(exc).strip() or exc.__class__.__name__
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": message,
                },
            },
        )

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        session_data, agent_data = await asyncio.gather(
            asyncio.to_thread(service.session_status),
            asyncio.to_thread(service.agent_status),
        )
        return _ok(
            {
                "status": "ok",
                "service": "momo-robot-service",
                "session": session_data,
                "agent": agent_data,
            }
        )

    @app.get("/api/v1/stt/aws/status")
    async def aws_stt_status(_request: Request) -> dict[str, Any]:
        return _ok(aws_stt_status_payload())

    @app.get("/agent-test")
    async def agent_test_page() -> FileResponse:
        return FileResponse(AGENT_STREAM_TEST_PAGE)

    @app.get("/haiguitang")
    async def haiguitang_web_page() -> RedirectResponse:
        response = RedirectResponse(url="/web/", status_code=307)
        response.headers.update(WEB_NO_CACHE_HEADERS)
        return response

    @app.get("/api/v1/session/status")
    async def session_status(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.session_status())

    @app.post("/api/v1/session/connect")
    async def connect(payload: ConnectRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.connect(prefer_real=payload.prefer_real, allow_sim_fallback=payload.allow_sim_fallback))

    @app.post("/api/v1/session/disconnect")
    async def disconnect(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.disconnect())

    @app.get("/api/v1/robot/state")
    async def robot_state(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.robot_state_payload())

    @app.post("/api/v1/motion/joint-step")
    async def motion_joint_step(payload: JointStepRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(
            service.joint_step(
                joint_index=payload.joint_index,
                delta_deg=payload.delta_deg,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/joints-target")
    async def motion_joints_target(payload: JointTargetRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(
            service.joints_target(
                targets_deg=payload.targets_deg,
                multi_turn_targets_continuous_raw=payload.multi_turn_targets_continuous_raw,
                duration=payload.duration,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/cartesian-jog")
    async def motion_cartesian_jog(payload: CartesianJogRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
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
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.home(source=payload.source, speed_percent=payload.speed_percent))

    @app.post("/api/v1/motion/stop")
    async def motion_stop(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.stop())

    @app.post("/api/v1/motion/free-move")
    async def motion_free_move(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.free_move())

    @app.post("/api/v1/tools/dispatch")
    async def tool_dispatch(payload: ToolDispatchRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(
            await asyncio.to_thread(
                service.dispatch_tool,
                name=payload.name,
                arguments=payload.arguments,
                request_id=payload.request_id,
                timeout_sec=payload.timeout_sec,
            )
        )

    @app.get("/api/v1/follow/status")
    async def follow_status(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.follow_status())

    @app.post("/api/v1/follow/start")
    async def follow_start(payload: FollowStartRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
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
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.follow_stop())

    @app.get("/api/v1/idle-scan/status")
    async def idle_scan_status(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.idle_scan_status())

    @app.post("/api/v1/idle-scan/start")
    async def idle_scan_start(payload: IdleScanStartRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
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
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.idle_scan_stop())

    @app.get("/api/v1/haiguitang/status")
    async def haiguitang_status(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.haiguitang_status())

    @app.get("/api/v1/scenes/haiguitang/config")
    async def haiguitang_scene_config(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.haiguitang_scene_config())

    @app.get("/api/v1/scenes/haiguitang/state")
    async def haiguitang_scene_state(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.haiguitang_scene_state())

    @app.post("/api/v1/scenes/haiguitang/state")
    async def haiguitang_scene_present(
        payload: HaiGuiTangSceneStateRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(
            service.haiguitang_scene_present(
                clip=payload.clip,
                subtitle_text=payload.subtitle_text,
                video_url=payload.video_url,
                loop_playback=payload.loop_playback,
            )
        )

    @app.post("/api/v1/haiguitang/agent/turn")
    async def haiguitang_agent_turn(
        payload: HaiGuiTangAgentTurnRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        result = await _run_haiguitang_agent_turn(
            service=service,
            message=payload.message,
        )
        return _ok(result)

    @app.post("/api/v1/haiguitang/start-round")
    async def haiguitang_start_round(
        payload: HaiGuiTangRoundStartRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        result = await _run_haiguitang_round_start(
            service=service,
            difficulty=payload.difficulty,
        )
        return _ok(result)

    @app.get("/api/v1/scenes/haiguitang/intro-video")
    async def haiguitang_intro_video(request: Request) -> FileResponse:
        del request
        intro_video_file = haiguitang_intro_video_file()
        if intro_video_file is None or not intro_video_file.is_file():
            raise MomoRobotError(
                "HAIGUITANG_INTRO_VIDEO_NOT_FOUND",
                "HaiGuiTang intro video not found in runtime/media",
                404,
            )
        return FileResponse(
            path=intro_video_file,
            media_type="video/mp4",
            filename=intro_video_file.name,
            headers=WEB_NO_CACHE_HEADERS,
        )

    @app.get("/api/v1/scenes/haiguitang/media/{media_name}")
    async def haiguitang_media(request: Request, media_name: str) -> FileResponse:
        del request
        media_file = haiguitang_media_file(media_name)
        if media_file is None or not media_file.is_file():
            raise MomoRobotError(
                "HAIGUITANG_MEDIA_NOT_FOUND",
                f"HaiGuiTang media not found: {media_name}",
                404,
            )
        return FileResponse(
            path=media_file,
            media_type="video/mp4",
            filename=media_file.name,
            headers=WEB_NO_CACHE_HEADERS,
        )

    @app.post("/api/v1/haiguitang/start")
    async def haiguitang_start(payload: HaiGuiTangStartRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
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
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.haiguitang_act(action=payload.action))

    @app.post("/api/v1/haiguitang/stop")
    async def haiguitang_stop(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(service.haiguitang_stop())

    @app.get("/api/v1/agent/status")
    async def agent_status(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(await asyncio.to_thread(service.agent_status))

    @app.get("/api/v1/agent/last-turn")
    async def agent_last_turn(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(await asyncio.to_thread(service.agent_last_turn))

    @app.post("/api/v1/agent/warmup")
    async def agent_warmup(payload: AgentWarmupRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(await asyncio.to_thread(service.agent_warmup, prompt=payload.prompt))

    @app.post("/api/v1/agent/reset-session")
    async def agent_reset_session(request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(await asyncio.to_thread(service.agent_reset_session))

    @app.post("/api/v1/agent/ask")
    async def agent_ask(payload: AgentAskRequest, request: Request) -> dict[str, Any]:
        service: MomoRobotService = request.app.state.robot_service
        return _ok(await asyncio.to_thread(service.agent_ask, message=payload.message))

    @app.websocket("/api/v1/ws/state")
    async def ws_state(websocket: WebSocket):
        await websocket.accept()
        service: MomoRobotService = websocket.app.state.robot_service
        try:
            while True:
                await websocket.send_json({"type": "state", "data": service.robot_state_payload()})
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/agent")
    async def ws_agent(websocket: WebSocket):
        await websocket.accept()
        service: MomoRobotService = websocket.app.state.robot_service
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
        service: MomoRobotService = websocket.app.state.robot_service
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
                    result = await asyncio.to_thread(service.agent_ask, message=message)
                except MomoRobotError as exc:
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

    @app.websocket("/api/v1/ws/stt")
    async def ws_stt(websocket: WebSocket):
        await websocket.accept()
        session = AwsRealtimeSttSession()
        await session.send_ready(websocket)
        try:
            while True:
                message = await websocket.receive()
                message_type = str(message.get("type", "") or "").strip()
                if message_type == "websocket.disconnect":
                    break
                if message_type != "websocket.receive":
                    continue

                if message.get("bytes") is not None:
                    try:
                        await session.send_audio(bytes(message.get("bytes") or b""))
                    except Exception as exc:  # noqa: BLE001
                        await session.send_error(
                            websocket,
                            stage="audio",
                            code="AUDIO_SEND_FAILED",
                            message=str(exc),
                        )
                    continue

                text_payload = str(message.get("text", "") or "").strip()
                if not text_payload:
                    continue
                try:
                    payload = json.loads(text_payload)
                except json.JSONDecodeError:
                    await session.send_error(
                        websocket,
                        stage="request",
                        code="INVALID_JSON",
                        message="WebSocket message must be valid JSON text or binary audio",
                    )
                    continue
                if not isinstance(payload, dict):
                    await session.send_error(
                        websocket,
                        stage="request",
                        code="INVALID_MESSAGE",
                        message="WebSocket JSON message must be an object",
                    )
                    continue

                op = str(payload.get("type", "") or "").strip().lower()
                if op == "ping":
                    await session.send_json(websocket, {"type": "pong"})
                    continue
                if op == "status":
                    await session.send_json(
                        websocket,
                        {
                            "type": "status",
                            "data": {
                                "state": session.state,
                                "config": aws_stt_status_payload(session.config),
                            },
                        },
                    )
                    continue
                if op == "start":
                    try:
                        await session.start(websocket, payload)
                    except Exception as exc:  # noqa: BLE001
                        await session.send_error(
                            websocket,
                            stage="start",
                            code="STT_START_FAILED",
                            message=str(exc),
                        )
                    continue
                if op == "stop":
                    try:
                        await session.stop(websocket, reason="client_stop", notify=True)
                    except Exception as exc:  # noqa: BLE001
                        await session.send_error(
                            websocket,
                            stage="stop",
                            code="STT_STOP_FAILED",
                            message=str(exc),
                        )
                    continue

                await session.send_error(
                    websocket,
                    stage="request",
                    code="UNSUPPORTED_OP",
                    message=f"Unsupported WebSocket op: {op or '<empty>'}",
                )
        except WebSocketDisconnect:
            return
        finally:
            await session.close()

    @app.websocket("/api/v1/ws/tts")
    async def ws_tts(websocket: WebSocket):
        await websocket.accept()
        service: MomoRobotService = websocket.app.state.robot_service
        try:
            while True:
                message = await websocket.receive()
                message_type = str(message.get("type", "") or "").strip()
                if message_type == "websocket.disconnect":
                    break
                if message_type != "websocket.receive":
                    continue

                text_payload = str(message.get("text", "") or "").strip()
                if not text_payload:
                    continue

                try:
                    payload = json.loads(text_payload)
                except json.JSONDecodeError:
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="INVALID_JSON",
                        message="WebSocket message must be valid JSON text",
                    )
                    continue

                if not isinstance(payload, dict):
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="INVALID_MESSAGE",
                        message="WebSocket JSON message must be an object",
                    )
                    continue

                op = str(payload.get("type", "") or "").strip().lower()
                if op == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if op == "status":
                    await websocket.send_json(
                        {
                            "type": "status",
                            "data": service.agent_tts_status(),
                        }
                    )
                    continue
                if op != "speak":
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="UNSUPPORTED_OP",
                        message=f"Unsupported WebSocket op: {op or '<empty>'}",
                    )
                    continue

                text = str(payload.get("text", "") or "").strip()
                if not text:
                    await _send_ws_error(
                        websocket,
                        stage="request",
                        code="INVALID_ARGUMENT",
                        message="TTS input text is empty",
                    )
                    continue

                tts_spec = service.agent_build_tts_stream_spec(text=text)
                tts_summary = dict(tts_spec.get("summary") or {"requested": True})
                if not bool(tts_spec.get("ok")):
                    await websocket.send_json(
                        {
                            "type": "tts_unavailable",
                            "data": tts_summary,
                        }
                    )
                    continue

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
                await websocket.send_json(
                    {
                        "type": "tts_result",
                        "data": tts_summary,
                    }
                )
        except WebSocketDisconnect:
            return

    return app


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Momo Robot Service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "momo_robot_service.app:create_app",
        host=str(args.host),
        port=int(args.port),
        reload=bool(args.reload),
        factory=True,
    )
