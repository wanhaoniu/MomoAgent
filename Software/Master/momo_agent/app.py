from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from typing import Optional

from .config import MomoAgentConfig, load_config
from .openclaw_client import OpenClawReply, build_openclaw_client
from .speech import audio_input_unavailable_message, record_until_enter, speak_text, transcribe_audio


def _join_text(parts: list[str]) -> str:
    return " ".join(str(part).strip() for part in parts if str(part).strip()).strip()


class MomoAgentApp:
    def __init__(self, config: MomoAgentConfig) -> None:
        self._config = config
        self._openclaw = build_openclaw_client(config.openclaw)

    def close(self) -> None:
        self._openclaw.close()

    def _print_bridge_timing(self, reply: OpenClawReply) -> None:
        payload = reply.raw_payload
        if not isinstance(payload, dict):
            return
        bridge = payload.get("bridge")
        if not isinstance(bridge, dict):
            return
        timing = bridge.get("timing")
        if not isinstance(timing, dict):
            return
        accept_ms = float(timing.get("accept_ms", 0.0) or 0.0)
        wait_ms = float(timing.get("wait_ms", 0.0) or 0.0)
        history_ms = float(timing.get("history_ms", 0.0) or 0.0)
        total_ms = float(timing.get("total_ms", 0.0) or 0.0)
        print(
            "[timing] openclaw-bridge "
            f"accept={accept_ms/1000.0:.2f}s "
            f"wait={wait_ms/1000.0:.2f}s "
            f"history={history_ms/1000.0:.2f}s "
            f"bridge_total={total_ms/1000.0:.2f}s",
            flush=True,
        )

    def _speak_reply(self, text: str) -> None:
        if not self._config.tts.enabled:
            return
        try:
            speak_text(text, self._config.tts)
        except Exception as exc:
            print(f"[tts] 播放失败: {exc}", flush=True)

    def ask_text(self, text: str, speak: bool = True) -> OpenClawReply:
        message = str(text or "").strip()
        if not message:
            raise RuntimeError("输入为空")
        started = time.perf_counter()
        reply = self._openclaw.ask(message)
        elapsed = time.perf_counter() - started
        print(f"[agent] {reply.text}", flush=True)
        print(f"[timing] openclaw={elapsed:.2f}s", flush=True)
        self._print_bridge_timing(reply)
        if speak:
            self._speak_reply(reply.text)
        return reply

    def warmup(self) -> None:
        started = time.perf_counter()
        reply = self._openclaw.ask("请只回复“就绪”。")
        elapsed = time.perf_counter() - started
        print(f"[warmup] {reply.text}", flush=True)
        print(f"[timing] warmup-openclaw={elapsed:.2f}s", flush=True)
        self._print_bridge_timing(reply)

    def reset_session(self) -> None:
        self._openclaw.reset_session()
        print("[session] 已重置本地缓存的 OpenClaw session", flush=True)

    def run_voice_turn(self, speak: bool = True) -> OpenClawReply:
        started = time.perf_counter()
        wav_bytes = record_until_enter(self._config.audio)
        record_elapsed = time.perf_counter() - started
        if not wav_bytes:
            raise RuntimeError("未检测到有效语音输入")
        print(f"[timing] record={record_elapsed:.2f}s bytes={len(wav_bytes)}", flush=True)

        stt_started = time.perf_counter()
        transcript = transcribe_audio(wav_bytes, self._config.stt)
        stt_elapsed = time.perf_counter() - stt_started
        print(f"[you] {transcript}", flush=True)
        print(f"[timing] stt={stt_elapsed:.2f}s", flush=True)
        return self.ask_text(transcript, speak=speak)

    def say(self, text: str) -> None:
        if not self._config.tts.enabled:
            raise RuntimeError("TTS 已禁用，可去掉 --no-tts 或打开 SOARMMOCE_TTS_ENABLED")
        started = time.perf_counter()
        speak_text(text, self._config.tts)
        elapsed = time.perf_counter() - started
        print(f"[timing] tts={elapsed:.2f}s", flush=True)

    def run_shell(self) -> int:
        print("Momo Agent shell", flush=True)
        print("输入自然语言会直接发给 OpenClaw。", flush=True)
        print(
            "命令: /voice 录音一轮, /say 文本播报, /session 查看会话, /warmup 预热, /reset 重置会话, /quit 退出",
            flush=True,
        )
        if not self._config.tts.enabled:
            print("[info] 当前 TTS 已关闭，可用 --no-tts 显式关闭或检查环境变量。", flush=True)
        while True:
            try:
                line = input("momo> ")
            except EOFError:
                print("", flush=True)
                return 0
            command = str(line or "").strip()
            if not command:
                continue
            if command in {"/quit", "/exit"}:
                return 0
            if command == "/help":
                print("命令: /voice, /say <text>, /session, /warmup, /reset, /quit", flush=True)
                continue
            if command == "/session":
                session_id = str(self._openclaw.session_id or "").strip()
                session_key = str(self._openclaw.bridge_session_key or "").strip()
                print(
                    f"[session] current={session_id or '<empty>'} bridge_key={session_key or '<empty>'}",
                    flush=True,
                )
                continue
            if command == "/warmup":
                try:
                    self.warmup()
                except Exception as exc:
                    print(f"[error] {exc}", flush=True)
                continue
            if command == "/reset":
                self.reset_session()
                continue
            if command == "/listen":
                return self.run_listen_loop(warmup=False)
                continue
            if command == "/voice":
                try:
                    self.run_voice_turn(speak=self._config.tts.enabled)
                except Exception as exc:
                    print(f"[error] {exc}", flush=True)
                continue
            if command.startswith("/say "):
                text = command[5:].strip()
                if not text:
                    print("[error] /say 后面需要文本", flush=True)
                    continue
                try:
                    self.say(text)
                except Exception as exc:
                    print(f"[error] {exc}", flush=True)
                continue
            try:
                self.ask_text(command, speak=self._config.tts.enabled)
            except Exception as exc:
                print(f"[error] {exc}", flush=True)

    def run_listen_loop(self, warmup: bool = False, speak: bool = True) -> int:
        print("Momo Agent listen mode", flush=True)
        print("按 Enter 开始一轮录音，录音后再按 Enter 结束；输入 q 退出。", flush=True)
        if warmup:
            try:
                self.warmup()
            except Exception as exc:
                print(f"[warmup-error] {exc}", flush=True)
        while True:
            try:
                line = input("listen> ")
            except EOFError:
                print("", flush=True)
                return 0
            command = str(line or "").strip().lower()
            if command in {"q", "quit", "exit", "/quit", "/exit"}:
                return 0
            try:
                self.run_voice_turn(speak=speak)
            except Exception as exc:
                print(f"[error] {exc}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lightweight voice/text OpenClaw agent for demos without the PyQt GUI."
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Do not play TTS replies for this run.",
    )
    parser.add_argument(
        "--force-new-session",
        action="store_true",
        help="Force a fresh OpenClaw session for this run.",
    )
    parser.add_argument(
        "--max-record-sec",
        type=float,
        default=None,
        help="Override the maximum voice recording duration.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("shell", help="Run the interactive shell")

    ask_parser = subparsers.add_parser("ask", help="Send one text prompt to OpenClaw")
    ask_parser.add_argument("text", nargs="+", help="Prompt text")

    voice_parser = subparsers.add_parser("voice", help="Record one voice turn and send it to OpenClaw")
    voice_parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Do not speak the reply for this one-shot voice turn.",
    )

    listen_parser = subparsers.add_parser(
        "listen",
        help="Keep one process alive and run repeated voice turns with a warm session",
    )
    listen_parser.add_argument(
        "--warmup",
        action="store_true",
        help="Send one hidden warmup turn before entering the listen loop.",
    )
    listen_parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Do not speak replies in listen mode.",
    )

    say_parser = subparsers.add_parser("say", help="Only play TTS for the given text")
    say_parser.add_argument("text", nargs="+", help="Text to speak")
    subparsers.add_parser("warmup", help="Warm the current OpenClaw session once")
    subparsers.add_parser("reset-session", help="Clear the cached OpenClaw session state")
    return parser


