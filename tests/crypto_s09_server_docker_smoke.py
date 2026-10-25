"""Real Docker fallback through the server's candidate verification endpoint."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.models import FactKind, Project
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore


class RuntimeStub:
    image = os.environ.get("ECHO_AGENT_TEST_IMAGE", "echo-agent-worker:crypto-20261022")

    def prepare_shared_resources(self) -> None:
        pass


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = SQLiteProjectStore(root / "state.db")
    files = CryptoProjectFiles(root / "projects")
    inputs, _ = files.ensure_layout(7)
    plaintext = bytes(16)
    manifest = {
        "algorithm": "aes", "mode": "gcm", "nonce_hex": "00" * 12,
        "ciphertext_hex": "0388dace60b6a392f328c2b971b2fe78",
        "tag_hex": "ab6e47d42cec13bdf53a67b21257bddf",
        "known_plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
    }
    path = inputs / "aes.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    ref = files.describe_input(7, "input-1", "parameters", "inputs/aes.json")
    project = Project(id=7, name="aes", task_type="crypto",
                      crypto={"schema_version": 1, "inputs": [ref], "goal": {"answer_encoding": "hex"}})
    project.add_fact("start", fact_id=0)
    project.add_fact("recover plaintext", fact_id=1)
    candidate = project.add_fact("known vector", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate", "source_input_ids": ["input-1"],
        "candidate": {"scheme": "aes", "key_hex": "00" * 16,
                      "answer": "00" * 16, "answer_encoding": "hex"},
    })
    store.save_project(project)
    with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
        app = create_app(store)
    with TestClient(app) as http:
        endpoint = f"/projects/7/candidates/{candidate.id}/verify"
        good = http.post(endpoint)
        assert good.status_code == 200 and good.json()["status"] == "pass", good.text
        manifest["tag_hex"] = "00" * 16
        path.write_text(json.dumps(manifest), encoding="utf-8")
        project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/aes.json")
        store.save_project(project)
        bad = http.post(endpoint)
        assert bad.status_code == 200 and bad.json()["status"] == "fail", bad.text
    print("server AES Docker fallback pass/fail verified")
