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
