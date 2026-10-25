"""Shared rules for deciding whether a saved fact supports a final answer."""

from __future__ import annotations

from typing import Any

PARTIAL_OUTPUT_PREFIX = "未完成执行的输出摘录："
ANSWER_WRAPPER_PREFIX = "agent reported possible final answer:"


def fact_kind_value(fact: Any) -> str:
    kind = getattr(fact, "kind", None)
    return str(getattr(kind, "value", kind) or "")


def answer_source_line(description: str) -> str:
    for line in description.splitlines():
        if line.startswith("source:"):
            return line.split(":", 1)[1].strip()
    return ""


def fact_supports_answer(fact: Any, answer: str) -> bool:
    """True only when the answer appears in an evidence fact, not a note or excerpt."""
    if fact_kind_value(fact) != "evidence":
        return False
    description = str(getattr(fact, "description", ""))
    if answer not in description:
        return False
    if description.startswith(PARTIAL_OUTPUT_PREFIX):
        return False
    if description.startswith(ANSWER_WRAPPER_PREFIX):
        source = answer_source_line(description)
        return bool(source and answer in source)
    return True
