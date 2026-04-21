from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

ALLOWED_HAIGUITANG_AGENT_CLIPS = frozenset({"default", "nod", "shake", "outro"})
ALLOWED_HAIGUITANG_AGENT_ACTIONS = frozenset({"none", "nod", "shake"})
ALLOWED_HAIGUITANG_DIFFICULTIES = frozenset({"easy", "medium", "hard"})
ALLOWED_HAIGUITANG_ROUND_STATUS = frozenset({"ongoing", "solved", "revealed"})
DEFAULT_SUBTITLE_LIMIT = 30

_JSON_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
_JSON_XML_TAG_RE = re.compile(r"<haiguitang>(.*?)</haiguitang>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_OUTRO_PATTERNS = (
    "答案是",
    "谜底",
    "揭晓",
    "公布答案",
    "真相",
    "结束",
)
_NEGATIVE_PATTERNS = (
    "不是",
    "不对",
    "并非",
    "没有",
    "猜错",
    "不行",
    "不能",
    "错了",
)
_AFFIRMATIVE_PATTERNS = (
    "是的",
    "对的",
    "没错",
    "可以",
    "确实",
    "答对",
    "有的",
    "没问题",
    "正确",
)

_CLIP_ALIASES = {
    "default": "default",
    "idle": "default",
    "thinking": "default",
    "mystery": "default",
    "neutral": "default",
    "ponder": "default",
    "pondering": "default",
    "nod": "nod",
    "yes": "nod",
    "positive": "nod",
    "happy": "nod",
    "approve": "nod",
    "shake": "shake",
    "no": "shake",
    "negative": "shake",
    "deny": "shake",
    "refuse": "shake",
    "outro": "outro",
    "ending": "outro",
    "end": "outro",
    "finish": "outro",
    "reveal": "outro",
}
_ACTION_ALIASES = {
    "none": "none",
    "idle": "none",
    "default": "none",
    "center": "none",
    "stay": "none",
    "nod": "nod",
    "yes": "nod",
    "approve": "nod",
    "shake": "shake",
    "no": "shake",
    "deny": "shake",
}


@dataclass
class HaiGuiTangAgentDirective:
    spoken_text: str
    subtitle_text: str
    clip: str = "default"
    action: str = "none"
    loop_playback: bool = True
    round_status: str = "ongoing"
    mood: str = ""
    parse_mode: str = "heuristic"
    raw_reply: str = ""

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def build_haiguitang_agent_prompt(user_message: str) -> str:
    message = str(user_message or "").strip()
    return (
        "你现在是“海龟汤”网页场景里的互动角色，要和玩家继续玩海龟汤。\n"
        "你的输出会被程序直接解析，并驱动全屏视频和机械臂动作。\n"
        "请只输出一个 JSON 对象，不要输出 markdown、解释、前缀或代码块。\n"
        "字段要求：\n"
        "{\n"
        '  "spoken_text": "要说给用户听的完整中文台词",\n'
        '  "subtitle_text": "用于悬浮字幕的短句",\n'
        '  "clip": "default|nod|shake|outro",\n'
        '  "action": "none|nod|shake",\n'
        '  "loop_playback": true,\n'
        '  "round_status": "ongoing|solved|revealed"\n'
        "}\n"
        "规则：\n"
        "1. 肯定、认可、接近正确时，用 clip=nod、action=nod、loop_playback=false。\n"
        "2. 否定、纠正、猜错时，用 clip=shake、action=shake、loop_playback=false。\n"
        "3. 思考、卖关子、继续追问时，用 clip=default、action=none、loop_playback=true。\n"
        "4. 揭晓或结束时，用 clip=outro、action=none、loop_playback=false。\n"
        "5. 如果玩家还没真正答出核心真相，round_status=ongoing。普通的是非问题即使答“是”，也仍然通常是 ongoing。\n"
        "6. 只有玩家已经猜中核心答案或真相时，round_status=solved。\n"
        "7. 如果你直接公布谜底、解释真相、宣布结束，则 round_status=revealed。\n"
        "8. spoken_text 保持自然中文；subtitle_text 更短，尽量不超过 24 个字。\n"
        "用户输入：\n"
        f"{message}"
    )


