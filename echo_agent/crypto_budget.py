"""Hard bounds for crypto worker runs. General projects do not use these limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_WALL_SECONDS = 300
MAX_WALL_SECONDS = 1800
DEFAULT_MEMORY_MB = 1024
MAX_MEMORY_MB = 2048
DEFAULT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
CRYPTO_PIDS_LIMIT = 128


@dataclass(frozen=True, slots=True)
class CryptoBudget:
    wall_seconds: int
    memory_mb: int
    output_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {"wall_seconds": self.wall_seconds, "memory_mb": self.memory_mb,
                "output_bytes": self.output_bytes}


def resolve_crypto_budget(defaults: dict[str, Any] | None, requested: dict[str, Any] | None) -> CryptoBudget:
    values = {**(defaults or {}), **(requested or {})}

    def bounded(name: str, default: int, maximum: int, minimum: int = 1) -> int:
        value = values.get(name, default)
        if type(value) is not int or value < minimum or value > maximum:
            raise ValueError(f"crypto budget {name} must be an integer in {minimum}..{maximum}")
        return value

    unsupported = set(values) - {"wall_seconds", "memory_mb", "output_bytes"}
    if unsupported:
        raise ValueError(f"unsupported crypto budget fields: {', '.join(sorted(unsupported))}")
    return CryptoBudget(
        wall_seconds=bounded("wall_seconds", DEFAULT_WALL_SECONDS, MAX_WALL_SECONDS),
        memory_mb=bounded("memory_mb", DEFAULT_MEMORY_MB, MAX_MEMORY_MB, 256),
        output_bytes=bounded("output_bytes", DEFAULT_OUTPUT_BYTES, MAX_OUTPUT_BYTES, 1024),
    )