def _apply_cli_overrides(config: MomoAgentConfig, args: argparse.Namespace) -> MomoAgentConfig:
    openclaw = config.openclaw
    if getattr(args, "force_new_session", False):
        openclaw = replace(openclaw, force_new_session=True, session_id="")
    tts = config.tts
    if getattr(args, "no_tts", False):
        tts = replace(tts, enabled=False)
    audio = config.audio
    if args.max_record_sec is not None:
        audio = replace(audio, max_record_sec=max(1.0, float(args.max_record_sec)))
    return MomoAgentConfig(audio=audio, stt=config.stt, tts=tts, openclaw=openclaw)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "shell"
    config = _apply_cli_overrides(load_config(), args)
    app = MomoAgentApp(config)
    try:
        if command == "shell":
            return app.run_shell()
        if command == "ask":
            app.ask_text(_join_text(args.text), speak=config.tts.enabled)
            return 0
        if command == "voice":
            app.run_voice_turn(speak=(config.tts.enabled and not getattr(args, "no_speak", False)))
            return 0
        if command == "listen":
            return app.run_listen_loop(
                warmup=bool(getattr(args, "warmup", False)),
                speak=(config.tts.enabled and not getattr(args, "no_speak", False)),
            )
        if command == "say":
            app.say(_join_text(args.text))
            return 0
        if command == "warmup":
            app.warmup()
            return 0
        if command == "reset-session":
            app.reset_session()
            return 0
        parser.print_help(sys.stderr)
        return 1
    finally:
        app.close()
