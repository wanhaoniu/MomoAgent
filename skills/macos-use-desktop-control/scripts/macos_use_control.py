#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import threading
import queue
from typing import Any, Optional

PROJECT_ROOT = "/Users/moce/.openclaw/mcp/macos-use"
SERVER_BIN_CANDIDATES = [
    os.path.join(PROJECT_ROOT, ".build", "release", "mcp-server-macos-use"),
    os.path.join(PROJECT_ROOT, ".build", "debug", "mcp-server-macos-use"),
]


class MCPClient:
    def __init__(self, binary: str, timeout: float = 30.0):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, method: str, params: Optional[dict] = None, notify: bool = False) -> Optional[dict]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            msg["id"] = self._next_id()
        if params is not None:
            msg["params"] = params

        data = (json.dumps(msg) + "\n").encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

        if notify:
            return None

        return self._read_response()

    def _read_response(self) -> dict:
        assert self.proc.stdout is not None
        result_q: queue.Queue = queue.Queue()

        def reader():
            try:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        result_q.put(RuntimeError("Server closed stdout"))
                        return
                    line = line.strip()
                    if line:
                        result_q.put(json.loads(line.decode("utf-8")))
                        return
            except Exception as exc:
                result_q.put(exc)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            result = result_q.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError(f"No response from server after {self.timeout}s")
        if isinstance(result, Exception):
            raise result
        return result

    def initialize(self) -> dict:
        result = self.send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "macos-use-control", "version": "1.0"},
        })
        self.send("notifications/initialized", notify=True)
        return result

    def list_tools(self) -> list[dict]:
        response = self.send("tools/list", {})
        return response.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> "ToolResult":
        response = self.send("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result", {})
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        return ToolResult(text=text, stderr_lines=self._stderr_lines[-20:])

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class ToolResult:
    def __init__(self, text: str, stderr_lines: list[str]):
        self.raw = text
        self.stderr_lines = stderr_lines
        self.fields: dict[str, str] = {}
        for line in text.splitlines():
            if ": " in line and not line.startswith("  "):
                key, _, value = line.partition(": ")
                self.fields[key.strip()] = value.strip()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.fields.get("status", "unknown"),
            "summary": self.fields.get("summary", ""),
            "file": self.fields.get("file"),
            "screenshot": self.fields.get("screenshot"),
            "app": self.fields.get("app"),
            "pid": int(self.fields["pid"]) if self.fields.get("pid") else None,
            "error": self.fields.get("error") or self.fields.get("traversal_error"),
            "raw": self.raw,
            "stderr_tail": self.stderr_lines,
        }
        return data


