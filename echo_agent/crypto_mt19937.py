"""Deterministic MT19937 replay for random-number prediction tasks.

The verifier never executes the submitted program.  It extracts literal parameters from the
trusted original inputs (or an explicit JSON manifest), replays the generator, checks the
published gift, and inverts the shuffle sequence to recover the plaintext.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


# CPython limits decimal conversions to 4300 digits; the gift value is much longer.
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(100000)

_N, _M = 624, 397
_MATRIX_A = 0x9908B0DF
_UPPER_MASK = 0x80000000
_LOWER_MASK = 0x7FFFFFFF
_MASK32 = 0xFFFFFFFF


class MT19937:
    def __init__(self, words: list[int], index: int = 0) -> None:
        if len(words) != _N or any(type(word) is not int or not 0 <= word <= _MASK32 for word in words):
            raise ValueError("mt19937 state must be 624 unsigned 32-bit words")
        if type(index) is not int or not 0 <= index <= _N:
            raise ValueError("mt19937 state index must be between 0 and 624")
        self.state = list(words)
        self.index = index

    def _twist(self) -> None:
        mt = self.state
        for i in range(_N - _M):
            y = (mt[i] & _UPPER_MASK) | (mt[i + 1] & _LOWER_MASK)
            mt[i] = mt[i + _M] ^ (y >> 1) ^ (_MATRIX_A if y & 1 else 0)
        for i in range(_N - _M, _N - 1):
            y = (mt[i] & _UPPER_MASK) | (mt[i + 1] & _LOWER_MASK)
            mt[i] = mt[i + (_M - _N)] ^ (y >> 1) ^ (_MATRIX_A if y & 1 else 0)
        y = (mt[_N - 1] & _UPPER_MASK) | (mt[0] & _LOWER_MASK)
        mt[_N - 1] = mt[_M - 1] ^ (y >> 1) ^ (_MATRIX_A if y & 1 else 0)

    def next_uint32(self) -> int:
        if self.index >= _N:
            self._twist()
            self.index = 0
        y = self.state[self.index]
        self.index += 1
        y ^= y >> 11
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= y >> 18
        return y & _MASK32

    def getrandbits(self, bits: int) -> int:
        if type(bits) is not int or bits < 0 or bits > 32:
            raise ValueError("mt19937 getrandbits supports 0..32 bits")
        if bits == 0:
            return 0
        return self.next_uint32() >> (32 - bits)


def _randbelow(mt: MT19937, bound: int) -> int:
    bits = bound.bit_length()
    value = mt.getrandbits(bits)
    while value >= bound:
        value = mt.getrandbits(bits)
    return value


def replay_mt19937(words: list[int], index: int, params: dict[str, Any],
                   cipher: bytes, gift: int) -> tuple[bytes, dict[str, Any]]:
    """Replay the gift and invert the shuffle chain; return the recovered plaintext."""
    mt = MT19937(words, index)
    parts: list[bytes] = []
    for round_index in range(params["rounds"]):
        first = mt.getrandbits(params["first_bits"])
        second = mt.getrandbits(params["second_bits"])
        value = (pow(first, 2 * round_index, params["modulus"]) & 0xFF) ^ second
        parts.append(value.to_bytes(params["value_bytes"], "big"))
    gift_bytes = b"".join(parts)
    checks: dict[str, Any] = {
        "gift_bytes": len(gift_bytes),
        "gift_matches": int.from_bytes(gift_bytes, "big") == gift,
        "shuffle_rounds": params["shuffle_rounds"],
        "cipher_bytes": len(cipher),
    }
    swaps: list[tuple[int, int]] = []
    for _ in range(params["shuffle_rounds"]):
        for position in reversed(range(1, len(cipher))):
            swaps.append((position, _randbelow(mt, position + 1)))
    plaintext = bytearray(cipher)
    for position, target in reversed(swaps):
        plaintext[position], plaintext[target] = plaintext[target], plaintext[position]
    checks["swap_count"] = len(swaps)
    return bytes(plaintext), checks


def _texts(inputs: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    texts = []
    for input_id, raw in inputs:
        try:
            texts.append((input_id, raw.decode("utf-8-sig")))
        except UnicodeDecodeError as exc:
            raise ValueError("mt19937 input must be UTF-8 text") from exc
    return texts


def _cipher_bytes(value: str) -> bytes:
    try:
        data = bytes(ord(char) for char in value)
    except ValueError as exc:
        raise ValueError("mt19937 cipher contains characters outside 0..255") from exc
    return data


def _manifest(text: str) -> dict[str, Any] | None:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("algorithm") == "mt19937":
        return data
    return None


def _params_from_text(text: str) -> dict[str, Any] | None:
    loop = re.search(
        r"for\s+\w+\s+in\s+range\((\d+)\):\s*\r?\n\s*\w+\s*=\s*getrandbits\((\d+)\)"
        r"\s*\r?\n\s*\w+\s*=\s*getrandbits\((\d+)\)",
        text,
    )
    modulus = re.search(r"pow\([^,]+,\s*2\s*\*\s*\w+\s*,\s*(\d+)\)", text)
    value_bytes = re.search(r"long_to_bytes\([^,]+,\s*(\d+)\)", text)
    shuffle = re.search(r"for\s+\w+\s+in\s+range\((\d+)\):\s*\r?\n\s*shuffle\(", text)
    if not (loop and modulus and value_bytes and shuffle):
        return None
    return {
        "rounds": int(loop.group(1)),
        "first_bits": int(loop.group(2)),
        "second_bits": int(loop.group(3)),
        "modulus": int(modulus.group(1)),
        "value_bytes": int(value_bytes.group(1)),
        "shuffle_rounds": int(shuffle.group(1)),
    }


def _validate_params(params: dict[str, Any]) -> dict[str, Any]:
    def int_field(name: str, low: int, high: int) -> int:
        value = params.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"mt19937 {name} must be an integer in {low}..{high}")
        return value

    return {
        "rounds": int_field("rounds", 1, 1_000_000),
        "first_bits": int_field("first_bits", 1, 32),
        "second_bits": int_field("second_bits", 1, 32),
        "modulus": int_field("modulus", 2, 2**32),
        "value_bytes": int_field("value_bytes", 1, 8),
        "shuffle_rounds": int_field("shuffle_rounds", 0, 1_000_000),
    }


def extract_mt19937(inputs: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Parse parameters, gift and cipher from trusted original inputs."""
    texts = _texts(inputs)
    params: dict[str, Any] | None = None
    gift: int | None = None
    cipher: bytes | None = None
    sources: list[dict[str, Any]] = []
    for input_id, text in texts:
        manifest = _manifest(text)
        if manifest is not None:
            params = {key: manifest[key] for key in (
                "rounds", "first_bits", "second_bits", "modulus", "value_bytes", "shuffle_rounds",
            ) if key in manifest}
            if "gift" in manifest:
                raw_gift = manifest["gift"]
                gift = raw_gift if type(raw_gift) is int else int(str(raw_gift), 10)
            cipher_value = manifest.get("cipher", manifest.get("c"))
            if isinstance(cipher_value, str):
                cipher = _cipher_bytes(cipher_value)
            sources.append({"input_id": input_id, "format": "mt19937-json"})
            continue
        found = _params_from_text(text)
        if found is not None:
            params = found
            sources.append({"input_id": input_id, "format": "python-source"})
            # Program files also contain ``c = "".join(...)``; never treat that as the cipher.
            continue
        for line in text.splitlines():
            if line.startswith("gift = "):
                gift = int(line[len("gift = "):].strip(), 10)
            elif line.startswith("c = "):
                cipher = _cipher_bytes(line[len("c = "):])
    if params is None:
        raise ValueError("mt19937 input needs generator parameters or an mt19937 JSON manifest")
    if gift is None:
        raise ValueError("mt19937 input needs a gift value")
    if cipher is None:
        raise ValueError("mt19937 input needs the shuffled cipher c")
    params = _validate_params(params)
    if not cipher:
        raise ValueError("mt19937 cipher must be non-empty")
    return {"params": params, "gift": gift, "cipher": cipher, "sources": sources}


def looks_like_mt19937(inputs: list[tuple[str, bytes]]) -> bool:
    for _, raw in inputs:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        manifest = _manifest(text)
        if manifest is not None:
            return True
        if "getrandbits(" in text and "shuffle(" in text and "gift" in text:
            return True
        has_gift = "gift = " in text or text.startswith("gift =")
        has_cipher = "c = " in text or text.startswith("c = ")
        if has_gift and has_cipher:
            return True
    return False
