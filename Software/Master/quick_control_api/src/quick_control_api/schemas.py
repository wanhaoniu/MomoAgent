from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ConnectRequest(BaseModel):
    prefer_real: bool = True
    allow_sim_fallback: bool = True


class JointStepRequest(BaseModel):
    joint_index: int = Field(..., ge=0, le=5)
    delta_deg: float
    speed_percent: int = Field(50, ge=1, le=100)


class CartesianJogRequest(BaseModel):
    axis: Literal[
        "+X",
        "-X",
        "+Y",
        "-Y",
        "+Z",
        "-Z",
        "+RX",
        "-RX",
        "+RY",
        "-RY",
        "+RZ",
        "-RZ",
    ]
    coord_frame: Literal["base", "tool"] = "base"
    jog_mode: Literal["step", "continuous"] = "step"
    step_dist_mm: float = Field(5.0, ge=0.1, le=200.0)
    step_angle_deg: float = Field(5.0, ge=0.1, le=180.0)
    speed_percent: int = Field(50, ge=1, le=100)


class HomeRequest(BaseModel):
    source: Literal["home", "origin", "zero", "startup"] = "home"
    speed_percent: int = Field(50, ge=1, le=100)


class AgentAskRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


class AgentWarmupRequest(BaseModel):
    prompt: str = Field("请只回复“就绪”。", min_length=1, max_length=200)


class FollowStartRequest(BaseModel):
    target_kind: Literal["face", "person", "generic"] = "face"
    latest_url: str = "http://127.0.0.1:8000/latest"
    poll_interval: float = Field(0.08, ge=0.01, le=2.0)
    http_timeout: float = Field(1.0, ge=0.1, le=10.0)
    move_duration: float = Field(0.20, ge=0.01, le=5.0)
    pan_joint: str = "shoulder_pan"
    tilt_joint: str = "elbow_flex"
    pan_sign: float = Field(1.0, ge=-1.0, le=1.0)
    tilt_sign: float = Field(1.0, ge=-1.0, le=1.0)
    pan_gain: float = Field(5.6, ge=0.0, le=30.0)
    tilt_gain: float = Field(7.0, ge=0.0, le=30.0)
    pan_dead_zone: float = Field(0.035, ge=0.0, le=0.5)
    tilt_dead_zone: float = Field(0.035, ge=0.0, le=0.5)
    pan_resume_zone: float = Field(0.06, ge=0.0, le=0.5)
    tilt_resume_zone: float = Field(0.06, ge=0.0, le=0.5)
    min_pan_step: float = Field(0.6, ge=0.0, le=20.0)
    min_tilt_step: float = Field(1.0, ge=0.0, le=20.0)
    pan_min_step_zone: float = Field(0.09, ge=0.0, le=1.0)
    tilt_min_step_zone: float = Field(0.10, ge=0.0, le=1.0)
    max_pan_step: float = Field(1.4, ge=0.0, le=20.0)
    max_tilt_step: float = Field(1.6, ge=0.0, le=20.0)
    command_mode: Literal["stream", "settle"] = "stream"
    limit_margin_raw: int = Field(60, ge=0, le=2048)
    stiction_eps_deg: float = Field(0.15, ge=0.0, le=10.0)
    stiction_frames: int = Field(3, ge=1, le=30)
    pan_breakaway_step: float = Field(1.8, ge=0.0, le=20.0)
    pan_breakaway_step_pos: float | None = Field(default=None, ge=0.0, le=20.0)
    pan_breakaway_step_neg: float | None = Field(default=3.2, ge=0.0, le=20.0)
    pan_negative_scale: float = Field(1.45, ge=1.0, le=5.0)
    tilt_breakaway_step: float = Field(1.8, ge=0.0, le=20.0)


class IdleScanStartRequest(BaseModel):
    speed_percent: int = Field(25, ge=1, le=100)
    pan_range_deg: float = Field(10.0, ge=1.0, le=45.0)
    tilt_range_deg: float = Field(8.0, ge=1.0, le=30.0)
    move_duration_min_sec: float = Field(1.2, ge=0.2, le=10.0)
    move_duration_max_sec: float = Field(2.8, ge=0.2, le=10.0)
    dwell_sec_min: float = Field(0.8, ge=0.0, le=20.0)
    dwell_sec_max: float = Field(2.5, ge=0.0, le=20.0)