def resolve_server_binary() -> str:
    for path in SERVER_BIN_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "macos-use binary not found. Build it first with: "
        "cd /Users/moce/.openclaw/mcp/macos-use && xcrun swift build -c release"
    )


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def emit_result(result: ToolResult, as_json: bool):
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.raw.rstrip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convenience wrapper around the local macos-use MCP server.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of the raw MCP summary.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("tools", help="List exposed macos-use tools.")

    open_parser = subparsers.add_parser("open", help="Open or activate an application and traverse its UI tree.")
    open_parser.add_argument("--app", required=True, help="App name, bundle id, or .app path.")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh an app traversal by PID.")
    refresh_parser.add_argument("--pid", type=int, required=True)

    click_text = subparsers.add_parser("click-text", help="Find a visible element by text and click it.")
    click_text.add_argument("--pid", type=int, required=True)
    click_text.add_argument("--text", required=True, help="Case-insensitive partial text to match.")
    click_text.add_argument("--role", help="Optional accessibility role filter such as AXButton.")
    click_text.add_argument("--double-click", action="store_true")
    click_text.add_argument("--right-click", action="store_true")
    click_text.add_argument("--type-text", help="Optional text to type after the click.")
    click_text.add_argument("--press-key", help="Optional key to press after click/type.")
    click_text.add_argument("--press-key-modifiers", help="Comma-separated modifier list.")

    click_coord = subparsers.add_parser("click-coord", help="Click coordinates within an app window and traverse again.")
    click_coord.add_argument("--pid", type=int, required=True)
    click_coord.add_argument("--x", type=float, required=True)
    click_coord.add_argument("--y", type=float, required=True)
    click_coord.add_argument("--width", type=float)
    click_coord.add_argument("--height", type=float)
    click_coord.add_argument("--double-click", action="store_true")
    click_coord.add_argument("--right-click", action="store_true")
    click_coord.add_argument("--type-text", help="Optional text to type after the click.")
    click_coord.add_argument("--press-key", help="Optional key to press after click/type.")
    click_coord.add_argument("--press-key-modifiers", help="Comma-separated modifier list.")

    type_parser = subparsers.add_parser("type", help="Type text into the target app.")
    type_parser.add_argument("--pid", type=int, required=True)
    type_parser.add_argument("--text", required=True)
    type_parser.add_argument("--press-key", help="Optional key to press after typing.")
    type_parser.add_argument("--press-key-modifiers", help="Comma-separated modifier list.")

    key_parser = subparsers.add_parser("key", help="Press a key in the target app.")
    key_parser.add_argument("--pid", type=int, required=True)
    key_parser.add_argument("--key", required=True)
    key_parser.add_argument("--modifiers", help="Comma-separated modifier list.")

    scroll_parser = subparsers.add_parser("scroll", help="Scroll at a coordinate in the target app.")
    scroll_parser.add_argument("--pid", type=int, required=True)
    scroll_parser.add_argument("--x", type=float, required=True)
    scroll_parser.add_argument("--y", type=float, required=True)
    scroll_parser.add_argument("--delta-y", type=int, required=True)
    scroll_parser.add_argument("--delta-x", type=int, default=0)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "tools":
        client = MCPClient(resolve_server_binary())
        try:
            client.initialize()
            tools = client.list_tools()
        finally:
            client.close()
        if args.json:
            print(json.dumps(tools, ensure_ascii=False, indent=2))
        else:
            for tool in tools:
                print(f'{tool["name"]}: {tool.get("description", "")}')
        return 0

    client = MCPClient(resolve_server_binary())
    try:
        client.initialize()

        if args.command == "open":
            result = client.call_tool("macos-use_open_application_and_traverse", {"identifier": args.app})
        elif args.command == "refresh":
            result = client.call_tool("macos-use_refresh_traversal", {"pid": args.pid})
        elif args.command == "click-text":
            payload: dict[str, Any] = {
                "pid": args.pid,
                "element": args.text,
            }
            if args.role:
                payload["role"] = args.role
            if args.double_click:
                payload["doubleClick"] = True
            if args.right_click:
                payload["rightClick"] = True
            if args.type_text:
                payload["text"] = args.type_text
            if args.press_key:
                payload["pressKey"] = args.press_key
            if args.press_key_modifiers:
                payload["pressKeyModifiers"] = comma_list(args.press_key_modifiers)
            result = client.call_tool("macos-use_click_and_traverse", payload)
        elif args.command == "click-coord":
            payload = {
                "pid": args.pid,
                "x": args.x,
                "y": args.y,
            }
            if args.width is not None:
                payload["width"] = args.width
            if args.height is not None:
                payload["height"] = args.height
            if args.double_click:
                payload["doubleClick"] = True
            if args.right_click:
                payload["rightClick"] = True
            if args.type_text:
                payload["text"] = args.type_text
            if args.press_key:
                payload["pressKey"] = args.press_key
            if args.press_key_modifiers:
                payload["pressKeyModifiers"] = comma_list(args.press_key_modifiers)
            result = client.call_tool("macos-use_click_and_traverse", payload)
        elif args.command == "type":
            payload = {
                "pid": args.pid,
                "text": args.text,
            }
            if args.press_key:
                payload["pressKey"] = args.press_key
            if args.press_key_modifiers:
                payload["pressKeyModifiers"] = comma_list(args.press_key_modifiers)
            result = client.call_tool("macos-use_type_and_traverse", payload)
        elif args.command == "key":
            payload = {
                "pid": args.pid,
                "keyName": args.key,
            }
            if args.modifiers:
                payload["modifierFlags"] = comma_list(args.modifiers)
            result = client.call_tool("macos-use_press_key_and_traverse", payload)
        elif args.command == "scroll":
            result = client.call_tool("macos-use_scroll_and_traverse", {
                "pid": args.pid,
                "x": args.x,
                "y": args.y,
                "deltaY": args.delta_y,
                "deltaX": args.delta_x,
            })
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 64

        emit_result(result, as_json=args.json)
        if result.fields.get("status") == "error":
            return 1
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