def build_haiguitang_round_start_prompt(difficulty: str) -> str:
    normalized = _normalize_difficulty(difficulty)
    difficulty_label = {
        "easy": "简单",
        "medium": "中等",
        "hard": "困难",
    }[normalized]
    difficulty_rules = {
        "easy": "题面要直观一些，线索更清楚，答案不要太绕。",
        "medium": "题面保留一定反转，但整体仍然能通过追问逐步摸到真相。",
        "hard": "题面可以更迷惑一些，但仍要自洽、可推理，不要靠纯随机脑洞。",
    }[normalized]
    return (
        "你现在是“海龟汤”网页场景里的出题角色，要立刻开始一局新的海龟汤。\n"
        "你的输出会被程序直接解析，并驱动全屏视频和机械臂动作。\n"
        "请只输出一个 JSON 对象，不要输出 markdown、解释、前缀或代码块。\n"
        "字段要求：\n"
        "{\n"
        '  "spoken_text": "要说给用户听的完整中文台词",\n'
        '  "subtitle_text": "用于悬浮字幕的短句",\n'
        '  "clip": "default",\n'
        '  "action": "none",\n'
        '  "loop_playback": true,\n'
        '  "round_status": "ongoing"\n'
        "}\n"
        "开局规则：\n"
        "1. 现在是新一局的开场，你要先出题。\n"
        f"2. 本局难度必须是：{difficulty_label}。\n"
        f"3. {difficulty_rules}\n"
        "4. spoken_text 只说题面和引导语，不要直接说谜底、真相、答案或原因。\n"
        "5. spoken_text 用自然中文控制在 2 到 4 句，适合直接口播。\n"
        "6. subtitle_text 是题面的短摘要，尽量不超过 24 个字。\n"
        "7. 开局一律保持 clip=default、action=none、loop_playback=true、round_status=ongoing。\n"
    )


def parse_haiguitang_agent_reply(raw_reply: str) -> HaiGuiTangAgentDirective:
    reply_text = _clean_text(raw_reply)
    payload = _extract_json_payload(reply_text)
    if isinstance(payload, dict):
        return _directive_from_payload(payload, reply_text)
    return _directive_from_text(reply_text)


def _directive_from_payload(payload: dict[str, Any], raw_reply: str) -> HaiGuiTangAgentDirective:
    clip_source = payload.get("clip") or payload.get("expression") or payload.get("scene")
    action_source = payload.get("action") or payload.get("motion") or payload.get("gesture")
    spoken_text = _clean_text(
        payload.get("spoken_text")
        or payload.get("speech")
        or payload.get("spokenText")
        or payload.get("reply")
        or payload.get("text")
        or ""
    )
    subtitle_text = _clean_text(
        payload.get("subtitle_text")
        or payload.get("subtitle")
        or payload.get("caption")
        or payload.get("short_text")
        or payload.get("subtitleText")
        or ""
    )
    clip = _normalize_clip(clip_source)
    action = _normalize_action(action_source)
    round_status = _normalize_round_status(
        payload.get("round_status")
        or payload.get("game_status")
        or payload.get("status")
        or payload.get("roundState")
        or ""
    )
    mood = _clean_text(payload.get("mood") or payload.get("tone") or "")
    loop_value = payload.get("loop_playback")
    loop_playback = bool(loop_value) if isinstance(loop_value, bool) else clip == "default"

    if not spoken_text:
        spoken_text = subtitle_text or _clean_text(raw_reply)
    if not subtitle_text:
        subtitle_text = _truncate_text(spoken_text, DEFAULT_SUBTITLE_LIMIT)

    clip, action, loop_playback = _coerce_scene_controls(
        clip=clip,
        action=action,
        loop_playback=loop_playback,
        clip_explicit=bool(_clean_text(clip_source)),
        action_explicit=bool(_clean_text(action_source)),
        fallback_text=f"{spoken_text}\n{subtitle_text}",
    )
    return HaiGuiTangAgentDirective(
        spoken_text=spoken_text,
        subtitle_text=subtitle_text,
        clip=clip,
        action=action,
        loop_playback=loop_playback,
        round_status=round_status,
        mood=mood,
        parse_mode="json",
        raw_reply=_clean_text(raw_reply),
    )


