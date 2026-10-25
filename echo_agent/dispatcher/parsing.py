from __future__ import annotations

import json
import re
from typing import Literal

from echo_agent.dispatcher.types import ReasonResult, WorkerObservation, WorkerResult
from echo_agent.models import normalize_action_key

TEMPLATE_REASON_INTENTS: tuple[tuple[str, str], ...] = (
    ("check-upload", "分析是否存在文件上传漏洞"),
    ("enum-backup", "枚举 Web 目录和敏感备份文件"),
)
CRYPTO_TEMPLATE_REASON_INTENT = ("check-shared-factor", "对指定两个模数计算 gcd，并记录等式与输入来源")


class ReasonFormatError(ValueError):
    """reason 输出里没有可解析的 JSON 对象。

    这是模型输出格式问题，不应当累计成基础设施失败。
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def is_template_reason_intent(item: dict[str, object]) -> bool:
    description = " ".join(str(item.get("description") or "").split())
    action_key = normalize_action_key(item.get("action_key"))
    return (action_key, description) in (*TEMPLATE_REASON_INTENTS, CRYPTO_TEMPLATE_REASON_INTENT)


def parse_worker_result(output: str) -> WorkerResult:
    slices = extract_mode_json_outputs(output, "explore")
    if not slices:
        raise ValueError("explore output does not contain a JSON object")
    parsed: list[WorkerResult] = []
    rejected: list[str] = []
    for index, raw in enumerate(slices):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            rejected.append(f"object {index}: invalid JSON: {exc}")
            continue
        if not isinstance(data, dict):
            rejected.append(f"object {index}: JSON object required")
            continue
        if is_template_worker_payload(data):
            continue
        try:
            parsed.append(_worker_result_from_data(data))
        except ValueError as exc:
            # One malformed object must not discard the rest of the batch.
            rejected.append(f"object {index}: {exc}")
    if not parsed:
        if rejected:
            raise ValueError(
                "worker output objects were all rejected: " + "; ".join(rejected[:3])
            )
        return WorkerResult()
    meaningful = [item for item in parsed if item.fact_description or item.answer or item.outcome]
    if not meaningful:
        return WorkerResult(rejected=rejected)
    # A hypothesis is context for later steps, never the intent's final result. Keeping it
    # out of the "last wins" slot preserves the real observation even when the model emits
    # the hypothesis last (the shape that used to discard a whole batch).
    last_index = _last_primary_index(meaningful)
    last = meaningful[last_index]
    priors = [
        WorkerObservation(
            answer=item.answer,
            fact_description=item.fact_description,
            outcome=item.outcome,
            crypto=item.crypto,
        )
        for index, item in enumerate(meaningful)
        if index != last_index and (item.fact_description or item.answer)
    ]
    return WorkerResult(
        answer=last.answer,
        fact_description=last.fact_description,
        outcome=last.outcome,
        prior_results=priors,
        crypto=last.crypto,
        rejected=rejected,
    )


def _last_primary_index(items: list[WorkerResult]) -> int:
    for index in range(len(items) - 1, -1, -1):
        if not _is_crypto_hypothesis(items[index]):
            return index
    # Only hypotheses were emitted: keep the last one so it is still recorded as pending,
    # rather than silently dropping the model's output.
    return len(items) - 1


def _is_crypto_hypothesis(item: WorkerResult) -> bool:
    fact = item.crypto.get("fact") if isinstance(item.crypto, dict) else None
    return isinstance(fact, dict) and fact.get("claim_status") == "hypothesis"


def _worker_result_from_data(data: dict[str, object]) -> WorkerResult:
    fact_description = _optional_fact_description(data.get("fact_description"))
    answer = _optional_answer(data.get("answer"))
    if answer is None and fact_description is None and "outcome" not in data:
        return WorkerResult()
    return WorkerResult(
        answer=answer,
        fact_description=fact_description,
        outcome=_optional_worker_outcome(data.get("outcome")),
        crypto=_optional_crypto_object(data.get("crypto")),
    )


def parse_reason_result(output: str) -> ReasonResult:
    data = _parse_json_object(output, mode="reason")
    completed = data.get("completed", False)
    if not isinstance(completed, bool):
        raise ValueError("reason output completed must be a boolean")
    intents = _parse_reason_intents(data)
    kept = [item for item in intents if not is_template_reason_intent(item)]
    if intents and not kept and not completed:
        # The tail is where the real upstream/process error usually is; the head is prompt echo.
        excerpt = " ".join(output.split())[-400:]
        raise ValueError(
            "reason output only contains prompt template intents; raw output excerpt: "
            + (excerpt or "<empty>")
        )
    return ReasonResult(
        completed=completed,
        next_intent_descriptions=[str(item["description"]) for item in kept],
        next_intents=kept,
        answer=_optional_answer(data.get("answer")),
        stop_reason=_optional_reason_text(data.get("stop_reason"), "stop_reason"),
        raw_output=output,
    )


def _parse_json_object(output: str, mode: Literal["explore", "reason"]) -> dict[str, object]:
    matches = _matching_json_objects(output, mode)
    if not matches:
        if mode == "reason":
            raise ReasonFormatError("reason output does not contain a JSON object", output=output)
        raise ValueError(f"{mode} output does not contain a JSON object")
    data = matches[-1]
    if not isinstance(data, dict):
        raise ValueError(f"{mode} output must be a JSON object")
    return data


def _matching_json_objects(output: str, mode: Literal["explore", "reason"]) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for text in _candidate_texts(output):
        matches.extend(_scan_matching_objects(text, mode))
    return matches


def _candidate_texts(output: str) -> list[str]:
    texts = [output]
    decoder = json.JSONDecoder()
    seen: set[str] = set()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        for text in _event_texts(value):
            if text not in seen:
                seen.add(text)
                texts.append(text)
    return texts


def _event_texts(value: dict[str, object]) -> list[str]:
    texts: list[str] = []
    item = value.get("item")
    containers: list[dict[str, object]] = [value]
    if isinstance(item, dict):
        containers.append(item)
    for container in containers:
        for key in ("text", "message", "content"):
            nested = container.get(key)
            if isinstance(nested, str) and nested.strip():
                texts.append(nested)
    return texts


def _scan_matching_objects(output: str, mode: Literal["explore", "reason"]) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    matches: list[dict[str, object]] = []
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and _matches_mode(value, mode):
            matches.append(value)
    return matches


def _matches_mode(value: dict[str, object], mode: Literal["explore", "reason"]) -> bool:
    if mode == "reason":
        return "completed" in value and (
            "next_intents" in value or "next_intent_descriptions" in value
        )
    return "answer" in value or "fact_description" in value or ("outcome" in value and "crypto" in value)


def extract_mode_json_output(output: str, mode: Literal["explore", "reason"]) -> str | None:
    found = extract_mode_json_outputs(output, mode)
    if not found:
        return None
    return found[-1]


def extract_mode_json_outputs(output: str, mode: Literal["explore", "reason"]) -> list[str]:
    found: list[str] = []
    _collect_mode_json_slices(output, mode, seen=set(), found=found)
    return found


def _collect_mode_json_slices(
    output: str,
    mode: Literal["explore", "reason"],
    seen: set[str],
    found: list[str],
) -> None:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(output):
        brace = output.find("{", index)
        if brace < 0:
            return
        try:
            value, end = decoder.raw_decode(output[brace:])
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if not isinstance(value, dict):
            index = brace + 1
            continue
        before = len(found)
        for nested in _event_texts(value):
            if nested in seen:
                continue
            seen.add(nested)
            _collect_mode_json_slices(nested, mode, seen, found)
        if len(found) == before and _matches_mode(value, mode):
            found.append(output[brace : brace + end])
        index = brace + end


def _parse_reason_intents(data: dict[str, object]) -> list[dict[str, object]]:
    raw_items = data.get("next_intents")
    if raw_items is None:
        raw_items = data.get("next_intent_descriptions") or []
    if not isinstance(raw_items, list):
        raise ValueError("reason output next_intents must be a list")

    intents: list[dict[str, object]] = []
    for item in raw_items:
        if isinstance(item, str):
            description = item.strip()
            if description:
                intents.append({"description": description, "from": None, "to": None, "action_key": ""})
            continue
        if not isinstance(item, dict):
            raise ValueError("reason output next_intents items must be strings or objects")
        description = _optional_str(item.get("description"))
        if not description:
            raise ValueError("reason output next_intents object description is required")
        standard = {"description", "from", "to", "action_key", "crypto"}
        extra = {key: value for key, value in item.items() if key not in standard}
        intents.append(
            {
                "description": description,
                "from": _optional_int(item.get("from")),
                "to": _optional_int(item.get("to")),
                "action_key": _optional_reason_text(item.get("action_key"), "action_key"),
                **({"crypto": _optional_crypto_object(item["crypto"])} if "crypto" in item else {}),
                # Keep misplaced top-level fields (for example budget) so the dispatcher can
                # either migrate them with a warning or reject them explicitly.  Silently
                # dropping them made "increase the budget" plans revert to the default limit.
                **extra,
            }
        )
    return intents


def _optional_crypto_object(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("crypto output metadata must be a JSON object")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer field must be an integer or null")
    return value


def _optional_reason_text(value: object, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"reason output {field_name} must be a string or null")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_answer(value: object) -> str | None:
    text = _optional_str(value)
    if text in {"真实 flag 字符串", "实际获得的完整 flag", "实际获得的完整 flag；不要照抄本句", "最终答案", "answer"}:
        return None
    return text


def _optional_worker_outcome(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"verified", "refuted", "error"}:
        return text
    if text in {"inconclusive", "hypothesis"}:
        # Trust is carried by crypto.fact.claim_status; these words mean "not established",
        # which is exactly the error outcome. Accepting them keeps a hypothesis object from
        # discarding the rest of the batch.
        return "error"
    raise ValueError("worker output outcome must be verified, refuted or error")


def _optional_fact_description(value: object) -> str | None:
    text = _optional_str(value)
    if text is None:
        return None
    normalized = " ".join(text.split()).lower()
    placeholder_facts = {
        "初始执行得到的新事实",
        "新的事实描述",
        "新事实描述",
        "事实描述",
        "fact description",
        "new fact",
        "new fact description",
        "写入你实际验证得到的具体事实；不要照抄本句",
        "写入你本次 intent 实际验证得到的具体事实；不要照抄本句",
        "该方向已验证不存在可利用点",
        "示例事实；不要照抄",
    }
    if normalized in placeholder_facts:
        return None
    return text


def repair_reason_output(output: str) -> str | None:
    """把围栏或尾逗号修成可解析的 reason JSON。修不好就返回 None，不重跑规划。"""
    text = output.strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        parse_reason_result(text)
    except (ReasonFormatError, ValueError, json.JSONDecodeError):
        return None
    return text


def strip_result_json_text(text: str) -> str:
    """去掉 explore/reason 结果 JSON，只保留原始执行输出。"""
    cleaned = text
    decoder = json.JSONDecoder()
    index = 0
    while index < len(cleaned):
        brace = cleaned.find("{", index)
        if brace < 0:
            break
        try:
            value, end = decoder.raw_decode(cleaned[brace:])
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(value, dict) and (
            _matches_mode(value, "explore") or _matches_mode(value, "reason")
        ):
            cleaned = f"{cleaned[:brace]} {cleaned[brace + end:]}"
            index = brace
            continue
        index = brace + end
    return cleaned


def is_template_worker_payload(data: dict[str, object]) -> bool:
    if data.get("template") is True:
        return True
    crypto = data.get("crypto")
    if isinstance(crypto, dict) and crypto.get("exit_reason"):
        return False
    if _optional_fact_description(data.get("fact_description")) is not None:
        return False
    if _optional_answer(data.get("answer")) is not None:
        return False
    return "answer" in data or "fact_description" in data or "outcome" in data


def substantive_worker_output(output: str) -> str | None:
    kept: list[str] = []
    for raw in extract_mode_json_outputs(output, "explore"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or is_template_worker_payload(data):
            continue
        try:
            result = _worker_result_from_data(data)
        except ValueError:
            continue
        if result.fact_description or result.answer:
            kept.append(raw)
    if not kept:
        return None
    return "\n".join(kept)


def strip_prompt_template_text(text: str) -> str:
    cleaned = text
    decoder = json.JSONDecoder()
    index = 0
    while index < len(cleaned):
        brace = cleaned.find("{", index)
        if brace < 0:
            break
        try:
            value, end = decoder.raw_decode(cleaned[brace:])
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(value, dict) and is_template_worker_payload(value):
            cleaned = f"{cleaned[:brace]} {cleaned[brace + end:]}"
            index = brace
            continue
        index = brace + end
    for phrase in (
        "该方向已验证不存在可利用点",
        "写入你实际验证得到的具体事实；不要照抄本句",
        "写入你本次 intent 实际验证得到的具体事实；不要照抄本句",
        "初始执行得到的新事实",
        "示例事实；不要照抄",
    ):
        cleaned = cleaned.replace(phrase, " ")
    return " ".join(cleaned.split())
