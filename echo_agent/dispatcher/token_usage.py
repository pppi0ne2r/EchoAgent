from __future__ import annotations

import re
from typing import Any


_TOKENS_USED = re.compile(
    r"tokens used\s*(?::\s*)?(?:[\r\n]+\s*)?([0-9][0-9,]*)",
    re.IGNORECASE,
)
_MODE = re.compile(r"^[a-z0-9_]{1,32}$")

SESSION_READER_SCRIPT = r"""
import json
import os
import sys


def main() -> None:
    root = sys.argv[1]
    newest = None
    newest_mtime = -1.0
    if os.path.isdir(root):
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime >= newest_mtime:
                    newest = path
                    newest_mtime = mtime
    if newest is None:
        return
    session_id = ""
    usage = None
    with open(newest, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "session_meta":
                found = payload.get("id")
                if isinstance(found, str) and found.strip():
                    session_id = found.strip()
            if obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    usage = total
    if usage is None:
        return
    print(json.dumps({
        "session_id": session_id,
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
"""


def parse_tokens_used_total(text: str) -> int | None:
    matches = _TOKENS_USED.findall(text or "")
    if not matches:
        return None
    value = int(matches[-1].replace(",", ""))
    return value if value > 0 else None


def _count(value: object, *, default: int = 0) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None
    if value < 0:
        return None
    return value


def build_token_usage(
    *,
    mode: str,
    intent_id: int | None,
    model: str,
    source: str,
    session_id: str,
    input_tokens: object,
    cached_input_tokens: object,
    output_tokens: object,
    reasoning_output_tokens: object,
    total_tokens: object,
) -> dict[str, Any] | None:
    cleaned_mode = mode.strip()
    if not _MODE.fullmatch(cleaned_mode):
        return None
    if source not in {"session", "text"}:
        return None
    if intent_id is not None and (isinstance(intent_id, bool) or not isinstance(intent_id, int) or intent_id < 0):
        return None
    counts = {
        "input_tokens": _count(input_tokens),
        "cached_input_tokens": _count(cached_input_tokens),
        "output_tokens": _count(output_tokens),
        "reasoning_output_tokens": _count(reasoning_output_tokens),
        "total_tokens": _count(total_tokens, default=-1),
    }
    if any(value is None for value in counts.values()):
        return None
    total = counts["total_tokens"]
    if total is None or total <= 0:
        return None
    cleaned_session = session_id.strip()[:128]
    if source == "text":
        cleaned_session = ""
    return {
        "mode": cleaned_mode,
        "intent_id": intent_id,
        "model": (model or "").strip()[:120],
        "session_id": cleaned_session,
        "source": source,
        "input_tokens": counts["input_tokens"],
        "cached_input_tokens": counts["cached_input_tokens"],
        "output_tokens": counts["output_tokens"],
        "reasoning_output_tokens": counts["reasoning_output_tokens"],
        "total_tokens": total,
    }


def normalize_token_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    intent_id = payload.get("intent_id")
    if intent_id is None or intent_id == "":
        parsed_intent: int | None = None
    elif isinstance(intent_id, int) and not isinstance(intent_id, bool):
        parsed_intent = intent_id
    else:
        return None
    return build_token_usage(
        mode=str(payload.get("mode") or ""),
        intent_id=parsed_intent,
        model=str(payload.get("model") or ""),
        source=str(payload.get("source") or ""),
        session_id=str(payload.get("session_id") or ""),
        input_tokens=payload.get("input_tokens", 0),
        cached_input_tokens=payload.get("cached_input_tokens", 0),
        output_tokens=payload.get("output_tokens", 0),
        reasoning_output_tokens=payload.get("reasoning_output_tokens", 0),
        total_tokens=payload.get("total_tokens", 0),
    )


def usage_from_session_payload(
    payload: dict[str, Any] | None,
    *,
    mode: str,
    intent_id: int | None,
    model: str,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return build_token_usage(
        mode=mode,
        intent_id=intent_id,
        model=model,
        source="session",
        session_id=str(payload.get("session_id") or ""),
        input_tokens=payload.get("input_tokens", 0),
        cached_input_tokens=payload.get("cached_input_tokens", 0),
        output_tokens=payload.get("output_tokens", 0),
        reasoning_output_tokens=payload.get("reasoning_output_tokens", 0),
        total_tokens=payload.get("total_tokens", 0),
    )


def usage_from_text_total(
    total: int | None,
    *,
    mode: str,
    intent_id: int | None,
    model: str,
) -> dict[str, Any] | None:
    if total is None:
        return None
    return build_token_usage(
        mode=mode,
        intent_id=intent_id,
        model=model,
        source="text",
        session_id="",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        total_tokens=total,
    )
