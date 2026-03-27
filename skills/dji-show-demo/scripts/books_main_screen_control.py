#!/usr/bin/env python3
"""Main-screen controller for Books demo actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = SCRIPT_DIR.parent.parent
MOUSE_TOOL = Path(
    os.environ.get(
        "DJI_SHOW_DEMO_BOOKS_MOUSE_TOOL",
        str(SKILLS_DIR / "screen-pointer-tools" / "scripts" / "mouse_click_helper.sh"),
    )
)
MACOS_USE_TOOL = Path(
    os.environ.get(
        "DJI_SHOW_DEMO_BOOKS_MACOS_USE_TOOL",
        str(SKILLS_DIR / "macos-use-desktop-control" / "scripts" / "macos_use_control.py"),
    )
)
BOOKS_IDENTIFIER = os.environ.get("DJI_SHOW_DEMO_BOOKS_IDENTIFIER", "com.apple.iBooksX")
OPEN_DELAY_MS = float(os.environ.get("DJI_SHOW_DEMO_BOOKS_OPEN_DELAY_MS", "1200"))
BETWEEN_MS = float(os.environ.get("DJI_SHOW_DEMO_BOOKS_BETWEEN_MS", "600"))
ACTIVATE_DELAY_MS = float(os.environ.get("DJI_SHOW_DEMO_BOOKS_ACTIVATE_DELAY_MS", "500"))
READER_CONTROL_DELAY_MS = float(os.environ.get("DJI_SHOW_DEMO_BOOKS_READER_CONTROL_DELAY_MS", "300"))

ELEMENT_RE = re.compile(
    r'^\s*\[(?P<role>[^\]]+)\]'
    r'(?:\s+"(?P<text>.*)")?'
    r'\s+x:(?P<x>-?\d+(?:\.\d+)?)'
    r'\s+y:(?P<y>-?\d+(?:\.\d+)?)'
    r'\s+w:(?P<w>\d+(?:\.\d+)?)'
    r'\s+h:(?P<h>\d+(?:\.\d+)?)'
    r'(?P<visible>\s+visible)?\s*$'
)


@dataclass
class Element:
    role: str
    text: str
    x: float
    y: float
    w: float
    h: float
    visible: bool

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2.0


@dataclass
class TraversalState:
    pid: int
    file: Path
    elements: list[Element]


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    return proc


def _sleep_ms(ms: float, *, dry_run: bool) -> None:
    if dry_run:
        return
    time.sleep(max(0.0, float(ms)) / 1000.0)


def _parse_json_stdout(stdout: str) -> dict:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON output, got: {stdout.strip() or '<empty>'}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected JSON object output")
    return payload


def _ensure_tools() -> None:
    if not MOUSE_TOOL.exists():
        raise FileNotFoundError(f"mouse tool not found: {MOUSE_TOOL}")
    if not MACOS_USE_TOOL.exists():
        raise FileNotFoundError(f"macos-use tool not found: {MACOS_USE_TOOL}")


def _mouse_proxy(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["bash", str(MOUSE_TOOL), *args], check=True)


def _activate_books(*, dry_run: bool, steps: list[str]) -> None:
    steps.append(f"activate identifier={BOOKS_IDENTIFIER}")
    if dry_run:
        return
    _mouse_proxy("activate", "--identifier", BOOKS_IDENTIFIER)
    _sleep_ms(ACTIVATE_DELAY_MS, dry_run=dry_run)


def _click_at(x: float, y: float, *, count: int = 1, dry_run: bool, steps: list[str]) -> None:
    steps.append(f"click x={x:.2f} y={y:.2f} count={count}")
    if dry_run:
        return
    _mouse_proxy("click", "--x", f"{x:.2f}", "--y", f"{y:.2f}", "--count", str(int(count)))


def _press_key(key_name: str, *, dry_run: bool, steps: list[str]) -> None:
    steps.append(f"key {key_name}")
    if dry_run:
        return
    _mouse_proxy("key", "--key", key_name)


def _open_books() -> dict:
    proc = _run(["python3", str(MACOS_USE_TOOL), "--json", "open", "--app", BOOKS_IDENTIFIER])
    payload = _parse_json_stdout(proc.stdout)
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or payload.get("summary") or "Failed to open Books")
    return payload


def _refresh_books(pid: int) -> dict:
    proc = _run(["python3", str(MACOS_USE_TOOL), "--json", "refresh", "--pid", str(int(pid))])
    payload = _parse_json_stdout(proc.stdout)
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or payload.get("summary") or f"Failed to refresh Books PID {pid}")
    return payload


def _elements_from_file(path: Path) -> list[Element]:
    elements: list[Element] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = ELEMENT_RE.match(line)
        if not match:
            continue
        elements.append(
            Element(
                role=match.group("role") or "",
                text=match.group("text") or "",
                x=float(match.group("x")),
                y=float(match.group("y")),
                w=float(match.group("w")),
                h=float(match.group("h")),
                visible=bool(match.group("visible")),
            )
        )
    return elements


def _state_from_payload(payload: dict) -> TraversalState:
    pid = int(payload.get("pid") or 0)
    file_path = str(payload.get("file") or "").strip()
    if pid <= 0 or not file_path:
        raise RuntimeError("Traversal payload did not include a PID and file path")
    file = Path(file_path)
    if not file.exists():
        raise RuntimeError(f"Traversal file does not exist: {file}")
    return TraversalState(pid=pid, file=file, elements=_elements_from_file(file))


def _role_is(role: str, prefix: str) -> bool:
    return role.startswith(prefix)


def _find_continue_button(elements: Iterable[Element]) -> Element | None:
    for element in elements:
        if element.visible and _role_is(element.role, "AXButton") and element.text.startswith("继续阅读"):
            return element
    return None


def _find_reader_window(elements: Iterable[Element]) -> Element | None:
    candidates = [
        element
        for element in elements
        if element.visible and _role_is(element.role, "AXWindow") and element.text.strip() and element.text.strip() != "主页"
    ]
    return candidates[0] if candidates else None


def _find_recent_card(elements: Iterable[Element]) -> Element | None:
    candidates: list[tuple[int, float, float, Element]] = []
    for element in elements:
        if not element.visible:
            continue
        if not (_role_is(element.role, "AXButton") or _role_is(element.role, "AXGenericElement")):
            continue
        text = element.text.strip()
        if not text:
            continue
        priority = None
        if text.startswith("继续阅读"):
            priority = 0
        elif "的作品《" in text:
            priority = 1
        elif "更多选项" in text and (", 图书" in text or ", PDF" in text):
            priority = 2
        elif text.startswith("已完成 ") and ("PDF" in text or "图书" in text):
            priority = 3
        if priority is not None:
            candidates.append((priority, element.y, element.x, element))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3] if candidates else None


def _find_pager_button(elements: Iterable[Element], label: str) -> Element | None:
    for element in elements:
        if element.visible and _role_is(element.role, "AXButton") and element.text.strip() == label:
            return element
    return None


def _has_reader_content(elements: Iterable[Element]) -> bool:
    for element in elements:
        if not element.visible or not _role_is(element.role, "AXStaticText"):
            continue
        text = element.text.strip()
        if len(text) < 60:
            continue
        if "Every Summer After" in text:
            continue
        return True
    return False


def _emit_targets() -> None:
    print("mode")
    print("  books-mode: main-screen dynamic traversal")
    print("open-recent")
    print("  step-1: open or activate Books on the main screen")
    print("  step-2: choose the first visible recent-reading card")
    print("  step-3: click 继续阅读 if the detail window appears")
    print("next-page")
    print("  step-1: ensure the reader content is visible")
    print("  step-2: reveal reader controls near the right edge if needed")
    print("  step-3: click the visible 下一页 button when present")


def _open_recent_main(*, dry_run: bool, skip_activate: bool, open_delay_ms: float) -> tuple[TraversalState, list[str]]:
    steps: list[str] = []
    if dry_run:
        steps.append(f"open books app={BOOKS_IDENTIFIER}")
        if not skip_activate:
            steps.append(f"activate identifier={BOOKS_IDENTIFIER}")
        steps.append("double-click first visible recent-reading card on the main Books screen")
        steps.append("click visible 继续阅读 button if a detail window appears")
        return TraversalState(pid=0, file=Path("<dry-run>"), elements=[]), steps

    open_payload = _open_books()
    state = _state_from_payload(open_payload)
    steps.append(f"open books pid={state.pid}")
    if not dry_run:
        _sleep_ms(BETWEEN_MS, dry_run=False)
        state = _state_from_payload(_refresh_books(state.pid))

    if _has_reader_content(state.elements):
        return state, steps

    if not skip_activate:
        _activate_books(dry_run=dry_run, steps=steps)

    continue_button = _find_continue_button(state.elements)
    if continue_button is None:
        recent_card = _find_recent_card(state.elements)
        if recent_card is None:
            raise RuntimeError("Could not find a recent-reading card on the main Books screen")
        _click_at(recent_card.center_x, recent_card.center_y, count=2, dry_run=dry_run, steps=steps)
        _sleep_ms(open_delay_ms, dry_run=dry_run)
        if not dry_run:
            state = _state_from_payload(_refresh_books(state.pid))
            continue_button = _find_continue_button(state.elements)

    if continue_button is not None:
        if not skip_activate:
            _activate_books(dry_run=dry_run, steps=steps)
        _click_at(continue_button.center_x, continue_button.center_y, dry_run=dry_run, steps=steps)
        _sleep_ms(open_delay_ms, dry_run=dry_run)
        if not dry_run:
            state = _state_from_payload(_refresh_books(state.pid))

    if not dry_run and not (_has_reader_content(state.elements) or _find_reader_window(state.elements)):
        raise RuntimeError("Books main-screen flow did not reach a readable book view")

    return state, steps


def _ensure_reader_main(*, dry_run: bool, skip_activate: bool, open_delay_ms: float) -> tuple[TraversalState, list[str]]:
    state, steps = _open_recent_main(dry_run=dry_run, skip_activate=skip_activate, open_delay_ms=open_delay_ms)
    return state, steps


def _page_turn_main(*, direction: str, dry_run: bool, skip_activate: bool, open_delay_ms: float) -> tuple[TraversalState, list[str]]:
    if dry_run:
        steps = [
            f"open books app={BOOKS_IDENTIFIER}",
            "open the recent-reading card on the main screen if needed",
            "enter the readable page with 继续阅读 if needed",
            "reveal the reader controls near the page edge if needed",
            f"click visible {'下一页' if direction == 'next' else '上一页'} when available",
        ]
        if not skip_activate:
            steps.insert(1, f"activate identifier={BOOKS_IDENTIFIER}")
        return TraversalState(pid=0, file=Path("<dry-run>"), elements=[]), steps

    state, steps = _ensure_reader_main(dry_run=dry_run, skip_activate=skip_activate, open_delay_ms=open_delay_ms)
    label = "下一页" if direction == "next" else "上一页"
    edge_ratio = 0.94 if direction == "next" else 0.06

    pager_button = _find_pager_button(state.elements, label)
    reader_window = _find_reader_window(state.elements)
    if pager_button is None and reader_window is not None:
        if not skip_activate:
            _activate_books(dry_run=dry_run, steps=steps)
        reveal_x = reader_window.x + reader_window.w * edge_ratio
        reveal_y = reader_window.y + reader_window.h / 2.0
        _click_at(reveal_x, reveal_y, dry_run=dry_run, steps=steps)
        _sleep_ms(READER_CONTROL_DELAY_MS, dry_run=dry_run)
        if not dry_run:
            state = _state_from_payload(_refresh_books(state.pid))
            pager_button = _find_pager_button(state.elements, label)

    if pager_button is not None:
        if not skip_activate:
            _activate_books(dry_run=dry_run, steps=steps)
        _click_at(pager_button.center_x, pager_button.center_y, dry_run=dry_run, steps=steps)
        _sleep_ms(READER_CONTROL_DELAY_MS, dry_run=dry_run)
        if not dry_run:
            state = _state_from_payload(_refresh_books(state.pid))
    elif reader_window is not None:
        if not skip_activate:
            _activate_books(dry_run=dry_run, steps=steps)
        fallback_x = reader_window.x + reader_window.w * edge_ratio
        fallback_y = reader_window.y + reader_window.h / 2.0
        _click_at(fallback_x, fallback_y, dry_run=dry_run, steps=steps)
        _sleep_ms(READER_CONTROL_DELAY_MS, dry_run=dry_run)
        if not dry_run:
            state = _state_from_payload(_refresh_books(state.pid))
    else:
        raise RuntimeError("Could not find a visible Books reader window on the main screen")

    return state, steps


def _close_main(*, dry_run: bool, skip_activate: bool, open_delay_ms: float) -> tuple[TraversalState, list[str]]:
    state, steps = _ensure_reader_main(dry_run=dry_run, skip_activate=skip_activate, open_delay_ms=open_delay_ms)
    reader_window = _find_reader_window(state.elements)
    if reader_window is None:
        raise RuntimeError("Could not find a visible Books reader window to close")
    close_x = reader_window.x + 18.0
    close_y = reader_window.y + 18.0
    if not skip_activate:
        _activate_books(dry_run=dry_run, steps=steps)
    _click_at(close_x, close_y, dry_run=dry_run, steps=steps)
    return state, steps


def _render_dry_run(header: str, steps: list[str]) -> None:
    print(header)
    for item in steps:
        print(f"  {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Main-screen Books demo helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    point = sub.add_parser("point")
    point.add_argument("--json", action="store_true")

    sub.add_parser("health")
    sub.add_parser("trust")
    sub.add_parser("targets")

    activate = sub.add_parser("activate-books")
    activate.add_argument("--dry-run", action="store_true")

    move = sub.add_parser("move-books-window")
    move.add_argument("--dry-run", action="store_true")

    open_recent = sub.add_parser("open-recent", aliases=["open", "books-open-recent"])
    open_recent.add_argument("--dry-run", action="store_true")
    open_recent.add_argument("--skip-activate", action="store_true")
    open_recent.add_argument("--skip-move", action="store_true")
    open_recent.add_argument("--open-delay-ms", type=float, default=OPEN_DELAY_MS)

    next_page = sub.add_parser("next-page", aliases=["books-next-page", "page-forward"])
    next_page.add_argument("--dry-run", action="store_true")
    next_page.add_argument("--skip-activate", action="store_true")

    next_page_key = sub.add_parser("next-page-key")
    next_page_key.add_argument("--dry-run", action="store_true")
    next_page_key.add_argument("--skip-activate", action="store_true")

    previous_page = sub.add_parser("previous-page-key", aliases=["previous-page", "books-previous-page", "page-back"])
    previous_page.add_argument("--dry-run", action="store_true")
    previous_page.add_argument("--skip-activate", action="store_true")

    close = sub.add_parser("close")
    close.add_argument("--dry-run", action="store_true")
    close.add_argument("--skip-activate", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--steps", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--between-ms", type=float, default=BETWEEN_MS)
    return parser


def main() -> int:
    _ensure_tools()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "health":
        proc = _mouse_proxy("health")
        sys.stdout.write(proc.stdout)
        return 0
    if args.cmd == "trust":
        proc = _mouse_proxy("trust")
        sys.stdout.write(proc.stdout)
        return 0
    if args.cmd == "point":
        proxy_args = ["point"]
        if args.json:
            proxy_args.append("--json")
        proc = _mouse_proxy(*proxy_args)
        sys.stdout.write(proc.stdout)
        return 0
    if args.cmd == "targets":
        _emit_targets()
        return 0
    if args.cmd == "activate-books":
        steps: list[str] = []
        _activate_books(dry_run=bool(args.dry_run), steps=steps)
        if args.dry_run:
            _render_dry_run("step activate-books", steps)
        return 0
    if args.cmd == "move-books-window":
        if args.dry_run:
            _render_dry_run("step move-books-window", ["noop main-screen mode"])
            return 0
        print("Books main-screen mode does not move windows to Sidecar.")
        return 0
    if args.cmd in {"open-recent", "open", "books-open-recent"}:
        state, steps = _open_recent_main(
            dry_run=bool(args.dry_run),
            skip_activate=bool(args.skip_activate),
            open_delay_ms=float(args.open_delay_ms),
        )
        if args.dry_run:
            _render_dry_run("step open-recent", steps)
        else:
            print(f"BOOKS_PID\t{state.pid}")
            print(f"BOOKS_FILE\t{state.file}")
        return 0
    if args.cmd in {"next-page", "books-next-page", "page-forward"}:
        state, steps = _page_turn_main(
            direction="next",
            dry_run=bool(args.dry_run),
            skip_activate=bool(args.skip_activate),
            open_delay_ms=OPEN_DELAY_MS,
        )
        if args.dry_run:
            _render_dry_run("step next-page", steps)
        else:
            print(f"BOOKS_PID\t{state.pid}")
            print(f"BOOKS_FILE\t{state.file}")
        return 0
    if args.cmd == "next-page-key":
        steps: list[str] = []
        if not args.skip_activate:
            _activate_books(dry_run=bool(args.dry_run), steps=steps)
        _press_key("RightArrow", dry_run=bool(args.dry_run), steps=steps)
        if args.dry_run:
            _render_dry_run("step next-page-key", steps)
        return 0
    if args.cmd in {"previous-page-key", "previous-page", "books-previous-page", "page-back"}:
        if args.cmd == "previous-page-key":
            steps = []
            if not args.skip_activate:
                _activate_books(dry_run=bool(args.dry_run), steps=steps)
            _press_key("LeftArrow", dry_run=bool(args.dry_run), steps=steps)
            if args.dry_run:
                _render_dry_run("step previous-page-key", steps)
            return 0
        state, steps = _page_turn_main(
            direction="previous",
            dry_run=bool(args.dry_run),
            skip_activate=bool(args.skip_activate),
            open_delay_ms=OPEN_DELAY_MS,
        )
        if args.dry_run:
            _render_dry_run("step previous-page", steps)
        else:
            print(f"BOOKS_PID\t{state.pid}")
            print(f"BOOKS_FILE\t{state.file}")
        return 0
    if args.cmd == "close":
        state, steps = _close_main(
            dry_run=bool(args.dry_run),
            skip_activate=bool(args.skip_activate),
            open_delay_ms=OPEN_DELAY_MS,
        )
        if args.dry_run:
            _render_dry_run("step close", steps)
        else:
            print(f"BOOKS_PID\t{state.pid}")
            print(f"BOOKS_FILE\t{state.file}")
        return 0
    if args.cmd == "run":
        step_list = [item.strip() for item in str(args.steps).split(",") if item.strip()]
        if args.dry_run:
            print(f"sequence {','.join(step_list)}")
        for index, step in enumerate(step_list):
            if step in {"activate-books"}:
                activate_steps: list[str] = []
                _activate_books(dry_run=bool(args.dry_run), steps=activate_steps)
                if args.dry_run:
                    _render_dry_run("step activate-books", activate_steps)
            elif step in {"open", "open-recent", "books-open-recent"}:
                _, step_trace = _open_recent_main(
                    dry_run=bool(args.dry_run),
                    skip_activate=False,
                    open_delay_ms=OPEN_DELAY_MS,
                )
                if args.dry_run:
                    _render_dry_run("step open-recent", step_trace)
            elif step in {"next-page", "books-next-page", "page-forward"}:
                _, step_trace = _page_turn_main(
                    direction="next",
                    dry_run=bool(args.dry_run),
                    skip_activate=False,
                    open_delay_ms=OPEN_DELAY_MS,
                )
                if args.dry_run:
                    _render_dry_run("step next-page", step_trace)
            elif step in {"close"}:
                _, step_trace = _close_main(
                    dry_run=bool(args.dry_run),
                    skip_activate=False,
                    open_delay_ms=OPEN_DELAY_MS,
                )
                if args.dry_run:
                    _render_dry_run("step close", step_trace)
            else:
                raise RuntimeError(f"Unknown step: {step}")
            if index < len(step_list) - 1:
                _sleep_ms(float(args.between_ms), dry_run=bool(args.dry_run))
        return 0

    raise RuntimeError(f"Unsupported command: {args.cmd}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