def _directive_from_text(raw_reply: str) -> HaiGuiTangAgentDirective:
    spoken_text = _clean_text(raw_reply)
    clip, action, loop_playback = _infer_scene_controls(spoken_text)
    return HaiGuiTangAgentDirective(
        spoken_text=spoken_text,
        subtitle_text=_truncate_text(spoken_text, DEFAULT_SUBTITLE_LIMIT),
        clip=clip,
        action=action,
        loop_playback=loop_playback,
        round_status=_infer_round_status(spoken_text, clip=clip, action=action),
        mood="",
        parse_mode="heuristic",
        raw_reply=spoken_text,
    )


def _extract_json_payload(raw_reply: str) -> dict[str, Any] | None:
    for matcher in (_JSON_XML_TAG_RE, _JSON_CODE_BLOCK_RE):
        match = matcher.search(raw_reply)
        if match:
            candidate = str(match.group(1) or "").strip()
            parsed = _try_load_json(candidate)
            if isinstance(parsed, dict):
                return parsed

    inline_candidate = _extract_first_json_object(raw_reply)
    if inline_candidate:
        parsed = _try_load_json(inline_candidate)
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_first_json_object(text: str) -> str:
    start_index = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start_index < 0:
            if char == "{":
                start_index = index
                depth = 1
                in_string = False
                escaped = False
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return ""


def _try_load_json(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _normalize_clip(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    normalized = _CLIP_ALIASES.get(normalized, normalized)
    if normalized not in ALLOWED_HAIGUITANG_AGENT_CLIPS:
        return "default"
    return normalized


def _normalize_action(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    normalized = _ACTION_ALIASES.get(normalized, normalized)
    if normalized not in ALLOWED_HAIGUITANG_AGENT_ACTIONS:
        return "none"
    return normalized


def _normalize_difficulty(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED_HAIGUITANG_DIFFICULTIES:
        return "medium"
    return normalized


def _normalize_round_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED_HAIGUITANG_ROUND_STATUS:
        return "ongoing"
    return normalized


def _infer_round_status(text: str, *, clip: str, action: str) -> str:
    normalized_text = _clean_text(text)
    if clip == "outro":
        return "revealed"
    if any(keyword in normalized_text for keyword in ("答对", "猜对", "你说中了", "就是这样", "完全正确")):
        return "solved"
    if clip == "nod" and action == "nod" and any(keyword in normalized_text for keyword in ("正确", "没错")):
        return "solved"
    return "ongoing"


def _coerce_scene_controls(
    *,
    clip: str,
    action: str,
    loop_playback: bool,
    clip_explicit: bool,
    action_explicit: bool,
    fallback_text: str,
) -> tuple[str, str, bool]:
    resolved_clip = _normalize_clip(clip)
    resolved_action = _normalize_action(action)

    if resolved_clip == "default" and resolved_action in {"nod", "shake"}:
        resolved_clip = resolved_action
    elif resolved_clip in {"nod", "shake"} and resolved_action == "none":
        resolved_action = resolved_clip
    elif resolved_clip == "outro":
        resolved_action = "none"

    if resolved_clip == "default" and resolved_action == "none" and not clip_explicit and not action_explicit:
        inferred_clip, inferred_action, inferred_loop = _infer_scene_controls(fallback_text)
        if inferred_clip != "default" or inferred_action != "none":
            return inferred_clip, inferred_action, inferred_loop

    if resolved_clip == "default":
        return resolved_clip, resolved_action, True
    return resolved_clip, resolved_action, bool(loop_playback)


def _infer_scene_controls(text: str) -> tuple[str, str, bool]:
    normalized_text = _clean_text(text)
    for pattern in _OUTRO_PATTERNS:
        if pattern and pattern in normalized_text:
            return "outro", "none", False
    for pattern in _NEGATIVE_PATTERNS:
        if pattern and pattern in normalized_text:
            return "shake", "shake", False
    for pattern in _AFFIRMATIVE_PATTERNS:
        if pattern and pattern in normalized_text:
            return "nod", "nod", False
    return "default", "none", True


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _truncate_text(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= int(limit):
        return text
    return text[: max(0, int(limit) - 1)].rstrip() + "…"


__all__ = [
    "HaiGuiTangAgentDirective",
    "build_haiguitang_agent_prompt",
    "build_haiguitang_round_start_prompt",
    "parse_haiguitang_agent_reply",
]
