from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
SDK_ROOT = SCRIPT_DIR.parent
REPO_ROOT = SDK_ROOT.parent
SDK_SRC = SDK_ROOT / "src"
DEFAULT_CONFIG_PATH = SDK_SRC / "soarmmoce_sdk" / "resources" / "configs" / "soarm_moce_serial.yaml"
DEFAULT_PORT_FALLBACK = "/dev/tty.usbmodem5B140317411"

if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))


@dataclass(frozen=True)
class RegisterSpec:
    key: str
    label_cn: str
    address: int
    length: int
    group: str
    access: str
    unit: str = ""
    sign_bit: int | None = None

    @property
    def address_hex(self) -> str:
        return f"0x{self.address:02X}"

    @property
    def read_only(self) -> bool:
        return self.access == "ro"


# STS3215 寄存器表，来源是仓库 docs/1.txt。
# key 尽量使用英文，label_cn 保留中文名，方便打印和查文档。
DOCUMENTED_REGISTERS: tuple[RegisterSpec, ...] = (
    RegisterSpec("Firmware_Major_Version", "固件主版本号", 0, 1, "version", "ro"),
    RegisterSpec("Firmware_Minor_Version", "固件次版本号", 1, 1, "version", "ro"),
    RegisterSpec("Endianness_Flag", "END", 2, 1, "version", "ro"),
    RegisterSpec("Servo_Major_Version", "舵机主版本号", 3, 1, "version", "ro"),
    RegisterSpec("Servo_Minor_Version", "舵机次版本号", 4, 1, "version", "ro"),
    RegisterSpec("ID", "舵机ID", 5, 1, "eprom", "rw", "id"),
    RegisterSpec("Baud_Rate", "波特率", 6, 1, "eprom", "rw"),
    RegisterSpec("Reserved_07", "预留地址", 7, 1, "eprom", "rw"),
    RegisterSpec("Response_Status_Level", "应答状态级别", 8, 1, "eprom", "rw"),
    RegisterSpec("Min_Position_Limit", "最小角度限制", 9, 2, "eprom", "rw", "0.087deg"),
    RegisterSpec("Max_Position_Limit", "最大角度限制", 11, 2, "eprom", "rw", "0.087deg"),
    RegisterSpec("Max_Temperature_Limit", "最高温度上限", 13, 1, "eprom", "rw", "C"),
    RegisterSpec("Max_Voltage_Limit", "最高输入电压", 14, 1, "eprom", "rw", "0.1V"),
    RegisterSpec("Min_Voltage_Limit", "最低输入电压", 15, 1, "eprom", "rw", "0.1V"),
    RegisterSpec("Max_Torque_Limit", "最大扭矩", 16, 2, "eprom", "rw", "0.1%"),
    RegisterSpec("Phase", "相位", 18, 1, "eprom", "rw"),
    RegisterSpec("Unloading_Condition", "卸载条件", 19, 1, "eprom", "rw"),
    RegisterSpec("LED_Alarm_Condition", "LED报警条件", 20, 1, "eprom", "rw"),
    RegisterSpec("Position_P_Coefficient", "位置环P比例系数", 21, 1, "eprom", "rw"),
    RegisterSpec("Position_D_Coefficient", "位置环D微分系数", 22, 1, "eprom", "rw"),
    RegisterSpec("Position_I_Coefficient", "位置环I积分系数", 23, 1, "eprom", "rw"),
    RegisterSpec("Minimum_Startup_Force", "最小启动力", 24, 1, "eprom", "rw", "0.1%"),
    RegisterSpec("Integral_Limit", "积分限制值", 25, 1, "eprom", "rw"),
    RegisterSpec("CW_Dead_Zone", "正向不灵敏区", 26, 1, "eprom", "rw", "0.087deg"),
    RegisterSpec("CCW_Dead_Zone", "负向不灵敏区", 27, 1, "eprom", "rw", "0.087deg"),
    RegisterSpec("Protection_Current", "保护电流", 28, 2, "eprom", "rw", "6.5mA"),
    RegisterSpec("Angular_Resolution", "角度分辨率", 30, 1, "eprom", "rw"),
    RegisterSpec("Position_Offset", "位置偏移", 31, 2, "eprom", "rw", "0.087deg"),
    RegisterSpec("Operating_Mode", "运行模式", 33, 1, "eprom", "rw"),
    RegisterSpec("Protective_Torque", "保持扭矩", 34, 1, "eprom", "rw", "1%"),
    RegisterSpec("Protection_Time", "保护时间", 35, 1, "eprom", "rw", "10ms"),
    RegisterSpec("Overload_Torque", "过载扭矩", 36, 1, "eprom", "rw", "1%"),
    RegisterSpec("Velocity_Loop_P_Coefficient", "速度闭环P比例系数", 37, 1, "eprom", "rw"),
    RegisterSpec("Over_Current_Protection_Time", "过流保护时间", 38, 1, "eprom", "rw", "10ms"),
    RegisterSpec("Velocity_Loop_I_Coefficient", "速度闭环I积分系数", 39, 1, "eprom", "rw"),
    RegisterSpec("Torque_Enable", "扭矩开关", 40, 1, "sram_control", "rw"),
    RegisterSpec("Acceleration", "加速度", 41, 1, "sram_control", "rw", "8.7deg/s^2"),
    RegisterSpec("Goal_Position", "目标位置", 42, 2, "sram_control", "rw", "0.087deg", sign_bit=15),
    RegisterSpec("PWM_Open_Loop_Speed", "PWM开环速度", 44, 2, "sram_control", "rw", "0.1%", sign_bit=10),
    RegisterSpec("Goal_Velocity", "运行速度", 46, 2, "sram_control", "rw", "0.732RPM/0.0146RPM", sign_bit=15),
    RegisterSpec("Torque_Limit", "转矩限制", 48, 2, "sram_control", "rw", "0.1%"),
    RegisterSpec("Lock", "锁标志", 55, 1, "sram_control", "rw"),
    RegisterSpec("Present_Position", "当前位置", 56, 2, "sram_feedback", "ro", "0.087deg", sign_bit=15),
    RegisterSpec("Present_Velocity", "当前速度", 58, 2, "sram_feedback", "ro", "0.732RPM/0.0146RPM", sign_bit=15),
    RegisterSpec("Present_Load", "当前负载", 60, 2, "sram_feedback", "ro", "0.1%", sign_bit=10),
    RegisterSpec("Present_Voltage", "当前电压", 62, 1, "sram_feedback", "ro", "0.1V"),
    RegisterSpec("Present_Temperature", "当前温度", 63, 1, "sram_feedback", "ro", "C"),
    RegisterSpec("Async_Write_Flag", "异步写标志", 64, 1, "sram_feedback", "ro"),
    RegisterSpec("Status", "舵机状态", 65, 1, "sram_feedback", "ro"),
    RegisterSpec("Moving", "移动标志", 66, 1, "sram_feedback", "ro"),
    RegisterSpec("Goal_Position_Echo", "目标位置反馈", 67, 2, "sram_feedback", "ro", "0.087deg", sign_bit=15),
    RegisterSpec("Present_Current", "当前电流", 69, 2, "sram_feedback", "ro", "6.5mA"),
    RegisterSpec("Moving_Velocity_Threshold", "移动速度阀值", 80, 1, "factory", "ro"),
    RegisterSpec("DTs", "DTs(ms)", 81, 1, "factory", "ro"),
    RegisterSpec("Velocity_Unit_Factor", "速度单位系数", 82, 1, "factory", "ro"),
    RegisterSpec("Minimum_Velocity_Limit", "最小速度限制", 83, 1, "factory", "ro", "0.732RPM"),
    RegisterSpec("Maximum_Velocity_Limit", "最大速度限制", 84, 1, "factory", "ro", "0.732RPM"),
    RegisterSpec("Acceleration_Limit", "加速度限制", 85, 1, "factory", "ro"),
    RegisterSpec("Acceleration_Multiplier", "加速度倍数", 86, 1, "factory", "ro"),
)

