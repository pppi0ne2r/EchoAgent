"""Structured planning and continuation rules for offline crypto intents."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from echo_agent.crypto_budget import resolve_crypto_budget
from echo_agent.crypto_identity import premise_versions
from echo_agent.models import Intent, IntentStatus, Project


MAX_SAME_SCOPE_ATTEMPTS = 3


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "integer":
            return {"type": "integer", "value": str(int(value["value"], value["radix"]))}
        if value.get("type") == "bytes":
            return {"type": "bytes", "value": value["value"].lower(), "length": value["length"]}
        return {key: _canonical(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(child) for child in value]
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _range(scope: dict[str, Any]) -> tuple[int, int] | None:
    if "start" not in scope or "end" not in scope:
        return None
    def integer(value: Any) -> int | None:
        if type(value) is int:
            return value
        if isinstance(value, dict) and value.get("type") == "integer":
            return int(value["value"], value["radix"])
        return None
    start, end = integer(scope["start"]), integer(scope["end"])
    if start is None or end is None or start > end:
        return None
    return start, end


def _scope_base(scope: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in scope.items() if key not in {"start", "end"}}


def _semantic_key(project: Project, metadata: dict[str, Any], *, historical: bool = False) -> str | None:
    input_by_id = {item["id"]: item for item in project.crypto.get("inputs", [])}
    versions = metadata.get("input_versions") or {}
    inputs = [
        {"id": input_id,
         "sha256": versions.get(input_id, input_by_id[input_id])["sha256"],
         "size_bytes": versions.get(input_id, input_by_id[input_id])["size_bytes"]}
        for input_id in sorted(metadata.get("input_ids", []))
    ]
    versions_of_facts = metadata.get("premise_versions")
    if versions_of_facts is None:
        if historical and metadata.get("input_fact_ids"):
            # Old records cannot prove which mutable premises they consumed.
            return None
        versions_of_facts = premise_versions(project, metadata.get("input_fact_ids", []))
    return _digest({
        "method": metadata.get("method", "").strip().casefold(),
        "inputs": inputs,
        "premises": sorted(versions_of_facts.values()),
        "params": metadata.get("params") or {},
        "scope_constraints": _scope_base(metadata.get("scope") or {}),
        **({"verification_context": metadata.get("verification_context", "legacy")}
           if metadata.get("method") == "verify.candidate" else {}),
    })


def _budget_increased(new: dict[str, Any], old: dict[str, Any],
                      defaults: dict[str, Any] | None) -> bool:
    try:
        before = resolve_crypto_budget(defaults, old).to_dict()
        after = resolve_crypto_budget(defaults, new).to_dict()
    except ValueError:
        return False
    return all(after[key] >= before[key] for key in before) and any(
        after[key] > before[key] for key in before
    )


def validate_checkpoint_resume(project: Project, metadata: dict[str, Any]) -> None:
    """A byte-identical checkpoint is usable only for its original computation."""
    reference = metadata.get("resume_from")
    if reference is None:
        return
    producer_id = int(reference["path"].split("/")[2])
    producer = project.intents.get(producer_id)
    execution = _execution(project, producer_id)
    if (producer is None or producer.status != IntentStatus.FAILED or execution is None
            or execution.get("checkpoint_ref") != reference):
        raise ValueError("resume_from must be the recorded checkpoint of a failed producer intent")
    if "input_versions" not in producer.crypto or "premise_versions" not in producer.crypto:
        raise ValueError("checkpoint producer has no historical input/premise snapshot; replan without resume_from")
    old_key = _semantic_key(project, producer.crypto, historical=True)
    if old_key is None or old_key != _semantic_key(project, metadata):
        raise ValueError("checkpoint computation changed (inputs, premises, method or parameters); replan without resume_from")
    old_scope, new_scope = producer.crypto.get("scope") or {}, metadata.get("scope") or {}
    old_range, new_range = _range(old_scope), _range(new_scope)
    same_scope = (_canonical(old_scope) == _canonical(new_scope))
    if old_range is not None and new_range is not None:
        same_scope = old_range == new_range and _canonical(_scope_base(old_scope)) == _canonical(_scope_base(new_scope))
    if not same_scope:
        raise ValueError("checkpoint scope differs from the requested computation; replan without resume_from")


def _execution(project: Project, intent_id: int) -> dict[str, Any] | None:
    for record in reversed(project.crypto.get("executions", [])):
        if record.get("intent_id") == intent_id:
            return record
    return None


def _authorized_retry(project: Project, old: Intent, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    execution = _execution(project, old.id) or {}
    checkpoint = execution.get("checkpoint_ref")
    previous_checkpoint = old.crypto.get("resume_from") or {}
    same_state = (previous_checkpoint.get("sha256") == checkpoint.get("sha256")
                  and previous_checkpoint.get("size_bytes") == checkpoint.get("size_bytes")) if checkpoint else False
    if (checkpoint is not None and metadata.get("resume_from") == checkpoint and not same_state):
        return "checkpoint", {"prior_intent_id": old.id, "prior_exit_reason": execution.get("exit_reason")}
    old_budget = execution.get("budget") or old.crypto.get("budget") or {}
    if _budget_increased(metadata.get("budget") or {}, old_budget,
                         project.crypto.get("default_budget")):
        return "increased_budget", {"prior_intent_id": old.id, "prior_exit_reason": execution.get("exit_reason")}
    return None


def _retry_after_failures(project: Project, failures: list[Intent], metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not failures:
        return metadata
    if len(failures) >= MAX_SAME_SCOPE_ATTEMPTS:
        return None
    latest = max(failures, key=lambda item: item.id)
    retry = _authorized_retry(project, latest, metadata)
    if retry is None:
        return None
    # A changed budget must exceed every earlier attempt, including the first one.
    if retry[0] == "increased_budget" and any(
        not _budget_increased(metadata.get("budget") or {},
                              (_execution(project, old.id) or {}).get("budget") or old.crypto.get("budget") or {},
                              project.crypto.get("default_budget"))
        for old in failures
    ):
        return None
    result = copy.deepcopy(metadata)
    result["continuation"] = {"reason": retry[0], **retry[1]}
    return result


def _subtract(segments: list[tuple[int, int]], covered: tuple[int, int]) -> list[tuple[int, int]]:
    result = []
    for start, end in segments:
        if covered[1] < start or covered[0] > end:
            result.append((start, end))
            continue
        if start < covered[0]:
            result.append((start, covered[0] - 1))
        if covered[1] < end:
            result.append((covered[1] + 1, end))
    return result


def plan_crypto_intents(project: Project, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return executable scope fragments; an empty list means a redundant plan."""
    validate_checkpoint_resume(project, metadata)
    requested_scope = metadata["scope"]
    requested_range = _range(requested_scope)
    key = _semantic_key(project, metadata)
    matching = [intent for intent in project.intents.values()
                if intent.crypto and _semantic_key(project, intent.crypto, historical=True) == key]
    if requested_range is None:
        exact = [old for old in matching
                 if _canonical(old.crypto.get("scope")) == _canonical(requested_scope)]
        if any(old.status != IntentStatus.FAILED for old in exact):
            return []
        retry = _retry_after_failures(project, exact, metadata)
        return [retry] if retry is not None else []

    segments = [requested_range]
    covered_by: list[int] = []
    avoided_attempts: list[int] = []
    for old in matching:
        old_range = _range(old.crypto.get("scope") or {})
        if old_range is None:
            continue
        if old.status in {IntentStatus.ACTIVE, IntentStatus.RUNNING}:
            segments = _subtract(segments, old_range)
            covered_by.append(old.id)
            continue
        execution = _execution(project, old.id)
        if (old.status == IntentStatus.COMPLETED and execution
                and execution.get("exit_reason") in {"completed", "range_exhausted"}):
            covered = _range(execution.get("covered_scope") or {})
            if covered is not None and old_range[0] <= covered[0] <= covered[1] <= old_range[1]:
                segments = _subtract(segments, covered)
                covered_by.append(old.id)
                continue
        if old.status == IntentStatus.COMPLETED:
            # Missing or malformed coverage cannot justify trimming other ranges.
            if old_range == requested_range:
                return []
            continue
        if old.status == IntentStatus.FAILED and old_range != requested_range:
            if _authorized_retry(project, old, metadata) is None:
                # This is only a planning guard: a failed range is not mathematically exhausted.
                segments = _subtract(segments, old_range)
                avoided_attempts.append(old.id)
    planned = []
    for start, end in segments:
        fragment = copy.deepcopy(metadata)
        fragment["scope"] = {**_scope_base(requested_scope), "start": start, "end": end}
        exact_failures = [old for old in matching if old.status == IntentStatus.FAILED
                          and _range(old.crypto.get("scope") or {}) == (start, end)]
        fragment = _retry_after_failures(project, exact_failures, fragment)
        if fragment is None:
            continue
        validate_checkpoint_resume(project, fragment)
        if covered_by or avoided_attempts:
            fragment.setdefault("continuation", {})
            fragment["continuation"].update({
                "reason": "uncovered_range" if covered_by else "new_unattempted_range",
                "requested_scope": requested_scope,
                "covered_by_intent_ids": sorted(set(covered_by)),
                "avoided_failed_intent_ids": sorted(set(avoided_attempts)),
            })
        planned.append(fragment)
    return planned
