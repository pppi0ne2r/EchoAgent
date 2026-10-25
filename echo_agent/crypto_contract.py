"""Validation for the optional, versioned cryptography data contract."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-fA-F]*\Z")
# Algorithms the independent verifier can compute; also the allowed answer_form=hash choices.
HASH_ALGORITHMS = frozenset({"md5", "sha1", "sha224", "sha256", "sha384", "sha512",
                             "sha3_256", "sha3_512", "blake2b", "blake2s"})
ANSWER_FORMS = ("literal", "hash")


def relative_path(value: object, prefix: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("crypto file path must be a project-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("crypto file path escapes its project directory")
    if len(path.parts) < 2 or path.parts[0] != prefix:
        raise ValueError(f"crypto file path must start with {prefix}/")
    return value


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON values") from exc


def _ids(value: object, label: str, kind: type) -> list[Any]:
    if not isinstance(value, list) or any(type(item) is not kind for item in value):
        raise ValueError(f"{label} must be a list of {kind.__name__} values")
    if any(item == "" or (kind is int and item < 0) for item in value):
        raise ValueError(f"{label} contains an invalid id")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate ids")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("crypto file sha256 must be 64 lowercase hex digits")
    return value


def _file_size(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("crypto file size_bytes must be a non-negative integer")
    return value


def input_ref(value: object) -> dict[str, Any]:
    item = _json_object(value, "input reference")
    if not isinstance(item.get("id"), str) or not item["id"].strip():
        raise ValueError("input reference needs a non-empty id")
    if not isinstance(item.get("role"), str) or not item["role"].strip():
        raise ValueError("input reference needs a non-empty role")
    relative_path(item.get("path"), "inputs")
    _sha(item.get("sha256"))
    _file_size(item.get("size_bytes"))
    if "media_type" in item and not isinstance(item["media_type"], str):
        raise ValueError("input media_type must be a string")
    if "filename" in item and (not isinstance(item["filename"], str) or not item["filename"]):
        raise ValueError("input filename must be a non-empty string")
    if "number_radix" in item and (type(item["number_radix"]) is not int or item["number_radix"] not in {10, 16}):
        raise ValueError("input number_radix must be 10 or 16")
    if "byte_order" in item and (not isinstance(item["byte_order"], str) or item["byte_order"] not in {"big", "little"}):
        raise ValueError("input byte_order must be big or little")
    if "byte_length" in item and (type(item["byte_length"]) is not int or item["byte_length"] < 0):
        raise ValueError("input byte_length must be a non-negative integer")
    return item


def artifact_ref(value: object) -> dict[str, Any]:
    item = _json_object(value, "artifact reference")
    try:
        path = relative_path(item.get("path"), "artifacts")
    except ValueError as exc:
        raise ValueError(
            f"{exc}; artifact path must be artifacts/intents/<id>/<file>, "
            "not /home/kali/workspace/intents/<id>/<file>"
        ) from exc
    parts = PurePosixPath(path).parts
    if len(parts) < 4 or parts[1] != "intents" or not parts[2].isdigit():
        raise ValueError("artifact path must be artifacts/intents/<id>/<file>")
    _sha(item.get("sha256"))
    _file_size(item.get("size_bytes"))
    if "producer_intent_id" in item:
        producer = item["producer_intent_id"]
        if type(producer) is not int or producer != int(parts[2]):
            raise ValueError("artifact producer does not match its intent directory")
    return item


def _typed_values(value: Any, path: str = "crypto") -> None:
    if isinstance(value, dict):
        if "type" in value:
            if value["type"] == "integer":
                radix, digits = value.get("radix"), value.get("value")
                if type(radix) is not int or radix not in {10, 16} or not isinstance(digits, str):
                    raise ValueError(f"{path}: typed integer needs a radix and string value")
                try:
                    int(digits, radix)
                except ValueError as exc:
                    raise ValueError(f"{path}: invalid typed integer") from exc
            elif value["type"] == "bytes":
                digits, length = value.get("value"), value.get("length")
                if value.get("encoding") != "hex" or not isinstance(digits, str):
                    raise ValueError(
                        f'{path}: typed bytes need a hexadecimal string; '
                        'expected {"type":"bytes","encoding":"hex","value":"0007","length":2} '
                        '(replace the example value and length; do not use a hex field)'
                    )
                if not _HEX.fullmatch(digits) or len(digits) % 2 or type(length) is not int or length != len(digits) // 2:
                    raise ValueError(f"{path}: typed bytes length or hex value is invalid")
            else:
                raise ValueError(f"{path}: unsupported crypto typed value")
        else:
            for key, child in value.items():
                _typed_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _typed_values(child, f"{path}[{index}]")
    elif isinstance(value, float):
        raise ValueError(f"{path}: crypto values must not be floating point")


def crypto_goal(value: object) -> dict[str, Any]:
    """Task-owner completion requirements; never infer them from solver output."""
    goal = _json_object(value, "crypto goal")
    allowed = {"answer_kind", "solution_policy", "answer_encoding", "byte_length", "byte_order",
               "verification_mode", "answer_form", "answer_hash"}
    if set(goal) - allowed:
        raise ValueError("unsupported crypto goal fields: " + ", ".join(sorted(set(goal) - allowed)))
    goal.setdefault("answer_kind", "plaintext")
    goal.setdefault("solution_policy", "unique")
    # answer_form is intentionally not defaulted: adding a key would change goal_sha256 for
    # every existing project and invalidate stored verification/acceptance records.  Readers
    # treat a missing answer_form as "literal".
    if not isinstance(goal["answer_kind"], str) or goal["answer_kind"] not in {"plaintext", "factors", "private_exponent"}:
        raise ValueError("crypto goal answer_kind must be plaintext, factors or private_exponent")
    if not isinstance(goal["solution_policy"], str) or goal["solution_policy"] not in {"unique", "any"}:
        raise ValueError("crypto goal solution_policy must be unique or any; complete solution sets are not supported")
    if "answer_form" in goal and (not isinstance(goal["answer_form"], str) or goal["answer_form"] not in ANSWER_FORMS):
        raise ValueError("crypto goal answer_form must be literal or hash")
    if goal.get("answer_form") == "hash":
        if not isinstance(goal.get("answer_hash"), str) or goal["answer_hash"] not in HASH_ALGORITHMS:
            raise ValueError("crypto goal answer_hash must name a supported hash algorithm: "
                             + ", ".join(sorted(HASH_ALGORITHMS)))
        if goal["answer_kind"] != "plaintext":
            raise ValueError("a hashed answer requires answer_kind=plaintext; the plaintext stays in the candidate")
    elif "answer_hash" in goal:
        raise ValueError("crypto goal answer_hash requires answer_form=hash")
    if "verification_mode" in goal and (
            not isinstance(goal["verification_mode"], str)
            or goal["verification_mode"] not in {"strict", "evidence", "acceptance"}):
        raise ValueError("crypto goal verification_mode must be strict, evidence or acceptance")
    if "answer_encoding" in goal and (not isinstance(goal["answer_encoding"], str) or goal["answer_encoding"] not in {"decimal", "hex", "utf8"}):
        raise ValueError("crypto goal answer_encoding must be decimal, hex or utf8")
    if goal.get("answer_form") == "hash":
        # The answer string itself is the digest text.  byte_length/byte_order still describe
        # the recovered plaintext (the hash preimage), so they remain valid below.
        if goal.get("answer_encoding", "utf8") != "utf8":
            raise ValueError("a hashed answer requires answer_encoding=utf8 (the digest text itself)")
        goal.setdefault("answer_encoding", "utf8")
    if goal["answer_kind"] != "plaintext" and any(key in goal for key in ("byte_length", "byte_order")):
        raise ValueError("byte conversion is only supported for plaintext goals")
    if goal["answer_kind"] != "plaintext" and goal.get("answer_encoding", "decimal") != "decimal":
        raise ValueError("RSA factor and private exponent goals require decimal answer_encoding")
    if "byte_length" in goal and (type(goal["byte_length"]) is not int or not 1 <= goal["byte_length"] <= 4096):
        raise ValueError("crypto goal byte_length must be an integer from 1 to 4096")
    if "byte_order" in goal and (not isinstance(goal["byte_order"], str) or goal["byte_order"] not in {"big", "little"}):
        raise ValueError("crypto goal byte_order must be big or little")
    if any(key in goal for key in ("byte_length", "byte_order")) and goal.get("answer_encoding") not in {"hex", "utf8"}:
        raise ValueError("crypto goal byte conversion requires hex or utf8 answer_encoding")
    return goal


def acceptance_record(value: object) -> dict[str, Any]:
    item = _json_object(value, "crypto acceptance")
    if type(item.get("candidate_fact_id")) is not int or item["candidate_fact_id"] < 0:
        raise ValueError("crypto acceptance candidate_fact_id must be a non-negative integer")
    _sha(item.get("candidate_sha256"))
    _sha(item.get("goal_sha256"))
    if not isinstance(item.get("answer"), str) or not item["answer"]:
        raise ValueError("crypto acceptance answer must be a non-empty string")
    for key, limit in (("accepted_at", 64), ("operator", 200)):
        if not isinstance(item.get(key), str) or not item[key] or len(item[key]) > limit:
            raise ValueError(f"crypto acceptance {key} must be a non-empty string")
    if "note" in item and (not isinstance(item["note"], str) or len(item["note"]) > 2000):
        raise ValueError("crypto acceptance note must be a string of at most 2000 characters")
    return item


def project_crypto(value: object) -> dict[str, Any]:
    item = _json_object(value, "project crypto metadata")
    if not item:
        return item
    if item.get("schema_version") != 1:
        raise ValueError("unsupported crypto schema_version")
    if "require_platform_acceptance" in item and type(item["require_platform_acceptance"]) is not bool:
        raise ValueError("require_platform_acceptance must be a boolean")
    if "goal" in item:
        item["goal"] = crypto_goal(item["goal"])
    inputs = item.get("inputs", [])
    if not isinstance(inputs, list):
        raise ValueError("crypto inputs must be a list")
    item["inputs"] = [input_ref(entry) for entry in inputs]
    _ids([entry["id"] for entry in item["inputs"]], "crypto input ids", str)
    for key in ("default_budget",):
        if key in item and item[key] is not None:
            _json_object(item[key], key)
    for key in ("executions", "verifications"):
        if key in item:
            if not isinstance(item[key], list):
                raise ValueError(f"crypto {key} must be a list")
            for record in item[key]:
                _json_object(record, key)
                _typed_values(record)
                for id_key in ("intent_id", "candidate_fact_id"):
                    if id_key in record and (type(record[id_key]) is not int or record[id_key] < 0):
                        raise ValueError(f"{id_key} must be a non-negative integer")
                if "artifact_refs" in record:
                    if not isinstance(record["artifact_refs"], list):
                        raise ValueError("record artifact_refs must be a list")
                    record["artifact_refs"] = [artifact_ref(ref) for ref in record["artifact_refs"]]
                if record.get("checkpoint_ref") is not None:
                    record["checkpoint_ref"] = artifact_ref(record["checkpoint_ref"])
                    if "intent_id" in record and int(record["checkpoint_ref"]["path"].split("/")[2]) != record["intent_id"]:
                        raise ValueError("execution checkpoint must belong to its intent")
    if "acceptances" in item:
        if not isinstance(item["acceptances"], list):
            raise ValueError("crypto acceptances must be a list")
        item["acceptances"] = [acceptance_record(entry) for entry in item["acceptances"]]
    return item


def fact_crypto(value: object) -> dict[str, Any]:
    item = _json_object(value, "fact crypto metadata")
    if "observation_kind" in item and (not isinstance(item["observation_kind"], str) or item["observation_kind"] not in {"evidence", "refuted"}):
        raise ValueError("observation_kind must be evidence or refuted")
    if "claim_status" in item and (not isinstance(item["claim_status"], str) or item["claim_status"] not in {"observed", "hypothesis", "candidate"}):
        raise ValueError("claim_status must be observed, hypothesis or candidate")
    if "candidate" in item:
        candidate = _json_object(item["candidate"], "candidate")
        _typed_values(candidate)
        if not isinstance(candidate.get("answer"), str) or not candidate["answer"]:
            raise ValueError("candidate answer must be a nonempty string")
    if "source_input_ids" in item:
        _ids(item["source_input_ids"], "source_input_ids", str)
    if "depends_on_fact_ids" in item:
        _ids(item["depends_on_fact_ids"], "depends_on_fact_ids", int)
    if "producer_intent_id" in item and (type(item["producer_intent_id"]) is not int or item["producer_intent_id"] < 0):
        raise ValueError("producer_intent_id must be a non-negative integer")
    if "typed_value" in item:
        _typed_values(item["typed_value"])
    if "artifact_refs" in item:
        if not isinstance(item["artifact_refs"], list):
            raise ValueError("artifact_refs must be a list")
        item["artifact_refs"] = [artifact_ref(ref) for ref in item["artifact_refs"]]
    return item


def intent_crypto(value: object) -> dict[str, Any]:
    item = _json_object(value, "intent crypto metadata")
    if "input_fact_ids" in item:
        _ids(item["input_fact_ids"], "input_fact_ids", int)
    if "input_ids" in item:
        _ids(item["input_ids"], "input_ids", str)
    if "input_versions" in item:
        versions = _json_object(item["input_versions"], "input_versions")
        for version in versions.values():
            version = _json_object(version, "input version")
            _sha(version.get("sha256"))
            _file_size(version.get("size_bytes"))
        if set(versions) != set(item.get("input_ids", [])):
            raise ValueError("input_versions must match input_ids")
    if "premise_versions" in item:
        versions = _json_object(item["premise_versions"], "premise_versions")
        if set(versions) != {str(fact_id) for fact_id in item.get("input_fact_ids", [])}:
            raise ValueError("premise_versions must match input_fact_ids")
        for version in versions.values():
            _sha(version)
    if "method" in item and not isinstance(item["method"], str):
        raise ValueError("intent method must be a string")
    for key in ("params", "scope", "budget"):
        if key in item and item[key] is not None:
            _typed_values(_json_object(item[key], key), f"crypto.{key}")
    if "resume_from" in item and item["resume_from"] is not None:
        item["resume_from"] = artifact_ref(item["resume_from"])
    return item
