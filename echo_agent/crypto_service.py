"""Shared crypto save invariants for HTTP and the web background dispatcher."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from .crypto_files import CryptoProjectFiles
from .crypto_verification import (DEFAULT_REGISTRY, DEFAULT_VERIFY_SECONDS, MAX_VERIFY_SECONDS,
                                  VerificationRegistry)
from .crypto_verify import answer_is_verified, verify_candidate
from .models import Project, ProjectStatus


@dataclass(frozen=True)
class CryptoProjectService:
    files: CryptoProjectFiles
    aes_image: str | None = None
    registry: VerificationRegistry = field(default_factory=lambda: DEFAULT_REGISTRY)

    def verify(self, project: Project, candidate_fact_id: int, *,
               verification_id: str | None = None,
               timeout_seconds: float | None = None) -> dict:
        verification_id = verification_id or uuid4().hex
        timeout = DEFAULT_VERIFY_SECONDS if timeout_seconds is None else timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 1 <= timeout <= MAX_VERIFY_SECONDS):
            raise ValueError(f"verification timeout_seconds must be between 1 and {MAX_VERIFY_SECONDS}")
        lease = self.registry.acquire(project.id, verification_id, float(timeout))
        try:
            return verify_candidate(
                project,
                candidate_fact_id,
                self.files,
                aes_image=self.aes_image,
                deadline=lease.deadline,
                cancel_event=lease.cancel_event,
                verification_id=verification_id,
                timeout_seconds=lease.timeout_seconds,
            )
        finally:
            self.registry.release(project.id, verification_id)

    def cancel(self, verification_id: str) -> bool:
        return self.registry.cancel(verification_id)

    def _verify_for_save(self, project: Project, candidate_fact_id: int) -> dict:
        """Completion re-check on the save path.

        It keeps a deadline but does not take the registry slot: a background save must not
        fail with 409 just because an operator started a manual verification.
        """
        timeout = DEFAULT_VERIFY_SECONDS
        return verify_candidate(
            project,
            candidate_fact_id,
            self.files,
            aes_image=self.aes_image,
            deadline=time.monotonic() + timeout,
            timeout_seconds=timeout,
        )

    def validate_save(self, old: Project, project: Project) -> None:
        if old.task_type == "crypto":
            if project.task_type != "crypto" or project.crypto.get("inputs", []) != old.crypto.get("inputs", []):
                raise ValueError("crypto inputs can only be added through the upload endpoint")
            for old_items, items in ((old.facts, project.facts), (old.intents, project.intents)):
                for item_id, item in items.items():
                    if item_id in old_items and old_items[item_id].crypto and not item.crypto:
                        raise ValueError(f"crypto metadata missing for existing item {item_id}")
        elif project.task_type == "crypto" and project.crypto.get("inputs"):
            raise ValueError("crypto inputs can only be added through the upload endpoint")
        if project.task_type != "crypto":
            return
        for reference in project.crypto.get("inputs", []):
            self.files.verify_ref(project.id, reference, "input")
        if project.status == ProjectStatus.COMPLETED:
            goal = project.facts.get(1)
            prefix = "result\nanswer: "
            if goal is None or not goal.description.startswith(prefix):
                raise ValueError("crypto completion needs a verified answer in the goal fact")
            answer = goal.description[len(prefix):]
            if not answer or not answer_is_verified(project, answer, lambda fact_id: self._verify_for_save(project, fact_id)):
                raise ValueError("crypto completion requires current independent verification satisfying the project goal")