REGISTER_BY_KEY = {spec.key: spec for spec in DOCUMENTED_REGISTERS}
REGISTER_GROUPS = tuple(sorted({spec.group for spec in DOCUMENTED_REGISTERS}))
DEFAULT_STATUS_REGISTER_KEYS = (
    "ID",
    "Operating_Mode",
    "Phase",
    "Torque_Enable",
    "Goal_Position",
    "Present_Position",
    "Present_Voltage",
    "Present_Temperature",
    "Status",
    "Moving",
)


def load_default_port(config_path: str | Path | None = None) -> str:
    path = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return DEFAULT_PORT_FALLBACK
    transport = payload.get("transport", {}) if isinstance(payload, dict) else {}
    port = str(transport.get("port", "")).strip() if isinstance(transport, dict) else ""
    return port or DEFAULT_PORT_FALLBACK


DEFAULT_PORT = load_default_port()


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def make_controller(config_path: str | Path | None = None):
    from soarmmoce_sdk import SoArmMoceController, resolve_config

    return SoArmMoceController(resolve_config(config_path))


def to_plain_json(value: Any) -> Any:
    from soarmmoce_sdk import to_jsonable

    return to_jsonable(value)


def _payload_mapping(payload: Any, key: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get(key, {})
    else:
        value = getattr(payload, key, {})
    return dict(value) if isinstance(value, dict) else {}


def summarize_state(state: Any) -> dict[str, Any]:
    from soarmmoce_sdk import JOINTS

    payload = to_plain_json(state)
    joint_state = _payload_mapping(payload, "joint_state")
    raw_present = _payload_mapping(payload, "raw_present_position")
    relative_raw = _payload_mapping(payload, "relative_raw_position")
    startup_raw = _payload_mapping(payload, "startup_raw_position")
    motor_deg = _payload_mapping(payload, "motor_position_deg")
    output_deg = _payload_mapping(payload, "output_position_deg")

    joints: dict[str, dict[str, Any]] = {}
    for joint_name in JOINTS:
        joints[joint_name] = {
            "joint_deg": joint_state.get(joint_name),
            "motor_deg": motor_deg.get(joint_name),
            "output_deg": output_deg.get(joint_name),
            "raw_present": raw_present.get(joint_name),
            "relative_raw": relative_raw.get(joint_name),
            "startup_raw": startup_raw.get(joint_name),
        }

    return {
        "timestamp": payload.get("timestamp"),
        "joint_order": list(JOINTS),
        "joints": joints,
        "tcp_pose": payload.get("tcp_pose"),
        "gripper": payload.get("gripper_state"),
    }


def summarize_motion_result(result: Any) -> dict[str, Any]:
    payload = to_plain_json(result)
    summary = {
        key: payload.get(key)
        for key in (
            "action",
            "target_deg",
            "targets_deg",
            "goal_raw",
            "duration_sec",
            "duration_source",
            "speed_percent",
            "wait",
            "settled",
        )
        if key in payload
    }
    if "state" in payload:
        summary["state"] = summarize_state(payload["state"])
    return summary


def summarize_pose_result(result: Any) -> dict[str, Any]:
    payload = to_plain_json(result)
    summary = {
        key: payload.get(key)
        for key in (
            "action",
            "frame",
            "delta",
            "target_xyz_m",
            "target_rpy_rad",
            "composed_target_rpy_rad",
            "orientation_mode",
            "orientation_constraint",
            "duration_sec",
            "duration_source",
            "speed_percent",
        )
        if key in payload
    }
    if "ik" in payload:
        summary["ik"] = payload["ik"]
    if "goal_raw" in payload:
        summary["goal_raw"] = payload["goal_raw"]
    if "targets_deg" in payload:
        summary["targets_deg"] = payload["targets_deg"]
    if "state" in payload:
        summary["state"] = summarize_state(payload["state"])
    return summary


def summarize_gripper_state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    payload = to_plain_json(state)
    if not isinstance(payload, dict):
        return {"raw": payload}
    return {
        key: payload.get(key)
        for key in (
            "available",
            "open_ratio",
            "present_raw",
            "present_register_raw",
            "adjusted_raw",
            "goal_raw",
            "range_min",
            "range_max",
            "homing_offset",
        )
        if key in payload
    }


def require_joint_name(joint_name: str) -> str:
    from soarmmoce_sdk import JOINTS

    value = str(joint_name).strip()
    if value not in JOINTS:
        raise ValueError(f"未知关节名: {value!r}。可用关节: {', '.join(JOINTS)}")
    return value


def clamp_open_ratio(value: float) -> float:
    return float(min(1.0, max(0.0, float(value))))


def coerce_vector3(values: Any, *, name: str) -> list[float]:
    payload = list(values)
    if len(payload) != 3:
        raise ValueError(f"{name} 必须包含 3 个数值，当前长度是 {len(payload)}。")
    return [float(payload[0]), float(payload[1]), float(payload[2])]


def require_lerobot() -> tuple[Any, Any, Any]:
    errors: list[BaseException] = []
    bus_modules = ("lerobot.motors.feetech", "lerobot.motors.feetech.feetech")
    motor_modules = ("lerobot.motors", "lerobot.motors.motors_bus")

    bus_cls = None
    for module_name in bus_modules:
        try:
            module = importlib.import_module(module_name)
            bus_cls = getattr(module, "FeetechMotorsBus")
            break
        except BaseException as exc:
            errors.append(exc)

    motor_cls = None
    norm_mode_cls = None
    for module_name in motor_modules:
        try:
            module = importlib.import_module(module_name)
            motor_cls = getattr(module, "Motor")
            norm_mode_cls = getattr(module, "MotorNormMode")
            break
        except BaseException as exc:
            errors.append(exc)

    if bus_cls is None or motor_cls is None or norm_mode_cls is None:
        raise ModuleNotFoundError(
            "LeRobot Feetech support is not installed. 请先在当前 Python 环境安装 lerobot[feetech]。"
        ) from (errors[-1] if errors else None)
    return bus_cls, motor_cls, norm_mode_cls


def make_motor(motor_id: int, model: str):
    _, Motor, MotorNormMode = require_lerobot()
    try:
        return Motor(id=int(motor_id), model=str(model), norm_mode=MotorNormMode.DEGREES)
    except TypeError:
        return Motor(int(motor_id), str(model), MotorNormMode.DEGREES)


def make_bus(
    *,
    port: str,
    motors: dict[str, Any],
    baudrate: int = 1_000_000,
    protocol_version: int = 0,
):
    FeetechMotorsBus, _, _ = require_lerobot()
    try:
        bus = FeetechMotorsBus(port=str(port), motors=motors, protocol_version=int(protocol_version))
    except TypeError:
        bus = FeetechMotorsBus(port=str(port), motors=motors)

    connect = getattr(bus, "connect", None)
    if callable(connect):
        try:
            connect(handshake=False)
        except TypeError:
            connect()

    try:
        bus.default_baudrate = int(baudrate)
    except Exception:
        pass

    set_baudrate = getattr(bus, "set_baudrate", None)
    if callable(set_baudrate):
        set_baudrate(int(baudrate))
    return bus


def make_single_motor_bus(
    *,
    port: str,
    motor_id: int,
    model: str = "sts3215",
    baudrate: int = 1_000_000,
    protocol_version: int = 0,
    motor_name: str = "target",
):
    return make_bus(
        port=port,
        baudrate=baudrate,
        protocol_version=protocol_version,
        motors={str(motor_name): make_motor(int(motor_id), str(model))},
    )


def disconnect_bus(bus: Any, *, disable_torque: bool = False) -> None:
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect(disable_torque=bool(disable_torque))
        except TypeError:
            disconnect()


def ping_motor(bus: Any, motor: str | int) -> int | None:
    try:
        model_number = bus.ping(motor, num_retry=1, raise_on_error=False)
    except TypeError:
        model_number = bus.ping(motor, raise_on_error=False)
    return None if model_number is None else int(model_number)


def decode_sign_magnitude(value: int, sign_bit: int) -> int:
    sign_mask = 1 << int(sign_bit)
    magnitude_mask = sign_mask - 1
    magnitude = int(value) & magnitude_mask
    return -magnitude if int(value) & sign_mask else magnitude


def require_register(key: str) -> RegisterSpec:
    register_key = str(key).strip()
    if register_key not in REGISTER_BY_KEY:
        valid_keys = ", ".join(sorted(REGISTER_BY_KEY))
        raise ValueError(f"未知寄存器 key: {register_key!r}。可用 key: {valid_keys}")
    return REGISTER_BY_KEY[register_key]


def select_registers(
    *,
    keys: list[str] | tuple[str, ...] | None = None,
    group: str | None = None,
    read_all: bool = False,
) -> list[RegisterSpec]:
    if read_all:
        return list(DOCUMENTED_REGISTERS)

    selected_keys: list[str] = []
    if group:
        group_name = str(group).strip()
        if group_name not in REGISTER_GROUPS:
            raise ValueError(f"未知寄存器分组: {group_name!r}。可用分组: {', '.join(REGISTER_GROUPS)}")
        selected_keys.extend(spec.key for spec in DOCUMENTED_REGISTERS if spec.group == group_name)
    if keys:
        selected_keys.extend(str(key).strip() for key in keys if str(key).strip())
    if not selected_keys:
        selected_keys = list(DEFAULT_STATUS_REGISTER_KEYS)

    seen: set[str] = set()
    selected: list[RegisterSpec] = []
    for key in selected_keys:
        spec = require_register(key)
        if spec.key in seen:
            continue
        selected.append(spec)
        seen.add(spec.key)
    return selected


def _comm_success(bus: Any, comm: int) -> bool:
    checker = getattr(bus, "_is_comm_success", None)
    if callable(checker):
        return bool(checker(comm))
    return int(comm) == 0


def _packet_error(bus: Any, error: int) -> bool:
    checker = getattr(bus, "_is_error", None)
    if callable(checker):
        return bool(checker(error))
    return int(error) != 0


def _comm_error_text(bus: Any, comm: int) -> str:
    packet_handler = getattr(bus, "packet_handler", None)
    formatter = getattr(packet_handler, "getTxRxResult", None)
    if callable(formatter):
        return str(formatter(comm))
    return f"comm={comm}"


def _packet_error_text(bus: Any, error: int) -> str:
    packet_handler = getattr(bus, "packet_handler", None)
    formatter = getattr(packet_handler, "getRxPacketError", None)
    if callable(formatter):
        return str(formatter(error))
    return f"error={error}"


def register_metadata(spec: RegisterSpec) -> dict[str, object]:
    return {
        "key": spec.key,
        "label_cn": spec.label_cn,
        "address_dec": int(spec.address),
        "address_hex": spec.address_hex,
        "length": int(spec.length),
        "group": spec.group,
        "access": spec.access,
        "unit": spec.unit,
    }


def read_register_raw(bus: Any, motor_id: int, spec: RegisterSpec, *, num_retry: int = 1) -> dict[str, object]:
    try:
        value, comm, error = bus._read(  # noqa: SLF001 - 示例脚本需要按地址直接读寄存器。
            int(spec.address),
            int(spec.length),
            int(motor_id),
            num_retry=int(num_retry),
            raise_on_error=False,
            err_msg=f"read {spec.key} id={motor_id}",
        )
    except TypeError:
        value, comm, error = bus._read(  # noqa: SLF001 - 兼容旧版 LeRobot 参数。
            int(spec.address),
            int(spec.length),
            int(motor_id),
            raise_on_error=False,
        )
    payload = register_metadata(spec)
    payload["motor_id"] = int(motor_id)
    if not _comm_success(bus, comm):
        payload["ok"] = False
        payload["comm_error"] = _comm_error_text(bus, comm)
        return payload
    if _packet_error(bus, error):
        payload["ok"] = False
        payload["packet_error"] = _packet_error_text(bus, error)
        return payload

    payload["ok"] = True
    payload["raw"] = int(value)
    if spec.sign_bit is not None:
        payload["decoded"] = int(decode_sign_magnitude(int(value), int(spec.sign_bit)))
    return payload


def write_register_raw(
    bus: Any,
    motor_id: int,
    spec: RegisterSpec,
    value: int,
    *,
    num_retry: int = 1,
) -> dict[str, object]:
    if spec.read_only:
        raise ValueError(f"{spec.key} / {spec.label_cn} 是只读寄存器，不能写入。")
    try:
        comm, error = bus._write(  # noqa: SLF001 - 示例脚本需要按地址直接写寄存器。
            int(spec.address),
            int(spec.length),
            int(motor_id),
            int(value),
            num_retry=int(num_retry),
            raise_on_error=False,
            err_msg=f"write {spec.key} id={motor_id} value={value}",
        )
    except TypeError:
        comm, error = bus._write(  # noqa: SLF001 - 兼容旧版 LeRobot 参数。
            int(spec.address),
            int(spec.length),
            int(motor_id),
            int(value),
            raise_on_error=False,
        )
    payload = register_metadata(spec)
    payload["motor_id"] = int(motor_id)
    payload["write_value"] = int(value)
    if not _comm_success(bus, comm):
        payload["ok"] = False
        payload["comm_error"] = _comm_error_text(bus, comm)
        return payload
    if _packet_error(bus, error):
        payload["ok"] = False
        payload["packet_error"] = _packet_error_text(bus, error)
        return payload
    payload["ok"] = True
    return payload


def ensure_ok(payload: dict[str, object], *, action: str) -> None:
    if bool(payload.get("ok", False)):
        return
    detail = payload.get("comm_error") or payload.get("packet_error") or "unknown error"
    raise ConnectionError(f"{action} failed: {detail}")
