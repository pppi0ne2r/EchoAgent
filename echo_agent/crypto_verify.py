"""Host-side, deterministic checks for offline crypto answer candidates.

The solver never supplies a verification status.  This module reads the saved
input bytes again and returns a reproducible record for the completion gate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .crypto_files import CryptoProjectFiles
from .crypto_contract import HASH_ALGORITHMS, crypto_goal
from .crypto_inputs import extract_dsa, extract_rsa, extract_rsa_aes, looks_like_rsa_aes
from .crypto_mt19937 import extract_mt19937, looks_like_mt19937, replay_mt19937
from .crypto_verification import (VerificationCancelled, VerificationTimeout,
                                  check_verification, verification_scope)
from .evidence import fact_supports_answer
from .models import FactKind, Project


VERSION = "crypto-verify-v8"
MAX_VERIFY_INPUT_BYTES = 1024 * 1024
MAX_RSA_BITS = 4096
_RSA_LINE = re.compile(r"(n|e|c|p|q)=(0x[0-9a-fA-F]+|[0-9]+)\Z")
_DECIMAL = re.compile(r"[0-9]+\Z")
_HEX = re.compile(r"[0-9a-fA-F]*\Z")
_HASH_ALGORITHMS = HASH_ALGORITHMS
_SUPPORTED_SCHEMES = {"rsa", "rsa-aes", "aes", "dsa", "ecdsa", "hash", "xor", "encoding",
                      "mt19937"}
_EC_CURVES: dict[str, dict[str, int]] = {
    "secp256k1": {
        "p": 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F,
        "a": 0,
        "b": 7,
        "gx": 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
        "gy": 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
        "n": 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141,
    },
    "secp256r1": {
        "p": 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF,
        "a": 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC,
        "b": 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
        "gx": 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
        "gy": 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
        "n": 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
    },
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def candidate_digest(fact: Any) -> str:
    return _digest(fact.to_dict())


def _number(value: object, name: str) -> int:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or len(value) > 1234:
        raise ValueError(f"{name} must be a decimal integer string")
    return int(value)


def _hex(value: object, name: str) -> bytes:
    if not isinstance(value, str) or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", value):
        raise ValueError(f"{name} must be an even-length hex string")
    return bytes.fromhex(value)


def _rsa_input(raw: bytes, required: set[str] | None = None) -> dict[str, int]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("RSA input must be ASCII n/e/c lines") from exc
    values: dict[str, int] = {}
    for line in lines:
        match = _RSA_LINE.fullmatch(line.strip())
        if match is None or match[1] in values:
            raise ValueError("RSA input needs unique n=, e=, c=, p=, q= lines")
        values[match[1]] = int(match[2], 0 if match[2].startswith("0x") else 10)
    if (not (required or {"n", "e", "c"}).issubset(values) or values["n"] <= 1
            or ("e" in values and values["e"] <= 1)
            or ("c" in values and not 0 <= values["c"] < values["n"])):
        raise ValueError("invalid RSA n/e/c values")
    if ("p" in values) != ("q" in values):
        raise ValueError("RSA input needs p and q together")
    if "p" in values:
        p, q = values["p"], values["q"]
        if p <= 1 or q <= 1 or p == q or p * q != values["n"]:
            raise ValueError("invalid RSA input factors")
        if p.bit_length() > MAX_RSA_BITS or q.bit_length() > MAX_RSA_BITS:
            raise ValueError("RSA input factor size limit exceeded")
    return values


def _probably_prime(value: int) -> bool:
    if value < 2:
        return False
    for divisor in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if value % divisor == 0:
            return value == divisor
    d, shift = value - 1, 0
    while d % 2 == 0:
        d //= 2
        shift += 1
    bases = (2, 325, 9375, 28178, 450775, 9780504, 1795265022) if value < 2**64 else (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for base in bases:
        check_verification()
        if base % value == 0:
            continue
        residue = pow(base, d, value)
        if residue in (1, value - 1):
            continue
        if all(pow(residue, 2**index, value) != value - 1 for index in range(1, shift)):
            return False
    return True


def _manifest(inputs: list[tuple[str, bytes]]) -> dict[str, Any]:
    if len(inputs) != 1:
        raise ValueError("this verification scheme requires one original JSON manifest")
    try:
        data = json.loads(inputs[0][1].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("this verification scheme needs a JSON manifest") from exc
    if not isinstance(data, dict):
        raise ValueError("verification manifest must be a JSON object")
    return data


def _manifest_int(data: dict[str, Any], key: str, *, required: bool = False) -> int | None:
    if key not in data:
        if required:
            raise ValueError(f"verification manifest needs {key}")
        return None
    value = data[key]
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 1234:
        if _DECIMAL.fullmatch(value):
            return int(value)
        if value[:2].lower() == "0x" and _HEX.fullmatch(value[2:]):
            return int(value, 16)
    raise ValueError(f"verification manifest field {key} must be a decimal or hex integer")


def _candidate_int(candidate: dict[str, Any], fact: Any, *, allow_answer: bool = True) -> int | None:
    for key in ("m", "d", "x"):
        if candidate.get(key) is not None:
            return _number(candidate[key], key)
    typed = getattr(fact, "crypto", {}).get("typed_value") if fact is not None else None
    if isinstance(typed, dict) and typed.get("type") == "integer" and typed.get("radix") == 10:
        return _number(typed.get("value"), "typed_value")
    if not allow_answer:
        # With answer_form=hash the answer field holds a digest, and a digest made only of
        # decimal digits must never be reinterpreted as the recovered private key.
        return None
    answer = candidate.get("answer")
    if isinstance(answer, str) and len(answer) <= 1234 and _DECIMAL.fullmatch(answer):
        return int(answer)
    return None


def _match_plaintext_answer(m: int, answer: str, goal: dict[str, Any], checks: dict[str, Any]) -> bool:
    """Compare a candidate answer with the bytes of a verified integer."""
    length = goal.get("byte_length", max(1, (m.bit_length() + 7) // 8))
    plaintext_bytes = m.to_bytes(length, goal.get("byte_order", "big"))
    checks["plaintext_sha256"] = hashlib.sha256(plaintext_bytes).hexdigest()
    checks["plaintext_length"] = len(plaintext_bytes)
    try:
        plaintext_text = plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError:
        plaintext_text = None
    relation = None
    if goal.get("answer_form") == "hash":
        # The task states the submitted answer is a hash of the recovered plaintext, so the
        # plaintext itself must not be accepted here; a bare plaintext stays a wrong answer.
        algorithm = goal.get("answer_hash", "sha256")
        digest = hashlib.new(algorithm, plaintext_bytes).hexdigest()
        checks["preimage_hex"] = plaintext_bytes.hex()
        checks["answer_hash_algorithm"] = algorithm
        checks["answer_hash_expected"] = digest
        checks["answer_hash_matches"] = answer.strip().lower() == digest
        if checks["answer_hash_matches"]:
            relation = "hash"
        checks["answer_matches"] = relation is not None
        if relation is not None:
            checks["answer_relation"] = relation
        return relation is not None
    if answer == str(m):
        relation = "decimal"
    elif plaintext_bytes.hex() == answer.lower():
        relation = "hex"
    elif plaintext_text is not None and answer == plaintext_text:
        relation = "plaintext"
    elif plaintext_text is not None and answer in plaintext_text:
        relation = "substring"
        checks["answer_offset"] = plaintext_text.find(answer)
    elif plaintext_text and plaintext_text in answer:
        relation = "wrapped"
        checks["answer_offset"] = answer.find(plaintext_text)
    checks["answer_matches"] = relation is not None
    if relation is not None:
        checks["answer_relation"] = relation
    return relation is not None


def _answer_bytes(candidate: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    answer = candidate["answer"]
    encoding = manifest.get("data_encoding", "utf8")
    if encoding == "hex":
        if not isinstance(answer, str) or len(answer) % 2 or not _HEX.fullmatch(answer):
            raise ValueError("candidate answer must be a hex string for this manifest")
        return bytes.fromhex(answer)
    if encoding != "utf8":
        raise ValueError("manifest data_encoding must be utf8 or hex")
    return answer.encode("utf-8")


def _verify_dsa(inputs: list[tuple[str, bytes]], candidate: dict[str, Any],
                goal: dict[str, Any] | None = None, fact: Any = None) -> tuple[str, dict[str, Any]]:
    values, extraction = extract_dsa(inputs)
    p, q, g, y = values["p"], values["q"], values["g"], values["y"]
    checks: dict[str, Any] = {"scheme": "dsa", "p_bits": p.bit_length(), "q_bits": q.bit_length()}
    valid = q < p and (p - 1) % q == 0 and pow(g, q, p) == 1 and pow(y, q, p) == 1
    checks["parameters_valid"] = valid
    if not valid:
        return "inconclusive", {**checks, "reason": "invalid DSA parameters"}
    goal = goal or crypto_goal({})
    private = _candidate_int(candidate, fact, allow_answer=goal.get("answer_form") != "hash")
    if private is None:
        return "inconclusive", {**checks,
                                "reason": "DSA candidate needs a decimal private key in candidate.m or typed_value"}
    checks["private_key_in_range"] = 1 <= private < q
    if not checks["private_key_in_range"]:
        return "fail", checks
    checks["public_key_matches"] = pow(g, private, p) == y
    if not checks["public_key_matches"]:
        return "fail", checks
    encoding = goal.get("answer_encoding", "utf8")
    if candidate.get("answer_encoding", encoding) != encoding:
        return "fail", {**checks, "reason": "candidate encoding does not match the project goal"}
    checks["answer_encoding"] = encoding
    try:
        matched = _match_plaintext_answer(private, candidate["answer"], goal, checks)
    except OverflowError:
        return "fail", {**checks, "reason": "DSA private key does not fit the project answer representation"}
    if not matched:
        return "fail", checks
    checks["extraction"] = extraction
    return "pass", checks


def _ec_add(first, second, p: int, a: int):
    if first is None:
        return second
    if second is None:
        return first
    x1, y1 = first
    x2, y2 = second
    if x1 == x2:
        if (y1 + y2) % p == 0:
            return None
        slope = (3 * x1 * x1 + a) * pow(2 * y1, -1, p) % p
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, p) % p
    x3 = (slope * slope - x1 - x2) % p
    return x3, (slope * (x1 - x3) - y1) % p


def _ec_mul(scalar: int, point, p: int, a: int):
    result = None
    addend = point
    while scalar:
        check_verification()
        if scalar & 1:
            result = _ec_add(result, addend, p, a)
        addend = _ec_add(addend, addend, p, a)
        scalar >>= 1
    return result


def _ec_on_curve(point, p: int, a: int, b: int) -> bool:
    if point is None:
        return False
    x, y = point
    return 0 <= x < p and 0 <= y < p and (y * y - x * x * x - a * x - b) % p == 0


def _verify_ecdsa(manifest: dict[str, Any], candidate: dict[str, Any],
                  goal: dict[str, Any] | None = None, fact: Any = None) -> tuple[str, dict[str, Any]]:
    curve = manifest.get("curve")
    if isinstance(curve, str) and curve in _EC_CURVES:
        params = dict(_EC_CURVES[curve])
        curve_name = curve
    else:
        params = {key: _manifest_int(manifest, key, required=True)
                  for key in ("p", "a", "b", "gx", "gy", "n")}
        curve_name = "explicit"
    p, a, b, n = params["p"], params["a"], params["b"], params["n"]
    G = (params["gx"], params["gy"])
    Q = (_manifest_int(manifest, "qx", required=True), _manifest_int(manifest, "qy", required=True))
    checks: dict[str, Any] = {"scheme": "ecdsa", "curve": curve_name, "field_bits": p.bit_length()}
    checks["generator_on_curve"] = _ec_on_curve(G, p, a, b)
    checks["public_point_on_curve"] = _ec_on_curve(Q, p, a, b)
    if not checks["generator_on_curve"] or not checks["public_point_on_curve"]:
        return "inconclusive", {**checks, "reason": "invalid ECDSA curve or public point"}
    checks["public_point_in_subgroup"] = _ec_mul(n, Q, p, a) is None
    if not checks["public_point_in_subgroup"]:
        return "inconclusive", {**checks, "reason": "ECDSA public point is not in the declared subgroup"}
    goal = goal or crypto_goal({})
    private = _candidate_int(candidate, fact, allow_answer=goal.get("answer_form") != "hash")
    if private is None:
        return "inconclusive", {**checks,
                                "reason": "ECDSA candidate needs a decimal private key in candidate.m or typed_value"}
    checks["private_key_in_range"] = 1 <= private < n
    if not checks["private_key_in_range"]:
        return "fail", checks
    checks["public_key_matches"] = _ec_mul(private, G, p, a) == Q
    if not checks["public_key_matches"]:
        return "fail", checks
    encoding = goal.get("answer_encoding", "utf8")
    if candidate.get("answer_encoding", encoding) != encoding:
        return "fail", {**checks, "reason": "candidate encoding does not match the project goal"}
    checks["answer_encoding"] = encoding
    try:
        matched = _match_plaintext_answer(private, candidate["answer"], goal, checks)
    except OverflowError:
        return "fail", {**checks, "reason": "ECDSA private key does not fit the project answer representation"}
    if not matched:
        return "fail", checks
    return "pass", checks


def _verify_hash(manifest: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    algorithm = manifest.get("hash")
    digest = manifest.get("digest")
    if algorithm not in _HASH_ALGORITHMS:
        raise ValueError(f"unsupported hash algorithm: {algorithm}")
    if not isinstance(digest, str) or len(digest) % 2 or not _HEX.fullmatch(digest):
        raise ValueError("hash manifest digest must be an even-length hex string")
    data = _answer_bytes(candidate, manifest)
    if len(data) > MAX_VERIFY_INPUT_BYTES:
        return "inconclusive", {"reason": "hash candidate exceeds the size limit"}
    checks = {"scheme": "hash", "algorithm": algorithm, "answer_length": len(data)}
    checks["digest_matches"] = hashlib.new(algorithm, data).hexdigest().lower() == digest.lower()
    return ("pass" if checks["digest_matches"] else "fail"), checks


def _verify_xor(manifest: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    ciphertext_hex = manifest.get("ciphertext")
    if not isinstance(ciphertext_hex, str) or len(ciphertext_hex) % 2 or not _HEX.fullmatch(ciphertext_hex):
        raise ValueError("xor manifest ciphertext must be an even-length hex string")
    ciphertext = bytes.fromhex(ciphertext_hex)
    data = _answer_bytes(candidate, manifest)
    checks = {"scheme": "xor", "ciphertext_length": len(ciphertext), "answer_length": len(data)}
    if len(data) != len(ciphertext):
        checks["length_matches"] = False
        return "fail", checks
    checks["length_matches"] = True
    if "key" in manifest:
        key_hex = manifest["key"]
        if not isinstance(key_hex, str) or not key_hex or len(key_hex) % 2 or not _HEX.fullmatch(key_hex):
            raise ValueError("xor manifest key must be a non-empty even-length hex string")
        key = bytes.fromhex(key_hex)
        expected = bytes(value ^ key[index % len(key)] for index, value in enumerate(data))
        checks["key_length"] = len(key)
        checks["ciphertext_matches"] = expected == ciphertext
        return ("pass" if checks["ciphertext_matches"] else "fail"), checks
    key_length = _manifest_int(manifest, "key_length") or 1
    if not 1 <= key_length <= len(data):
        return "inconclusive", {**checks, "reason": "xor key_length is outside the candidate length"}
    differences = [left ^ right for left, right in zip(data, ciphertext)]
    checks["key_length"] = key_length
    checks["ciphertext_matches"] = all(
        differences[index] == differences[index % key_length] for index in range(len(differences)))
    return ("pass" if checks["ciphertext_matches"] else "fail"), checks


def _verify_encoding(manifest: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    encoding = manifest.get("encoding")
    data = manifest.get("data")
    if not isinstance(data, str):
        raise ValueError("encoding manifest data must be a string")
    answer = candidate["answer"]
    if len(answer.encode("utf-8")) > MAX_VERIFY_INPUT_BYTES:
        return "inconclusive", {"reason": "encoding candidate exceeds the size limit"}
    if encoding == "base64":
        encoded = base64.b64encode(answer.encode("utf-8")).decode("ascii")
    elif encoding == "base32":
        encoded = base64.b32encode(answer.encode("utf-8")).decode("ascii")
    elif encoding == "hex":
        encoded = answer.encode("utf-8").hex()
    elif encoding == "url":
        encoded = quote(answer, safe="")
    else:
        raise ValueError(f"unsupported encoding: {encoding}")
    checks = {"scheme": "encoding", "encoding": encoding, "answer_length": len(answer)}
    checks["encoded_matches"] = encoded == data
    return ("pass" if checks["encoded_matches"] else "fail"), checks


def _manifest_algorithm(inputs: list[tuple[str, bytes]]) -> str | None:
    if len(inputs) != 1 or not inputs[0][1].lstrip().startswith(b"{"):
        return None
    try:
        data = json.loads(inputs[0][1].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data.get("algorithm") if isinstance(data, dict) else None


def _looks_like_dsa_source(inputs: list[tuple[str, bytes]]) -> bool:
    if len(inputs) != 1:
        return False
    try:
        text = inputs[0][1].decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if not all(re.search(pattern, text) for pattern in
               (r"(?m)^\s*p\s*[:=]", r"(?m)^\s*q\s*[:=]", r"(?m)^\s*g\s*[:=]")):
        return False
    return bool(re.search(r"(?m)^\s*y\s*=", text) or "def sign" in text or "powmod(" in text)


def _verify_rsa(raw: bytes, candidate: dict[str, Any], goal: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    answer_kind = candidate.get("answer_kind", "plaintext")
    if answer_kind not in {"plaintext", "factors", "private_exponent"}:
        return "inconclusive", {"reason": "unsupported RSA answer_kind"}
    required = {"n"} if answer_kind == "factors" else {"n", "e"} if answer_kind == "private_exponent" else {"n", "e", "c"}
    values = _rsa_input(raw, required)
    n, e, c = values["n"], values.get("e", 0), values.get("c", 0)
    if n.bit_length() > MAX_RSA_BITS or e.bit_length() > 256:
        return "inconclusive", {"reason": "RSA verifier size limit exceeded"}
    checks: dict[str, Any] = {"n_bits": n.bit_length(), "answer_kind": answer_kind}
    p_value, q_value = candidate.get("p"), candidate.get("q")
    if (p_value is None) != (q_value is None):
        return "inconclusive", {**checks, "reason": "p and q must be supplied together"}
    p = q = lam = None
    factors_source = None
    if p_value is not None:
        factors_source = "candidate"
        p, q = _number(p_value, "p"), _number(q_value, "q")
        checks["factor_product_matches"] = p > 1 and q > 1 and p != q and p * q == n
        if not checks["factor_product_matches"]:
            return "fail", checks
        checks["factors_probably_prime"] = _probably_prime(p) and _probably_prime(q)
        if not checks["factors_probably_prime"]:
            return "fail", checks
        lam = math.lcm(p - 1, q - 1)
        checks["unique_root"] = math.gcd(e, lam) == 1 if e else None
    elif "p" in values and "q" in values:
        factors_source = "input"
        p, q = values["p"], values["q"]
        checks["factor_product_matches"] = p > 1 and q > 1 and p != q and p * q == n
        if not checks["factor_product_matches"]:
            return "inconclusive", {**checks, "reason": "input factors do not match n"}
        checks["factors_probably_prime"] = _probably_prime(p) and _probably_prime(q)
        if not checks["factors_probably_prime"]:
            return "inconclusive", {**checks, "reason": "input factors are not prime"}
        lam = math.lcm(p - 1, q - 1)
        checks["unique_root"] = math.gcd(e, lam) == 1 if e else None
    if factors_source is not None:
        checks["factors_source"] = factors_source
    if answer_kind == "factors":
        if p is None or q is None:
            return "inconclusive", {**checks, "reason": "factor answer needs p and q"}
        checks["answer_matches"] = candidate["answer"] in {f"{p},{q}", f"{q},{p}"}
        return ("pass" if checks["answer_matches"] else "fail"), checks
    if candidate.get("d") is not None:
        if lam is None:
            return "inconclusive", {**checks, "reason": "private exponent needs verified factors"}
        d = _number(candidate["d"], "d")
        checks["private_exponent_matches"] = (e * d) % lam == 1
        if not checks["private_exponent_matches"]:
            return "fail", checks
    if answer_kind == "private_exponent":
        if candidate.get("d") is None:
            return "inconclusive", {**checks, "reason": "private exponent answer needs d"}
        checks["answer_matches"] = candidate["answer"] == str(d)
        return ("pass" if checks["answer_matches"] else "fail"), checks
    goal = goal or crypto_goal({})
    encoding = goal.get("answer_encoding", "decimal")
    if candidate.get("answer_encoding", encoding) != encoding:
        return "fail", {**checks, "reason": "candidate encoding does not match the project goal"}
    answer_text = candidate["answer"]
    supplied_m = candidate.get("m")
    if supplied_m is None and goal.get("answer_form") == "hash":
        return "inconclusive", {
            **checks,
            "reason": ("a hashed answer needs the recovered plaintext in crypto.candidate.m "
                       "(decimal); the answer field holds only the digest"),
        }
    if supplied_m is None:
        if not _DECIMAL.fullmatch(answer_text) or len(answer_text) > 1234:
            return "inconclusive", {
                **checks,
                "reason": "RSA plaintext candidate needs a decimal m when answer is not a decimal integer",
            }
        m = int(answer_text)
    else:
        m = _number(supplied_m, "m")
    if m.bit_length() > MAX_RSA_BITS:
        return "inconclusive", {**checks, "reason": "RSA plaintext exceeds the size limit"}
    checks["answer_encoding"] = encoding
    try:
        checks["answer_matches"] = _match_plaintext_answer(m, answer_text, goal, checks)
    except OverflowError:
        return "fail", {**checks, "reason": "RSA plaintext does not fit the project answer representation"}
    checks["ciphertext_matches"] = 0 <= m < n and pow(m, e, n) == c
    if not checks["answer_matches"] or not checks["ciphertext_matches"]:
        return "fail", checks
    if candidate.get("d") is not None:
        checks["private_exponent_matches"] = pow(c, d, n) == m
        if not checks["private_exponent_matches"]:
            return "fail", checks
    if n <= 10000:
        roots = []
        for number in range(n):
            if number % 256 == 0:
                check_verification()
            if pow(number, e, n) == c:
                roots.append(number)
        checks["candidate_set"] = [str(number) for number in roots]
        checks["unique_root"] = len(roots) == 1
    else:
        checks.setdefault("unique_root", None)
    return "pass", checks


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes):
    """Decrypt and unpad CBC data; None means no AES library, False means bad padding."""
    try:
        from Crypto.Cipher import AES
        padded = AES.new(key, AES.MODE_CBC, iv=iv).decrypt(ciphertext)
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            return None
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return False
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        return False
    return padded[:-pad]


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes):
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv=iv).encrypt(padded)
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            return None
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()


def _verify_rsa_aes(inputs: list[tuple[str, bytes]], candidate: dict[str, Any],
                    goal: dict[str, Any] | None = None, fact: Any = None) -> tuple[str, dict[str, Any]]:
    """Verify ``key = long_to_bytes(RSA plaintext)[:16]`` protecting an AES-CBC ciphertext.

    Every parameter comes from the trusted original inputs; the candidate only supplies the
    RSA plaintext ``m`` and the recovered plaintext ``answer``.  Worker-built manifests are
    deliberately not used, because their expected-plaintext hash would be self-asserted.
    """
    goal = goal or crypto_goal({})
    values, extraction = extract_rsa_aes(inputs)
    n, c, e = values["N"], values["c"], values["e"]
    iv, ciphertext = values["iv"], values["ciphertext"]
    checks: dict[str, Any] = {
        "scheme": "rsa-aes", "n_bits": n.bit_length(), "e_bits": e.bit_length(),
        "ciphertext_bytes": len(ciphertext), "extraction": extraction,
    }
    m = _candidate_int(candidate, fact, allow_answer=False)
    if m is None:
        return "inconclusive", {
            **checks,
            "reason": "rsa-aes candidate needs the RSA plaintext in crypto.candidate.m",
        }
    checks["rsa_ciphertext_matches"] = 0 <= m < n and pow(m, e, n) == c
    if not checks["rsa_ciphertext_matches"]:
        return "fail", checks
    key = m.to_bytes(max(1, (m.bit_length() + 7) // 8), "big")[:16]
    if len(key) not in {16, 24, 32}:
        return "inconclusive", {
            **checks, "reason": "RSA plaintext prefix is shorter than an AES key",
        }
    checks["key_hex"] = key.hex()
    supplied_key = candidate.get("key_hex")
    if isinstance(supplied_key, str) and supplied_key.strip().lower() != key.hex():
        return "fail", {**checks, "reason": "candidate key_hex does not match long_to_bytes(m)[:16]"}
    plaintext = _aes_cbc_decrypt(key, iv, ciphertext)
    if plaintext is None:
        return "inconclusive", {**checks, "reason": "optional AES verifier package is unavailable"}
    if plaintext is False:
        return "fail", {**checks, "aes_roundtrip": False, "reason": "AES-CBC decryption or padding failed"}
    checks["plaintext_hex"] = plaintext.hex()
    checks["plaintext_bytes"] = len(plaintext)
    checks["plaintext_sha256"] = hashlib.sha256(plaintext).hexdigest()
    encrypted = _aes_cbc_encrypt(key, iv, plaintext)
    checks["aes_roundtrip"] = encrypted == ciphertext
    if not checks["aes_roundtrip"]:
        return "fail", checks
    answer = candidate["answer"]
    if goal.get("answer_form") == "hash":
        algorithm = goal.get("answer_hash", "sha256")
        digest = hashlib.new(algorithm, plaintext).hexdigest()
        checks["answer_hash_algorithm"] = algorithm
        checks["answer_hash_expected"] = digest
        checks["answer_hash_matches"] = answer.strip().lower() == digest
        checks["answer_matches"] = checks["answer_hash_matches"]
    else:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        checks["answer_matches"] = (
            (text is not None and answer == text) or plaintext.hex() == answer.strip().lower()
        )
    return ("pass" if checks["answer_matches"] else "fail"), checks


def _mt_words(value: Any) -> tuple[list[int], int]:
    if isinstance(value, dict):
        words = value.get("words", value.get("state"))
        index = value.get("index", 0)
    elif isinstance(value, list):
        words = value
        index = 0
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            words = json.loads(text)
        elif re.fullmatch(r"[0-9a-fA-F]{4992}", text):
            words = [int(text[pos:pos + 8], 16) for pos in range(0, len(text), 8)]
        else:
            words = [int(part, 0) for part in re.split(r"[,\s]+", text) if part]
        index = 0
    else:
        raise ValueError("mt19937 candidate needs state (624 words) or state_ref")
    if not isinstance(words, list):
        raise ValueError("mt19937 state words must be a list")
    words = [int(word) for word in words]
    if type(index) is not int:
        raise ValueError("mt19937 state index must be an integer")
    return words, index


def _candidate_mt_state(candidate: dict[str, Any], fact: Any, files: CryptoProjectFiles,
                        project: Project) -> tuple[list[int], int, str]:
    raw = candidate.get("state")
    if raw is not None:
        words, index = _mt_words(raw)
        return words, candidate.get("state_index", index), "candidate.state"
    refs = fact.crypto.get("artifact_refs", [])
    ref_path = candidate.get("state_ref") or candidate.get("state_artifact")
    selected: list[dict[str, Any]] = []
    if isinstance(ref_path, str) and ref_path:
        selected = [ref for ref in refs if ref.get("path") == ref_path]
        if not selected:
            raise ValueError("state_ref must name one of the candidate's artifact_refs")
    else:
        # Compatibility for candidates that already saved state*.json but did not reference it.
        selected = [
            ref for ref in refs
            if isinstance(ref.get("path"), str) and ref["path"].endswith(".json")
            and Path(ref["path"]).name.lower().startswith("state")
        ]
    for ref in selected:
        path = files.verify_ref(project.id, ref, "artifact")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("mt19937 state artifact is not valid JSON") from exc
        words, index = _mt_words(data)
        return words, candidate.get("state_index", index), f"artifact:{ref['path']}"
    raise ValueError(
        "mt19937 candidate needs state (624 words) or state_ref pointing to its state JSON artifact"
    )


def _verify_mt19937(inputs: list[tuple[str, bytes]], candidate: dict[str, Any],
                    goal: dict[str, Any] | None, fact: Any, files: CryptoProjectFiles,
                    project: Project) -> tuple[str, dict[str, Any]]:
    goal = goal or crypto_goal({})
    data = extract_mt19937(inputs)
    checks: dict[str, Any] = {
        "scheme": "mt19937", "extraction": data["sources"],
        "rounds": data["params"]["rounds"],
        "shuffle_rounds": data["params"]["shuffle_rounds"],
    }
    try:
        words, index, source = _candidate_mt_state(candidate, fact, files, project)
        plaintext, replay = replay_mt19937(
            words, index, data["params"], data["cipher"], data["gift"])
    except ValueError as exc:
        return "inconclusive", {**checks, "reason": str(exc)}
    checks.update(replay)
    checks.update({
        "state_source": source,
        "state_index": index,
        "plaintext_hex": plaintext.hex(),
        "plaintext_bytes": len(plaintext),
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
    })
    if not checks["gift_matches"]:
        return "fail", checks
    answer = candidate["answer"]
    if goal.get("answer_form") == "hash":
        algorithm = goal.get("answer_hash", "sha256")
        digest = hashlib.new(algorithm, plaintext).hexdigest()
        checks["answer_hash_algorithm"] = algorithm
        checks["answer_hash_expected"] = digest
        checks["answer_hash_matches"] = answer.strip().lower() == digest
        checks["answer_matches"] = checks["answer_hash_matches"]
    else:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        relation = None
        if text is not None and answer == text:
            relation = "plaintext"
        elif plaintext.hex() == answer.strip().lower():
            relation = "hex"
        elif text is not None and answer in text:
            # A task may append explanatory noise to the plaintext; accept the flag substring.
            relation = "substring"
        elif text and text in answer:
            relation = "wrapped"
        if relation is not None:
            checks["answer_relation"] = relation
        checks["answer_matches"] = relation is not None
    return ("pass" if checks["answer_matches"] else "fail"), checks


def _verify_aes(raw: bytes, candidate: dict[str, Any],
                goal: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("AES input must be a JSON manifest") from exc
    if not isinstance(manifest, dict):
        raise ValueError("AES input must be a JSON object")
    mode = manifest.get("mode")
    if mode not in {"gcm", "cbc"} or manifest.get("algorithm") != "aes":
        raise ValueError("supported AES modes are gcm and cbc")
    expected_hash = manifest.get("known_plaintext_sha256")
    if expected_hash is not None and (not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        return "inconclusive", {"reason": "known_plaintext_sha256 must be 64 lowercase hex digits"}
    if mode == "cbc" and expected_hash is None:
        return "inconclusive", {"reason": "independent known_plaintext_sha256 constraint is required"}
    key = _hex(candidate.get("key_hex"), "key_hex")
    if len(key) not in {16, 24, 32}:
        return "fail", {"reason": "invalid AES key length"}
    ciphertext = _hex(manifest.get("ciphertext_hex"), "ciphertext_hex")
    try:
        from Crypto.Cipher import AES as CryptoAES
    except ImportError:
        CryptoAES = None
    if CryptoAES is None:
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return "inconclusive", {"reason": "optional AES verifier package is unavailable"}
    else:
        InvalidTag = ValueError
    checks: dict[str, Any] = {"mode": mode, "known_plaintext_sha256": expected_hash}
    try:
        if mode == "gcm":
            if manifest.get("padding") not in (None, "none"):
                return "fail", {**checks, "reason": "GCM must not use padding"}
            nonce = _hex(manifest.get("nonce_hex"), "nonce_hex")
            tag = _hex(manifest.get("tag_hex"), "tag_hex")
            aad = _hex(manifest.get("aad_hex", ""), "aad_hex")
            if len(nonce) != 12 or len(tag) != 16:
                return "fail", {**checks, "reason": "GCM nonce/tag lengths must be 12/16 bytes"}
            if CryptoAES is not None:
                decryptor = CryptoAES.new(key, CryptoAES.MODE_GCM, nonce=nonce, mac_len=16)
                decryptor.update(aad)
                plaintext = decryptor.decrypt_and_verify(ciphertext, tag)
            else:
                plaintext = AESGCM(key).decrypt(nonce, ciphertext + tag, aad)
            checks["authentication_tag_valid"] = True
        else:
            iv = _hex(manifest.get("iv_hex"), "iv_hex")
            if len(iv) != 16 or len(ciphertext) == 0 or len(ciphertext) % 16:
                return "fail", {**checks, "reason": "CBC IV/ciphertext length is invalid"}
            if manifest.get("padding") not in {"pkcs7", "none"}:
                return "inconclusive", {**checks, "reason": "CBC padding must be explicit"}
            if CryptoAES is not None:
                plaintext = CryptoAES.new(key, CryptoAES.MODE_CBC, iv=iv).decrypt(ciphertext)
            else:
                decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
                plaintext = decryptor.update(ciphertext) + decryptor.finalize()
            if manifest["padding"] == "pkcs7":
                pad = plaintext[-1]
                if not 1 <= pad <= 16 or plaintext[-pad:] != bytes([pad]) * pad:
                    return "fail", {**checks, "padding_valid": False}
                plaintext = plaintext[:-pad]
                checks["padding_valid"] = True
    except (ValueError, InvalidTag):
        return "fail", {**checks, "authentication_tag_valid": False} if mode == "gcm" else {**checks, "reason": "decryption failed"}
    if expected_hash is not None:
        checks["plaintext_sha256_matches"] = hashlib.sha256(plaintext).hexdigest() == expected_hash
    checks["constraint"] = "known-plaintext-sha256" if expected_hash is not None else "gcm-authentication-tag"
    checks["plaintext_bytes"] = len(plaintext)
    encoding = candidate.get("answer_encoding", "utf8")
    try:
        answer = plaintext.hex() if encoding == "hex" else plaintext.decode("utf-8") if encoding == "utf8" else None
    except UnicodeDecodeError:
        answer = None
    if (goal or {}).get("answer_form") == "hash":
        algorithm = goal.get("answer_hash", "sha256")
        digest = hashlib.new(algorithm, plaintext).hexdigest()
        submitted = candidate.get("answer", "").strip().lower()
        checks["preimage_hex"] = plaintext.hex()
        checks["answer_hash_algorithm"] = algorithm
        checks["answer_hash_expected"] = digest
        checks["answer_hash_matches"] = submitted == digest
        checks["answer_matches"] = checks["answer_hash_matches"]
    else:
        checks["answer_matches"] = answer is not None and answer == candidate.get("answer")
    constraint_valid = checks.get("plaintext_sha256_matches", checks.get("authentication_tag_valid", False))
    return ("pass" if constraint_valid and checks["answer_matches"] else "fail"), checks


def _verify_aes_in_container(raw: bytes, candidate: dict[str, Any], image: str,
                             goal: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Use the already configured worker image as a fixed, resource-limited checker."""
    source = Path(__file__).resolve().parent
    container_name = f"echo-verify-{uuid4().hex}"
    code = (
        "import json,sys; "
        "from echo_agent.crypto_verify import _verify_aes; "
        "p=json.load(sys.stdin); "
        "print(json.dumps(_verify_aes(p['raw'].encode('utf-8'),p['candidate'],p.get('goal'))))"
    )
    command = [
        "docker", "run", "--rm", "-i", "--name", container_name,
        "--pull=never", "--network", "none", "--read-only",
        "--memory", "256m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,source={source},target=/src/echo_agent,readonly", "--workdir", "/src",
        image, "python3", "-B", "-c", code,
    ]
    payload = json.dumps({"raw": raw.decode("utf-8"), "candidate": candidate, "goal": goal or {}})
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return "inconclusive", {"reason": f"isolated AES verifier unavailable: {type(exc).__name__}"}
    try:
        # Feed stdin from a helper thread: a large manifest must not block cancellation, and
        # docker may take a while to start the container's python process.
        def write_stdin() -> None:
            try:
                if process.stdin is not None:
                    process.stdin.write(payload)
                    process.stdin.close()
            except OSError:
                pass

        writer = threading.Thread(target=write_stdin, daemon=True, name=f"{container_name}-stdin")
        writer.start()
        # 30 seconds is the container's own resource bound; the surrounding verification
        # deadline/cancel handle is polled separately so a project stop takes effect promptly.
        local_deadline = time.monotonic() + 30
        while process.poll() is None:
            try:
                check_verification()
            except (VerificationCancelled, VerificationTimeout):
                _terminate_process(process)
                _remove_verifier_container(container_name)
                raise
            if time.monotonic() >= local_deadline:
                _terminate_process(process)
                _remove_verifier_container(container_name)
                return "inconclusive", {"reason": "isolated AES verifier timed out"}
            time.sleep(0.05)
        writer.join(timeout=1)
        stdout = process.stdout.read() if process.stdout is not None else ""
    except (VerificationCancelled, VerificationTimeout):
        raise
    except (OSError, UnicodeDecodeError) as exc:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated AES verifier unavailable: {type(exc).__name__}"}
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass
    if process.returncode != 0:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated AES verifier exited {process.returncode}"}
    try:
        parsed = json.loads(stdout)
        if (not isinstance(parsed, list) or len(parsed) != 2 or parsed[0] not in {"pass", "fail", "inconclusive"}
                or not isinstance(parsed[1], dict)):
            raise ValueError("isolated AES verifier returned an invalid record")
        return parsed[0], parsed[1]
    except (json.JSONDecodeError, ValueError) as exc:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated AES verifier unavailable: {type(exc).__name__}"}


