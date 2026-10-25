"""Immutable identities for the facts a crypto computation actually consumed."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def premise_versions(project: Any, fact_ids: list[int]) -> dict[str, str]:
    cache: dict[int, str] = {}

    def digest(fact_id: int, visiting: set[int]) -> str:
        if fact_id in visiting or len(visiting) >= 64:
            raise ValueError("crypto premise dependencies contain a cycle or exceed depth 64")
        if fact_id in cache:
            return cache[fact_id]
        fact = project.facts.get(fact_id)
        if fact is None:
            raise ValueError(f"crypto premise references unknown fact {fact_id}")
        metadata = fact.crypto
        payload = {
            "description": fact.description,
            "kind": fact.kind.value,
            "typed_value": metadata.get("typed_value"),
            "source_input_ids": sorted(metadata.get("source_input_ids", [])),
            "claim_status": metadata.get("claim_status"),
            "candidate": metadata.get("candidate"),
            "artifact_refs": metadata.get("artifact_refs", []),
            "dependencies": sorted(digest(parent, visiting | {fact_id})
                                   for parent in metadata.get("depends_on_fact_ids", [])),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cache[fact_id] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return cache[fact_id]

    return {str(fact_id): digest(fact_id, set()) for fact_id in fact_ids}


def premises_current(project: Any, metadata: dict[str, Any]) -> bool:
    facts = metadata.get("input_fact_ids", [])
    if not facts:
        return True
    return metadata.get("premise_versions") == premise_versions(project, facts)
