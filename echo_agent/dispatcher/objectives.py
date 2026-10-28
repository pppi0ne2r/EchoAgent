from __future__ import annotations

import re

_VULN_OBJECTIVE = re.compile(r"漏洞|vuln(?:erabilit(?:y|ies))?|findings?", re.IGNORECASE)
_MIN_COUNT = re.compile(r"(?:至少|不少于|at\s+least|>=)\s*(\d+)", re.IGNORECASE)


def required_finding_count(objective: str | None) -> int:
    """How many distinct vulnerability facts the objective requires.

    Ordinary single-answer tasks stay at 1. Only an objective that both asks
    for vulnerabilities and states a minimum count above 1 uses the
    multi-finding completion path.
    """
    text = str(objective or "")
    if not _VULN_OBJECTIVE.search(text):
        return 1
    match = _MIN_COUNT.search(text)
    if match is None:
        return 1
    count = int(match.group(1))
    return count if count > 1 else 1