def _terminate_process(process: subprocess.Popen) -> None:
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _verify_rsa_aes_in_container(inputs: list[tuple[str, bytes]], candidate: dict[str, Any],
                                 goal: dict[str, Any] | None, image: str) -> tuple[str, dict[str, Any]]:
    """Run the RSA+AES check in the worker image when the host has no AES package."""
    source = Path(__file__).resolve().parent
    container_name = f"echo-verify-{uuid4().hex}"
    code = (
        "import json,sys; "
        "from echo_agent.crypto_verify import _verify_rsa_aes; "
        "p=json.load(sys.stdin); "
        "print(json.dumps(_verify_rsa_aes("
        "[(i,t.encode('utf-8')) for i,t in p['inputs']],p['candidate'],p.get('goal'))))"
    )
    command = [
        "docker", "run", "--rm", "-i", "--name", container_name,
        "--pull=never", "--network", "none", "--read-only",
        "--memory", "256m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,source={source},target=/src/echo_agent,readonly", "--workdir", "/src",
        image, "python3", "-B", "-c", code,
    ]
    payload = json.dumps({
        "inputs": [[input_id, text.decode("utf-8-sig")] for input_id, text in inputs],
        "candidate": candidate,
        "goal": goal or {},
    })
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return "inconclusive", {"reason": f"isolated RSA+AES verifier unavailable: {type(exc).__name__}"}
    try:
        def write_stdin() -> None:
            try:
                if process.stdin is not None:
                    process.stdin.write(payload)
                    process.stdin.close()
            except OSError:
                pass

        writer = threading.Thread(target=write_stdin, daemon=True, name=f"{container_name}-stdin")
        writer.start()
        local_deadline = time.monotonic() + 30
        while process.poll() is None:
            try:
                check_verification()
            except (VerificationCancelled, VerificationTimeout):
                _terminate_process(process)
                _remove_verifier_container(container_name)
                raise
            if time.monotonic() >= local_deadline:
                _terminate_process(process)
                _remove_verifier_container(container_name)
                return "inconclusive", {"reason": "isolated RSA+AES verifier timed out"}
            time.sleep(0.05)
        writer.join(timeout=1)
        stdout = process.stdout.read() if process.stdout is not None else ""
    except (VerificationCancelled, VerificationTimeout):
        raise
    except (OSError, UnicodeDecodeError) as exc:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated RSA+AES verifier unavailable: {type(exc).__name__}"}
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass
    if process.returncode != 0:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated RSA+AES verifier exited {process.returncode}"}
    try:
        parsed = json.loads(stdout)
        if (not isinstance(parsed, list) or len(parsed) != 2
                or parsed[0] not in {"pass", "fail", "inconclusive"} or not isinstance(parsed[1], dict)):
            raise ValueError("isolated RSA+AES verifier returned an invalid record")
        return parsed[0], parsed[1]
    except (json.JSONDecodeError, ValueError) as exc:
        _remove_verifier_container(container_name)
        return "inconclusive", {"reason": f"isolated RSA+AES verifier unavailable: {type(exc).__name__}"}


