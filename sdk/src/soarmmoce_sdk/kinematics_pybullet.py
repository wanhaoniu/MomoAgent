from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

try:
    import pybullet as _pb

    PYBULLET_AVAILABLE = True
    PYBULLET_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised through runtime fallback
    _pb = None
    PYBULLET_AVAILABLE = False
    PYBULLET_IMPORT_ERROR = exc


def _normalized_name(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("-", "_")


def _wrap_angle_rad(value: float) -> float:
    return float((float(value) + math.pi) % (2.0 * math.pi) - math.pi)


def _angular_error_norm(target_rpy: np.ndarray, actual_rpy: np.ndarray) -> float:
    diff = np.asarray(
        [_wrap_angle_rad(float(target_rpy[idx]) - float(actual_rpy[idx])) for idx in range(3)],
        dtype=float,
    )
    return float(np.linalg.norm(diff))


def _matrix_to_rpy(rotation: np.ndarray) -> np.ndarray:
    rot = np.asarray(rotation, dtype=float).reshape(3, 3)
    sy = math.sqrt(float(rot[0, 0]) ** 2 + float(rot[1, 0]) ** 2)
    singular = sy < 1e-8

    if not singular:
        roll = math.atan2(float(rot[2, 1]), float(rot[2, 2]))
        pitch = math.atan2(-float(rot[2, 0]), sy)
        yaw = math.atan2(float(rot[1, 0]), float(rot[0, 0]))
    else:
        roll = math.atan2(-float(rot[1, 2]), float(rot[1, 1]))
        pitch = math.atan2(-float(rot[2, 0]), sy)
        yaw = 0.0
    return np.asarray([roll, pitch, yaw], dtype=float)


def _rpy_to_matrix(rpy: Sequence[float]) -> np.ndarray:
    if not PYBULLET_AVAILABLE or _pb is None:
        raise RuntimeError(f"PyBullet is unavailable: {PYBULLET_IMPORT_ERROR}")
    quat = _pb.getQuaternionFromEuler([float(rpy[0]), float(rpy[1]), float(rpy[2])])
    matrix = _pb.getMatrixFromQuaternion(quat)
    return np.asarray(matrix, dtype=float).reshape(3, 3)


@dataclass(frozen=True, slots=True)
class ForwardKinematicsResult:
    xyz: np.ndarray
    rpy: np.ndarray


@dataclass(frozen=True, slots=True)
class InverseKinematicsResult:
    q_user: np.ndarray
    xyz: np.ndarray
    rpy: np.ndarray
    pos_error_m: float
    rot_error_rad: float | None


class PybulletKinematicsModel:
    """Small URDF-backed FK/IK helper used by the SDK Cartesian layer."""

    def __init__(
        self,
        *,
        urdf_path: str | Path,
        sdk_joint_names: Sequence[str],
        joint_name_aliases: Mapping[str, str] | None = None,
        model_offsets_deg: Mapping[str, float] | None = None,
        target_frame: str = "wrist_roll",
    ) -> None:
        if not PYBULLET_AVAILABLE or _pb is None:
            raise RuntimeError(f"PyBullet is unavailable: {PYBULLET_IMPORT_ERROR}")

        self.urdf_path = Path(urdf_path).expanduser().resolve()
        if not self.urdf_path.exists():
            raise FileNotFoundError(f"URDF not found: {self.urdf_path}")

        self.sdk_joint_names = [str(name) for name in sdk_joint_names]
        self.joint_name_aliases = {
            str(joint_name): str(alias_name)
            for joint_name, alias_name in dict(joint_name_aliases or {}).items()
        }
        self.target_frame = str(target_frame or "").strip() or "wrist_roll"
        self.model_offsets_rad = np.asarray(
            [
                math.radians(float(dict(model_offsets_deg or {}).get(joint_name, 0.0)))
                for joint_name in self.sdk_joint_names
            ],
            dtype=float,
        )

        self._client_id = _pb.connect(_pb.DIRECT)
        self._robot_id = _pb.loadURDF(
            str(self.urdf_path),
            basePosition=[0.0, 0.0, 0.0],
            baseOrientation=[0.0, 0.0, 0.0, 1.0],
            useFixedBase=True,
            physicsClientId=self._client_id,
        )

        self._movable_joint_index_by_name: dict[str, int] = {}
        self._movable_joint_index_by_link_name: dict[str, int] = {}
        self._movable_joint_limits_by_name: dict[str, tuple[float, float]] = {}
        self._movable_joint_link_name_by_name: dict[str, str] = {}
        self._movable_joint_parent_indices: set[int] = set()

        for joint_index in range(_pb.getNumJoints(self._robot_id, physicsClientId=self._client_id)):
            info = _pb.getJointInfo(self._robot_id, joint_index, physicsClientId=self._client_id)
            joint_type = int(info[2])
            if joint_type not in (_pb.JOINT_REVOLUTE, _pb.JOINT_PRISMATIC):
                continue

            joint_name = info[1].decode("utf-8")
            link_name = info[12].decode("utf-8")
            lower = float(info[8])
            upper = float(info[9])
            if (not math.isfinite(lower)) or (not math.isfinite(upper)) or lower >= upper:
                lower, upper = -math.pi, math.pi

            self._movable_joint_index_by_name[joint_name] = int(joint_index)
            self._movable_joint_index_by_link_name[link_name] = int(joint_index)
            self._movable_joint_limits_by_name[joint_name] = (lower, upper)
            self._movable_joint_link_name_by_name[joint_name] = link_name

            parent_index = int(info[16])
            if parent_index >= 0:
                self._movable_joint_parent_indices.add(parent_index)

            _pb.resetJointState(
                self._robot_id,
                int(joint_index),
                0.0,
                physicsClientId=self._client_id,
            )

        if not self._movable_joint_index_by_name:
            raise RuntimeError(f"No movable joints found in URDF: {self.urdf_path}")

        self.ordered_joint_indices: list[int] = []
        self.ordered_joint_model_limits: list[tuple[float, float]] = []
        for joint_name in self.sdk_joint_names:
            urdf_joint_name = self._resolve_urdf_joint_name(joint_name)
            if urdf_joint_name not in self._movable_joint_index_by_name:
                raise KeyError(
                    f"URDF joint mapping not found for SDK joint '{joint_name}' "
                    f"(expected '{urdf_joint_name}') in {self.urdf_path}"
                )
            self.ordered_joint_indices.append(self._movable_joint_index_by_name[urdf_joint_name])
            self.ordered_joint_model_limits.append(self._movable_joint_limits_by_name[urdf_joint_name])

        self.ordered_joint_user_limits: list[tuple[float, float]] = []
        for idx, (lower, upper) in enumerate(self.ordered_joint_model_limits):
            offset = float(self.model_offsets_rad[idx]) if idx < len(self.model_offsets_rad) else 0.0
            self.ordered_joint_user_limits.append((float(lower - offset), float(upper - offset)))

        self.ee_link_index = self._resolve_end_effector_link_index(self.target_frame)
        if self.ee_link_index is None:
            raise RuntimeError(f"Failed to resolve end-effector frame '{self.target_frame}' from URDF {self.urdf_path}")

    def close(self) -> None:
        client_id = getattr(self, "_client_id", None)
        if client_id is None or not PYBULLET_AVAILABLE or _pb is None:
            return
        try:
            _pb.disconnect(physicsClientId=int(client_id))
        except Exception:
            pass
        self._client_id = None

    def forward(self, q_user: Sequence[float]) -> ForwardKinematicsResult:
        q_model = self._user_to_model_q(q_user)
        self._reset_joint_state_model(q_model)
        state = _pb.getLinkState(
            self._robot_id,
            int(self.ee_link_index),
            computeForwardKinematics=True,
            physicsClientId=self._client_id,
        )
        xyz = np.asarray(state[4], dtype=float)
        rpy = np.asarray(_pb.getEulerFromQuaternion(state[5]), dtype=float)
        return ForwardKinematicsResult(xyz=xyz, rpy=rpy)

    def inverse(
        self,
        *,
        target_xyz: Sequence[float],
        target_rpy: Sequence[float] | None,
        seed_q_user: Sequence[float],
        max_iters: int = 200,
        residual_threshold: float = 1e-5,
    ) -> InverseKinematicsResult:
        target_xyz_arr = np.asarray(target_xyz, dtype=float).reshape(3)
        target_rpy_arr = None if target_rpy is None else np.asarray(target_rpy, dtype=float).reshape(3)
        lower_limits = [float(lower) for lower, _ in self.ordered_joint_model_limits]
        upper_limits = [float(upper) for _, upper in self.ordered_joint_model_limits]
        joint_ranges = [max(1e-4, float(upper - lower)) for lower, upper in self.ordered_joint_model_limits]
        target_orientation = None
        if target_rpy_arr is not None:
            target_orientation = _pb.getQuaternionFromEuler(
                [float(target_rpy_arr[0]), float(target_rpy_arr[1]), float(target_rpy_arr[2])]
            )

        def _solve_once(seed_q_candidate: Sequence[float], threshold_value: float) -> InverseKinematicsResult:
            seed_model = self._user_to_model_q(seed_q_candidate)
            # PyBullet uses the model's current joint state as part of IK solving,
            # so reset explicitly to the requested seed before every attempt.
            self._reset_joint_state_model(seed_model)
            ik_kwargs = {
                "bodyUniqueId": self._robot_id,
                "endEffectorLinkIndex": int(self.ee_link_index),
                "targetPosition": [float(target_xyz_arr[0]), float(target_xyz_arr[1]), float(target_xyz_arr[2])],
                "lowerLimits": lower_limits,
                "upperLimits": upper_limits,
                "jointRanges": joint_ranges,
                "restPoses": seed_model.tolist(),
                "maxNumIterations": max(1, int(max_iters)),
                "residualThreshold": max(1e-9, float(threshold_value)),
                "physicsClientId": self._client_id,
            }
            if target_orientation is not None:
                ik_kwargs["targetOrientation"] = target_orientation
            q_full = _pb.calculateInverseKinematics(**ik_kwargs)

            q_values = list(q_full) if q_full is not None else []
            q_model = np.asarray(seed_model, dtype=float).copy()
            for ordered_idx, joint_index in enumerate(self.ordered_joint_indices):
                if joint_index < len(q_values):
                    raw_value = float(q_values[joint_index])
                elif ordered_idx < len(q_values):
                    raw_value = float(q_values[ordered_idx])
                else:
                    raw_value = float(q_model[ordered_idx])

                lower, upper = self.ordered_joint_model_limits[ordered_idx]
                q_model[ordered_idx] = float(np.clip(raw_value, float(lower), float(upper)))

            q_user = self._model_to_user_q(q_model)
            for idx, (lower, upper) in enumerate(self.ordered_joint_user_limits):
                q_user[idx] = float(np.clip(float(q_user[idx]), float(lower), float(upper)))

            fk = self.forward(q_user)
            pos_error_m = float(np.linalg.norm(np.asarray(fk.xyz, dtype=float) - target_xyz_arr))
            rot_error_rad = (
                _angular_error_norm(target_rpy_arr, np.asarray(fk.rpy, dtype=float))
                if target_rpy_arr is not None
                else None
            )
            return InverseKinematicsResult(
                q_user=np.asarray(q_user, dtype=float),
                xyz=np.asarray(fk.xyz, dtype=float),
                rpy=np.asarray(fk.rpy, dtype=float),
                pos_error_m=pos_error_m,
                rot_error_rad=rot_error_rad,
            )

        result = _solve_once(seed_q_user, float(residual_threshold))
        if target_rpy_arr is None:
            return result

        best = result
        best_key = (
            float("inf") if best.rot_error_rad is None else float(best.rot_error_rad),
            float(best.pos_error_m),
        )
        refine_seed = best.q_user.tolist()
        for _ in range(4):
            # Empirically the orientation-constrained solution often improves when
            # the previous IK result is fed back as the next seed.
            refined = _solve_once(refine_seed, float(residual_threshold))
            refined_key = (
                float("inf") if refined.rot_error_rad is None else float(refined.rot_error_rad),
                float(refined.pos_error_m),
            )
            if refined_key >= best_key:
                break
            best = refined
            best_key = refined_key
            refine_seed = refined.q_user.tolist()
            if best.rot_error_rad is not None and best.rot_error_rad <= 1e-3:
                break

        return best

    def compose_delta_target(
        self,
        *,
        current_xyz: Sequence[float],
        current_rpy: Sequence[float],
        delta_xyz: Sequence[float],
        delta_rpy: Sequence[float],
        frame: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        current_xyz_arr = np.asarray(current_xyz, dtype=float).reshape(3)
        current_rpy_arr = np.asarray(current_rpy, dtype=float).reshape(3)
        delta_xyz_arr = np.asarray(delta_xyz, dtype=float).reshape(3)
        delta_rpy_arr = np.asarray(delta_rpy, dtype=float).reshape(3)

        current_rot = _rpy_to_matrix(current_rpy_arr)
        delta_rot = _rpy_to_matrix(delta_rpy_arr)
        frame_norm = "tool" if str(frame or "").strip().lower() == "tool" else "base"

        if frame_norm == "tool":
            target_xyz = current_xyz_arr + current_rot @ delta_xyz_arr
            target_rot = current_rot @ delta_rot
        else:
            target_xyz = current_xyz_arr + delta_xyz_arr
            target_rot = delta_rot @ current_rot
        target_rpy = _matrix_to_rpy(target_rot)
        return np.asarray(target_xyz, dtype=float), np.asarray(target_rpy, dtype=float)

    def _resolve_urdf_joint_name(self, sdk_joint_name: str) -> str:
        alias_name = str(self.joint_name_aliases.get(sdk_joint_name, sdk_joint_name))
        if alias_name in self._movable_joint_index_by_name:
            return alias_name
        if sdk_joint_name in self._movable_joint_index_by_name:
            return sdk_joint_name

        alias_key = _normalized_name(alias_name)
        sdk_key = _normalized_name(sdk_joint_name)
        for candidate_name in self._movable_joint_index_by_name:
            candidate_key = _normalized_name(candidate_name)
            if candidate_key in {alias_key, sdk_key}:
                return candidate_name
        raise KeyError(f"Unable to resolve URDF joint name for SDK joint '{sdk_joint_name}'")

    def _resolve_end_effector_link_index(self, target_frame: str) -> int | None:
        target_text = str(target_frame or "").strip()
        candidate_names: list[str] = []
        if target_text:
            candidate_names.append(target_text)
        if target_text in self.joint_name_aliases:
            candidate_names.append(str(self.joint_name_aliases[target_text]))

        target_key = _normalized_name(target_text)
        for sdk_joint_name, alias_name in self.joint_name_aliases.items():
            if _normalized_name(alias_name) == target_key:
                candidate_names.append(alias_name)
                candidate_names.append(sdk_joint_name)

        for candidate_name in candidate_names:
            if candidate_name in self._movable_joint_index_by_name:
                return self._movable_joint_index_by_name[candidate_name]
            if candidate_name in self._movable_joint_index_by_link_name:
                return self._movable_joint_index_by_link_name[candidate_name]

        candidate_keys = {_normalized_name(name) for name in candidate_names if str(name).strip()}
        for joint_name, joint_index in self._movable_joint_index_by_name.items():
            if _normalized_name(joint_name) in candidate_keys:
                return joint_index
        for link_name, joint_index in self._movable_joint_index_by_link_name.items():
            if _normalized_name(link_name) in candidate_keys:
                return joint_index

        movable_indices = set(self._movable_joint_index_by_name.values())
        leaves = sorted(movable_indices - self._movable_joint_parent_indices)
        if leaves:
            return leaves[-1]
        if movable_indices:
            return sorted(movable_indices)[-1]
        return None

    def _user_to_model_q(self, q_user: Sequence[float]) -> np.ndarray:
        q_arr = np.asarray(q_user, dtype=float).reshape(-1)
        if q_arr.shape[0] != len(self.sdk_joint_names):
            raise ValueError(f"Expected {len(self.sdk_joint_names)} joint values, got {q_arr.shape[0]}")
        return np.asarray(q_arr + self.model_offsets_rad, dtype=float)

    def _model_to_user_q(self, q_model: Sequence[float]) -> np.ndarray:
        q_arr = np.asarray(q_model, dtype=float).reshape(-1)
        if q_arr.shape[0] != len(self.sdk_joint_names):
            raise ValueError(f"Expected {len(self.sdk_joint_names)} joint values, got {q_arr.shape[0]}")
        return np.asarray(q_arr - self.model_offsets_rad, dtype=float)

    def _reset_joint_state_model(self, q_model: Sequence[float]) -> None:
        q_arr = np.asarray(q_model, dtype=float).reshape(-1)
        if q_arr.shape[0] != len(self.ordered_joint_indices):
            raise ValueError(f"Expected {len(self.ordered_joint_indices)} model joint values, got {q_arr.shape[0]}")
        for idx, joint_index in enumerate(self.ordered_joint_indices):
            _pb.resetJointState(
                self._robot_id,
                int(joint_index),
                float(q_arr[idx]),
                physicsClientId=self._client_id,
            )


__all__ = [
    "ForwardKinematicsResult",
    "InverseKinematicsResult",
    "PYBULLET_AVAILABLE",
    "PYBULLET_IMPORT_ERROR",
    "PybulletKinematicsModel",
]
