"""Bounded, cancellable execution of independent crypto verification.

The verifier runs in the server request thread (and, for the web dispatcher, in the background
runner).  Without an explicit deadline it can outlive the HTTP request, ignore a project stop and
run alongside other verification containers.  This module owns the shared limits and the
cancellation handles; the verifier signals them through ``check_verification()``.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


DEFAULT_VERIFY_SECONDS = 60
MAX_VERIFY_SECONDS = 300
DEFAULT_MAX_CONCURRENT = 2


class VerificationError(RuntimeError):
    """Base class for verification lifecycle failures."""


class VerificationBusy(VerificationError):
    """Another verification already holds the project slot or the global slot."""


class VerificationTimeout(VerificationError):
    """The verification exceeded its wall-clock deadline."""


class VerificationCancelled(VerificationError):
    """The verification was cancelled by the operator or by a project stop."""


@dataclass(frozen=True, slots=True)
class VerificationLease:
    verification_id: str
    deadline: float
    cancel_event: threading.Event
    started_at: float
    timeout_seconds: float


class VerificationRegistry:
    """Process-wide concurrency limits plus the cancellation handles of running checks."""

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self._global = threading.BoundedSemaphore(max_concurrent)
        self._project_locks: dict[int, threading.Lock] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _project_lock(self, project_id: int) -> threading.Lock:
        with self._lock:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_id] = lock
            return lock

    def acquire(self, project_id: int, verification_id: str, timeout_seconds: float) -> VerificationLease:
        if not isinstance(verification_id, str) or not verification_id:
            raise ValueError("verification_id must be a non-empty string")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 1 <= timeout_seconds <= MAX_VERIFY_SECONDS):
            raise ValueError(f"verification timeout_seconds must be between 1 and {MAX_VERIFY_SECONDS}")
        if not self._global.acquire(blocking=False):
            raise VerificationBusy("global verification concurrency limit reached")
        project_lock = self._project_lock(project_id)
        if not project_lock.acquire(blocking=False):
            self._global.release()
            raise VerificationBusy(f"another verification is already running for project {project_id}")
        event = threading.Event()
        with self._lock:
            if verification_id in self._events:
                project_lock.release()
                self._global.release()
                raise VerificationBusy(f"verification id {verification_id} is already running")
            self._events[verification_id] = event
        started = time.monotonic()
        return VerificationLease(
            verification_id=verification_id,
            deadline=started + float(timeout_seconds),
            cancel_event=event,
            started_at=started,
            timeout_seconds=float(timeout_seconds),
        )

    def release(self, project_id: int, verification_id: str) -> None:
        with self._lock:
            self._events.pop(verification_id, None)
        self._project_lock(project_id).release()
        self._global.release()

    def cancel(self, verification_id: str) -> bool:
        with self._lock:
            event = self._events.get(verification_id)
        if event is None:
            return False
        event.set()
        return True

    def active(self) -> list[str]:
        with self._lock:
            return list(self._events)


DEFAULT_REGISTRY = VerificationRegistry()


_context = threading.local()


@contextmanager
def verification_scope(deadline: float | None, cancel_event: threading.Event | None) -> Iterator[None]:
    """Bind the current thread's deadline/cancel handle for the verifier's inner loops."""
    previous = getattr(_context, "current", None)
    _context.current = (deadline, cancel_event)
    try:
        yield
    finally:
        _context.current = previous


def check_verification() -> None:
    """Raise when the current verification was cancelled or ran past its deadline."""
    current = getattr(_context, "current", None)
    if current is None:
        return
    deadline, cancel_event = current
    if cancel_event is not None and cancel_event.is_set():
        raise VerificationCancelled("verification cancelled")
    if deadline is not None and time.monotonic() > deadline:
        raise VerificationTimeout("verification exceeded its wall-clock deadline")