def _remove_verifier_container(name: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def goal_sha256(project: Project) -> str:
    return _digest({"goal": crypto_goal(project.crypto.get("goal", {})),
                    "objective": project.objective, "hint": project.hint, "target_url": project.target_url})


def verification_context(project: Project, candidate_fact_id: object) -> str:
    candidate = project.facts.get(candidate_fact_id) if type(candidate_fact_id) is int else None
    return _digest({"method": VERSION, "goal_sha256": goal_sha256(project),
                    "candidate": candidate.to_dict() if candidate is not None else None})


def verification_matches(record: dict[str, Any], fresh: dict[str, Any]) -> bool:
    return (record.get("status") == fresh.get("status") == "pass"
            and record.get("method") == fresh.get("method") == VERSION
            and all(key in record and key in fresh and record[key] == fresh[key] for key in (
                "candidate_fact_id", "candidate_sha256", "answer", "goal_sha256",
                "input_refs", "artifact_refs", "checks", "math_status",
            )))


def _verification_mode(project: Project) -> str:
    return crypto_goal(project.crypto.get("goal", {})).get("verification_mode", "strict")


def _matching_acceptance(project: Project, answer: str) -> bool:
    digest = goal_sha256(project)
    for record in reversed(project.crypto.get("acceptances", [])):
        if record.get("answer") != answer or record.get("goal_sha256") != digest:
            continue
        fact = project.facts.get(record.get("candidate_fact_id"))
        if fact is None or fact.kind != FactKind.CANDIDATE:
            continue
        if fact.crypto.get("candidate", {}).get("answer") != answer:
            continue
        if record.get("candidate_sha256") != _digest(fact.to_dict()):
            continue
        return True
    return False


def _strict_pass(project: Project, answer: str, verifier) -> bool:
    digest = goal_sha256(project)
    for record in reversed(project.crypto.get("verifications", [])):
        if (record.get("status") != "pass" or record.get("answer") != answer
                or record.get("goal_sha256") != digest or record.get("method") != VERSION):
            continue
        try:
            fresh = verifier(record["candidate_fact_id"])
        except (KeyError, ValueError, RuntimeError, OSError):
            continue
        if verification_matches(record, fresh):
            return True
    return False


def completion_source(project: Project, answer: str, verifier) -> str | None:
    """Return acceptance/evidence/verification, or None when completion is not allowed."""
    if _matching_acceptance(project, answer):
        return "acceptance"
    mode = _verification_mode(project)
    if mode == "acceptance" or project.crypto.get("require_platform_acceptance"):
        return None
    if mode == "evidence":
        if any(fact_supports_answer(fact, answer) for fact in project.facts.values()):
            return "evidence"
        return None
    return "verification" if _strict_pass(project, answer, verifier) else None


def answer_is_verified(project: Project, answer: str, verifier) -> bool:
    """One gate shared by dispatch and both production save paths."""
    return completion_source(project, answer, verifier) is not None


def _check_goal(goal: dict[str, Any], scheme: str, candidate: dict[str, Any], checks: dict[str, Any]) -> tuple[str, str]:
    kind = candidate.get("answer_kind", "plaintext")
    if kind != goal["answer_kind"] or (scheme == "aes" and kind != "plaintext"):
        return "fail", "candidate answer_kind does not match the project goal"
    encoding = goal.get("answer_encoding", "decimal" if scheme == "rsa" else "utf8")
    actual_encoding = candidate.get("answer_encoding", "decimal" if scheme == "rsa" else "utf8")
    if encoding != actual_encoding or (kind != "plaintext" and encoding != "decimal"):
        return "fail", "candidate answer_encoding does not match the project goal"
    if goal.get("answer_form") == "hash":
        # Hashing is an outer representation of a recovered plaintext.  Factor/private-exponent
        # goals submit integers, and hash/xor/encoding already submit the exact preimage, so
        # asking for yet another digest there would contradict the challenge statement.
        if kind != "plaintext" or scheme not in {"rsa", "rsa-aes", "aes", "dsa", "ecdsa", "mt19937"}:
            return "fail", "a hashed answer requires a plaintext goal and a key-recovery scheme"
        if checks.get("answer_hash_matches") is not True:
            return "fail", "candidate answer is not the required hash of the recovered plaintext"
    if scheme == "rsa" and kind == "plaintext" and goal["solution_policy"] == "unique":
        if checks.get("unique_root") is not True:
            return "inconclusive", "project requires a unique plaintext; uniqueness is false or unproved"
    if scheme in {"aes", "rsa-aes"}:
        if goal.get("byte_order", "big") != "big":
            return "inconclusive", "AES plaintext byte order conversion is not supported"
    if scheme in {"aes", "rsa-aes", "mt19937"}:
        if "byte_length" in goal and checks.get("plaintext_bytes") != goal["byte_length"]:
            return "fail", "plaintext length does not match the project goal"
    return "pass", ""


def verify_candidate(project: Project, candidate_fact_id: int, files: CryptoProjectFiles,
                     *, aes_image: str | None = None,
                     deadline: float | None = None,
                     cancel_event: threading.Event | None = None,
                     verification_id: str | None = None,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
    if project.task_type != "crypto":
        raise ValueError("candidate verification requires a crypto project")
    fact = project.facts.get(candidate_fact_id)
    if fact is None or fact.kind != FactKind.CANDIDATE or fact.crypto.get("claim_status") != "candidate":
        raise ValueError("candidate_fact_id must name a candidate fact")
    candidate = fact.crypto.get("candidate")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("answer"), str) or not candidate["answer"]:
        raise ValueError("candidate fact needs a structured answer")
    refs = {ref["id"]: ref for ref in project.crypto.get("inputs", [])}
    source_ids = list(fact.crypto.get("source_input_ids", []))
    started = time.monotonic()
    record: dict[str, Any] = {
        "candidate_fact_id": candidate_fact_id,
        "candidate_sha256": _digest(fact.to_dict()),
        "answer": candidate["answer"],
        "method": VERSION,
        "status": "inconclusive",
        "input_refs": [],
        "artifact_refs": fact.crypto.get("artifact_refs", []),
        "checks": {},
        "math_status": "inconclusive",
        "goal_sha256": goal_sha256(project),
        "verification_id": verification_id,
        "timeout_seconds": int(timeout_seconds) if timeout_seconds is not None else None,
        "backend": "host",
        "elapsed_ms": 0,
        "terminated_reason": "completed",
    }
    try:
        with verification_scope(deadline, cancel_event):
            check_verification()
            if not source_ids or len(source_ids) > 16 or len(set(source_ids)) != len(source_ids):
                raise ValueError("verification needs 1 to 16 distinct source inputs")
            record["input_refs"] = [refs[item] for item in source_ids]
            inputs = []
            remaining = MAX_VERIFY_INPUT_BYTES
            for input_id in source_ids:
                check_verification()
                raw = files.read_verified(project.id, refs[input_id], "input", max_bytes=remaining)
                remaining -= len(raw)
                inputs.append((input_id, raw))
            for ref in record["artifact_refs"]:
                files.verify_ref(project.id, ref, "artifact")
            check_verification()
            goal = crypto_goal(project.crypto.get("goal", {}))
            scheme = candidate.get("scheme", "auto")
            if scheme == "auto":
                algorithm = _manifest_algorithm(inputs)
                if isinstance(algorithm, str) and algorithm:
                    scheme = algorithm
                elif _looks_like_dsa_source(inputs):
                    scheme = "dsa"
                elif looks_like_rsa_aes(inputs):
                    scheme = "rsa-aes"
                elif looks_like_mt19937(inputs):
                    scheme = "mt19937"
                else:
                    scheme = "rsa"
            if not isinstance(scheme, str):
                scheme = "rsa"
            if scheme == "rsa" and looks_like_rsa_aes(inputs):
                # Older candidates often carry scheme=rsa for this composite task.  The
                # original inputs unambiguously contain an AES-CBC ciphertext, and the
                # rsa-aes check is strictly stronger (RSA relation + AES roundtrip), so
                # upgrade instead of failing with the parameter-only RSA error.
                scheme = "rsa-aes"
            if scheme == "rsa-aes":
                # The RSA+AES constants may be split across original inputs (for example
                # ``e = 3`` in the program and N/c/iv/ciphertext in the output file).  Read the
                # project's remaining original inputs -- never worker artifacts -- to complete
                # the parameter set, staying within the same 1 MiB budget.
                try:
                    extract_rsa_aes(inputs)
                except ValueError:
                    for ref in project.crypto.get("inputs", []):
                        if ref["id"] in source_ids:
                            continue
                        if remaining <= 0:
                            break
                        check_verification()
                        raw = files.read_verified(project.id, ref, "input", max_bytes=remaining)
                        remaining -= len(raw)
                        inputs.append((ref["id"], raw))
                        source_ids.append(ref["id"])
                        record["input_refs"].append(ref)
                        try:
                            extract_rsa_aes(inputs)
                            break
                        except ValueError:
                            continue
            if scheme == "mt19937":
                # The generator constants usually live in the program file while gift/c live in
                # the output file; read the remaining original inputs to complete the parse.
                try:
                    extract_mt19937(inputs)
                except ValueError:
                    for ref in project.crypto.get("inputs", []):
                        if ref["id"] in source_ids:
                            continue
                        if remaining <= 0:
                            break
                        check_verification()
                        raw = files.read_verified(project.id, ref, "input", max_bytes=remaining)
                        remaining -= len(raw)
                        inputs.append((ref["id"], raw))
                        source_ids.append(ref["id"])
                        record["input_refs"].append(ref)
                        try:
                            extract_mt19937(inputs)
                            break
                        except ValueError:
                            continue
            if scheme not in _SUPPORTED_SCHEMES:
                record["checks"] = {"reason": f"unsupported verification scheme: {scheme}; no independent checker is registered"}
            elif goal.get("answer_form") == "hash" and scheme in {"hash", "xor", "encoding"}:
                # These schemes already accept the exact preimage as the answer, so demanding a
                # digest on top cannot be satisfied by any candidate.  Say so instead of failing
                # with a misleading digest mismatch.
                record["checks"] = {
                    "reason": (f"answer_form=hash cannot be combined with the {scheme} scheme; "
                               "those schemes already submit the exact preimage"),
                }
            elif scheme == "rsa":
                kind = candidate.get("answer_kind", "plaintext")
                required = {"n"} if kind == "factors" else {"n", "e"} if kind == "private_exponent" else {"n", "e", "c"}
                raw, extraction = extract_rsa(inputs, required)
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "decimal"))}
                record["math_status"], record["checks"] = _verify_rsa(raw, candidate, goal)
                record["checks"]["extraction"] = extraction
            elif scheme == "rsa-aes":
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "utf8"))}
                record["math_status"], record["checks"] = _verify_rsa_aes(inputs, candidate, goal, fact)
                if (aes_image and record["math_status"] == "inconclusive"
                        and record["checks"].get("reason") == "optional AES verifier package is unavailable"):
                    record["math_status"], record["checks"] = _verify_rsa_aes_in_container(
                        inputs, candidate, goal, aes_image)
                    record["backend"] = "container"
                    if record["checks"].get("reason") == "isolated RSA+AES verifier timed out":
                        record["terminated_reason"] = "timeout"
            elif scheme == "mt19937":
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "utf8"))}
                record["math_status"], record["checks"] = _verify_mt19937(
                    inputs, candidate, goal, fact, files, project)
            elif scheme == "aes":
                if len(inputs) != 1:
                    raise ValueError("AES verifier requires one original JSON manifest")
                raw = inputs[0][1]
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "utf8"))}
                record["math_status"], record["checks"] = _verify_aes(raw, candidate, goal)
                if (aes_image and record["math_status"] == "inconclusive"
                        and record["checks"].get("reason") == "optional AES verifier package is unavailable"):
                    record["math_status"], record["checks"] = _verify_aes_in_container(
                        raw, candidate, aes_image, goal)
                    record["backend"] = "container"
                    if record["checks"].get("reason") == "isolated AES verifier timed out":
                        record["terminated_reason"] = "timeout"
            elif scheme == "dsa":
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "utf8"))}
                record["math_status"], record["checks"] = _verify_dsa(inputs, candidate, goal, fact)
            elif scheme == "ecdsa":
                candidate = {**candidate, "answer_encoding": candidate.get("answer_encoding", goal.get("answer_encoding", "utf8"))}
                record["math_status"], record["checks"] = _verify_ecdsa(_manifest(inputs), candidate, goal, fact)
            elif scheme in {"hash", "xor", "encoding"}:
                manifest = _manifest(inputs)
                verifier = {"hash": _verify_hash, "xor": _verify_xor, "encoding": _verify_encoding}[scheme]
                record["math_status"], record["checks"] = verifier(manifest, candidate)
            check_verification()
            record["status"] = record["math_status"]
            if record["math_status"] == "pass":
                status, reason = _check_goal(goal, scheme, candidate, record["checks"])
                record["status"] = status
                record["checks"]["goal_satisfied"] = status == "pass"
                if reason:
                    record["checks"]["reason"] = reason
    except VerificationCancelled as exc:
        record["status"] = "inconclusive"
        record["checks"] = {"reason": f"verification cancelled: {exc}"}
        record["terminated_reason"] = "cancelled"
    except VerificationTimeout as exc:
        record["status"] = "inconclusive"
        record["checks"] = {"reason": f"verification timed out: {exc}"}
        record["terminated_reason"] = "timeout"
    except (ValueError, OSError, KeyError, OverflowError, RecursionError, TypeError) as exc:
        record["status"] = "inconclusive"
        record["checks"] = {"reason": str(exc)}
        record["terminated_reason"] = "error"
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return record
