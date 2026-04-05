from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manual smoke-test CLI for the rebuilt SoArmMoce SDK. "
            "This is meant for interactive hardware verification, not pytest."
        )
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional config yaml path. Defaults to SOARMMOCE_CONFIG or the SDK default config.",
    )
    parser.add_argument(
        "--release-torque-on-exit",
        action="store_true",
        help="Disable torque when closing the controller. Default is to keep torque locked.",
    )
    parser.add_argument(
        "--print-state-after",
        action="store_true",
        help="Read and print the latest state after the command finishes.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("state", help="Read current robot state.")
    subparsers.add_parser("meta", help="Read controller metadata.")

    joint_target_parser = subparsers.add_parser("joint-target", help="Move one joint to an absolute target angle.")
    joint_target_parser.add_argument("--joint", required=True, choices=JOINTS)
    joint_target_parser.add_argument("--target-deg", required=True, type=float)
    joint_target_parser.add_argument("--duration", type=float, default=1.5)
    joint_target_parser.add_argument("--timeout", type=float, default=None)
    joint_target_parser.add_argument("--no-wait", action="store_true")
    joint_target_parser.add_argument("--trace", action="store_true")

    joint_delta_parser = subparsers.add_parser("joint-delta", help="Move one joint by a relative angle increment.")
    joint_delta_parser.add_argument("--joint", required=True, choices=JOINTS)
    joint_delta_parser.add_argument("--delta-deg", required=True, type=float)
    joint_delta_parser.add_argument("--duration", type=float, default=1.5)
    joint_delta_parser.add_argument("--timeout", type=float, default=None)
    joint_delta_parser.add_argument("--no-wait", action="store_true")
    joint_delta_parser.add_argument("--trace", action="store_true")

    joints_parser = subparsers.add_parser(
        "joints-target",
        help="Move multiple joints at once. Repeat --set joint_name=value_deg.",
    )
    joints_parser.add_argument(
        "--set",
        dest="targets",
        action="append",
        default=[],
        metavar="JOINT=DEG",
        help="Example: --set shoulder_pan=10 --set wrist_flex=-15",
    )
    joints_parser.add_argument("--duration", type=float, default=2.0)
    joints_parser.add_argument("--timeout", type=float, default=None)
    joints_parser.add_argument("--no-wait", action="store_true")
    joints_parser.add_argument("--trace", action="store_true")

    home_parser = subparsers.add_parser("home", help="Move all joints to the runtime zero reference.")
    home_parser.add_argument("--duration", type=float, default=2.0)
    home_parser.add_argument("--timeout", type=float, default=None)
    home_parser.add_argument("--no-wait", action="store_true")

    stop_parser = subparsers.add_parser("stop", help="Hold current pose by rewriting current raw positions as goals.")
    stop_parser.add_argument("--print-hold-state", action="store_true")

    gripper_parser = subparsers.add_parser("gripper", help="Set gripper open ratio in [0.0, 1.0].")
    gripper_parser.add_argument("--open-ratio", required=True, type=float)
    gripper_parser.add_argument("--duration", type=float, default=1.0)
    gripper_parser.add_argument("--timeout", type=float, default=None)
    gripper_parser.add_argument("--no-wait", action="store_true")

    open_parser = subparsers.add_parser("open-gripper", help="Fully open the gripper.")
    open_parser.add_argument("--duration", type=float, default=1.0)
    open_parser.add_argument("--timeout", type=float, default=None)
    open_parser.add_argument("--no-wait", action="store_true")

    close_parser = subparsers.add_parser("close-gripper", help="Fully close the gripper.")
    close_parser.add_argument("--duration", type=float, default=1.0)
    close_parser.add_argument("--timeout", type=float, default=None)
    close_parser.add_argument("--no-wait", action="store_true")

    pose_parser = subparsers.add_parser("move-pose", help="Move TCP to an absolute base-frame target pose.")
    pose_parser.add_argument("--x", required=True, type=float)
    pose_parser.add_argument("--y", required=True, type=float)
    pose_parser.add_argument("--z", required=True, type=float)
    pose_parser.add_argument("--roll", type=float, default=None, help="Optional target roll in radians")
    pose_parser.add_argument("--pitch", type=float, default=None, help="Optional target pitch in radians")
    pose_parser.add_argument("--yaw", type=float, default=None, help="Optional target yaw in radians")
    pose_parser.add_argument("--duration", type=float, default=2.0)
    pose_parser.add_argument("--timeout", type=float, default=None)
    pose_parser.add_argument("--no-wait", action="store_true")
    pose_parser.add_argument("--trace", action="store_true")

    delta_parser = subparsers.add_parser(
        "move-delta",
        help="Call move_delta directly with optional orientation increments in radians.",
    )
    delta_parser.add_argument("--dx", type=float, default=0.0)
    delta_parser.add_argument("--dy", type=float, default=0.0)
    delta_parser.add_argument("--dz", type=float, default=0.0)
    delta_parser.add_argument("--drx", type=float, default=0.0)
    delta_parser.add_argument("--dry", type=float, default=0.0)
    delta_parser.add_argument("--drz", type=float, default=0.0)
    delta_parser.add_argument("--frame", type=str, default="base", choices=("base", "tool"))
    delta_parser.add_argument("--duration", type=float, default=1.0)
    delta_parser.add_argument("--timeout", type=float, default=None)
    delta_parser.add_argument("--no-wait", action="store_true")

    subparsers.add_parser(
        "shell",
        help="Keep one controller session alive and type commands interactively. Use this to verify home() correctly.",
    )

    return parser


def _parse_joint_targets(raw_targets: list[str]) -> dict[str, float]:
    if not raw_targets:
        raise ValueError("joints-target requires at least one --set joint=value")

    parsed: dict[str, float] = {}
    for item in raw_targets:
        text = str(item or "").strip()
        if "=" not in text:
            raise ValueError(f"Invalid --set value '{text}', expected JOINT=DEG")
        joint_name, target_text = text.split("=", 1)
        joint_name = joint_name.strip()
        if joint_name not in JOINTS:
            raise ValueError(f"Unknown joint in --set: {joint_name}")
        parsed[joint_name] = float(target_text.strip())
    return parsed


def _print_json(title: str, payload: Any) -> None:
    print(f"[{title}]")
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _state_summary(state: Any) -> dict[str, Any]:
    state_payload = to_jsonable(state)
    joint_state = dict(state_payload.get("joint_state", {}))
    gripper_state = dict(state_payload.get("gripper_state", {}))
    tcp_pose = dict(state_payload.get("tcp_pose", {}))
    return {
        "joint_deg": {joint: joint_state.get(joint) for joint in JOINTS},
        "tcp_xyz_m": tcp_pose.get("xyz", [0.0, 0.0, 0.0]),
        "tcp_rpy_rad": tcp_pose.get("rpy", [0.0, 0.0, 0.0]),
        "relative_raw": state_payload.get("relative_raw_position", {}),
        "raw_present": state_payload.get("raw_present_position", {}),
        "gripper": {
            "available": gripper_state.get("available", False),
            "open_ratio": gripper_state.get("open_ratio"),
            "present_raw": gripper_state.get("present_raw"),
            "adjusted_raw": gripper_state.get("adjusted_raw"),
        },
        "timestamp": state_payload.get("timestamp"),
    }


def _run_command(controller: SoArmMoceController, args: argparse.Namespace) -> Any:
    wait = not bool(getattr(args, "no_wait", False))

    if args.command == "state":
        return controller.get_state()

    if args.command == "meta":
        return controller.meta()

    if args.command == "joint-target":
        return controller.move_joint(
            joint=args.joint,
            target_deg=float(args.target_deg),
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
            trace=bool(args.trace),
        )

    if args.command == "joint-delta":
        return controller.move_joint(
            joint=args.joint,
            delta_deg=float(args.delta_deg),
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
            trace=bool(args.trace),
        )

    if args.command == "joints-target":
        return controller.move_joints(
            _parse_joint_targets(list(args.targets)),
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
            trace=bool(args.trace),
        )

    if args.command == "home":
        return controller.home(
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
        )

    if args.command == "stop":
        result = controller.stop()
        if bool(args.print_hold_state):
            result = {
                "stop_result": to_jsonable(result),
                "hold_state": controller.capture_hold_state(),
            }
        return result

    if args.command == "gripper":
        return controller.set_gripper(
            open_ratio=float(args.open_ratio),
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
        )

    if args.command == "open-gripper":
        return controller.open_gripper(
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
        )

    if args.command == "close-gripper":
        return controller.close_gripper(
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
        )

    if args.command == "move-pose":
        target_rpy = None
        if args.roll is not None or args.pitch is not None or args.yaw is not None:
            if args.roll is None or args.pitch is None or args.yaw is None:
                raise ValueError("--roll, --pitch, and --yaw must be provided together")
            target_rpy = [float(args.roll), float(args.pitch), float(args.yaw)]
        return controller.move_pose(
            xyz=[float(args.x), float(args.y), float(args.z)],
            rpy=target_rpy,
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
            trace=bool(args.trace),
        )

    if args.command == "move-delta":
        return controller.move_delta(
            dx=float(args.dx),
            dy=float(args.dy),
            dz=float(args.dz),
            drx=float(args.drx),
            dry=float(args.dry),
            drz=float(args.drz),
            frame=str(args.frame),
            duration=float(args.duration),
            wait=wait,
            timeout=args.timeout,
        )

    raise ValueError(f"Unsupported command: {args.command}")


def _print_command_output(
    *,
    controller: SoArmMoceController,
    command_name: str,
    result: Any,
    print_state_after: bool,
) -> None:
    if command_name == "state":
        _print_json("state-summary", _state_summary(result))
        return

    _print_json("result", result)
    if print_state_after:
        state_after = controller.get_state()
        _print_json("state-summary", _state_summary(state_after))


def _print_shell_help() -> None:
    print(
        "\n".join(
            [
                "shell commands:",
                "  state",
                "  meta",
                "  home [duration_sec]",
                "  stop",
                "  joint-target <joint> <target_deg> [duration_sec]",
                "  joint-delta <joint> <delta_deg> [duration_sec]",
                "  joints-target <joint=deg> [joint=deg ...] [duration_sec]",
                "  gripper <open_ratio> [duration_sec]",
                "  open-gripper [duration_sec]",
                "  close-gripper [duration_sec]",
                "  move-pose <x> <y> <z> [roll pitch yaw] [duration_sec]",
                "  move-delta <dx> <dy> <dz> [drx dry drz] [frame] [duration_sec]",
                "  help",
                "  quit",
            ]
        )
    )


def _run_shell(controller: SoArmMoceController) -> int:
    print("[shell] 已建立单次 controller 会话。当前姿态已经被记为本次会话的 startup reference。")
    print("[shell] 在这个 shell 里先动一下，再执行 home，才会回到刚进入 shell 时的姿态。输入 help 查看命令。")

    while True:
        try:
            line = input("smoke> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            return 0

        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError as exc:
            _print_json("error", {"type": type(exc).__name__, "message": str(exc)})
            continue

        command = str(parts[0]).strip().lower()
        if command in {"quit", "exit"}:
            return 0
        if command == "help":
            _print_shell_help()
            continue

        try:
            if command == "state":
                result = controller.get_state()
                _print_command_output(
                    controller=controller,
                    command_name="state",
                    result=result,
                    print_state_after=False,
                )
                continue

            if command == "meta":
                _print_json("result", controller.meta())
                continue

            if command == "home":
                duration = float(parts[1]) if len(parts) >= 2 else 2.0
                result = controller.home(duration=duration, wait=True, timeout=None)
                _print_command_output(
                    controller=controller,
                    command_name="home",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "stop":
                result = controller.stop()
                _print_command_output(
                    controller=controller,
                    command_name="stop",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "joint-target":
                if len(parts) < 3:
                    raise ValueError("Usage: joint-target <joint> <target_deg> [duration_sec]")
                duration = float(parts[3]) if len(parts) >= 4 else 1.5
                result = controller.move_joint(
                    joint=str(parts[1]),
                    target_deg=float(parts[2]),
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="joint-target",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "joint-delta":
                if len(parts) < 3:
                    raise ValueError("Usage: joint-delta <joint> <delta_deg> [duration_sec]")
                duration = float(parts[3]) if len(parts) >= 4 else 1.5
                result = controller.move_joint(
                    joint=str(parts[1]),
                    delta_deg=float(parts[2]),
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="joint-delta",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "joints-target":
                if len(parts) < 2:
                    raise ValueError("Usage: joints-target <joint=deg> [joint=deg ...] [duration_sec]")
                duration = 2.0
                raw_targets = list(parts[1:])
                if "=" not in raw_targets[-1]:
                    duration = float(raw_targets.pop())
                result = controller.move_joints(
                    _parse_joint_targets(raw_targets),
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="joints-target",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "gripper":
                if len(parts) < 2:
                    raise ValueError("Usage: gripper <open_ratio> [duration_sec]")
                duration = float(parts[2]) if len(parts) >= 3 else 1.0
                result = controller.set_gripper(
                    open_ratio=float(parts[1]),
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="gripper",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "open-gripper":
                duration = float(parts[1]) if len(parts) >= 2 else 1.0
                result = controller.open_gripper(duration=duration, wait=True, timeout=None)
                _print_command_output(
                    controller=controller,
                    command_name="open-gripper",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "close-gripper":
                duration = float(parts[1]) if len(parts) >= 2 else 1.0
                result = controller.close_gripper(duration=duration, wait=True, timeout=None)
                _print_command_output(
                    controller=controller,
                    command_name="close-gripper",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "move-pose":
                if len(parts) not in {4, 5, 7, 8}:
                    raise ValueError("Usage: move-pose <x> <y> <z> [roll pitch yaw] [duration_sec]")
                target_rpy = None
                duration = 2.0
                if len(parts) == 5:
                    duration = float(parts[4])
                elif len(parts) in {7, 8}:
                    target_rpy = [float(parts[4]), float(parts[5]), float(parts[6])]
                    if len(parts) == 8:
                        duration = float(parts[7])
                result = controller.move_pose(
                    xyz=[float(parts[1]), float(parts[2]), float(parts[3])],
                    rpy=target_rpy,
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="move-pose",
                    result=result,
                    print_state_after=True,
                )
                continue

            if command == "move-delta":
                if len(parts) < 4:
                    raise ValueError("Usage: move-delta <dx> <dy> <dz> [drx dry drz] [frame] [duration_sec]")
                drx = 0.0
                dry = 0.0
                drz = 0.0
                frame = "base"
                duration = 1.0
                remainder = list(parts[4:])
                if len(remainder) >= 3 and remainder[0] not in {"base", "tool"}:
                    drx = float(remainder.pop(0))
                    dry = float(remainder.pop(0))
                    drz = float(remainder.pop(0))
                if remainder:
                    if remainder[0] in {"base", "tool"}:
                        frame = str(remainder.pop(0))
                    if remainder:
                        duration = float(remainder.pop(0))
                if remainder:
                    raise ValueError("Too many arguments for move-delta")
                result = controller.move_delta(
                    dx=float(parts[1]),
                    dy=float(parts[2]),
                    dz=float(parts[3]),
                    drx=drx,
                    dry=dry,
                    drz=drz,
                    frame=frame,
                    duration=duration,
                    wait=True,
                    timeout=None,
                )
                _print_command_output(
                    controller=controller,
                    command_name="move-delta",
                    result=result,
                    print_state_after=True,
                )
                continue

            raise ValueError(f"Unknown shell command: {command}")
        except Exception as exc:
            _print_json("error", {"type": type(exc).__name__, "message": str(exc)})


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    config = resolve_config(args.config)
    controller = SoArmMoceController(config)

    try:
        controller._ensure_bus()
        if args.command == "shell":
            return _run_shell(controller)
        result = _run_command(controller, args)
        should_print_state = bool(args.print_state_after) or args.command in {
            "joint-target",
            "joint-delta",
            "joints-target",
            "home",
            "stop",
            "gripper",
            "open-gripper",
            "close-gripper",
            "move-pose",
            "move-delta",
        }
        _print_command_output(
            controller=controller,
            command_name=str(args.command),
            result=result,
            print_state_after=should_print_state,
        )

        return 0
    except Exception as exc:
        _print_json(
            "error",
            {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        )
        return 1
    finally:
        controller.close(disable_torque=bool(args.release_torque_on_exit))


if __name__ == "__main__":
    raise SystemExit(main())
