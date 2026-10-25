"""Opt-in S05 check: actual HTTP upload, activation and worker byte equality."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from echo_agent.server.app import create_app
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.storage import SQLiteProjectStore


class CryptoS05DockerSmoke(unittest.TestCase):
    def test_uploaded_bytes_reach_worker(self) -> None:
        container = f"echo-agent-s05-{uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            runtime = DockerProjectRuntime(
                image="echo-agent-worker:crypto-s03",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                project_data_root=root / "projects",
            )
            raw = b"n=0x0037\r\n\x00\xff\n"
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
                app = create_app(store)
            try:
                with TestClient(app) as http:
                    created = http.post("/projects", json={"name": "s05-smoke", "docker": container,
                        "task_type": "crypto", "objective": "factor n"})
                    self.assertEqual(created.status_code, 201, created.text)
                    project_id = created.json()["id"]
                    uploaded = http.post(f"/projects/{project_id}/inputs",
                        params={"filename": "challenge.bin", "role": "parameters"},
                        content=raw, headers={"Content-Type": "application/octet-stream"})
                    self.assertEqual(uploaded.status_code, 201, uploaded.text)
                    reference = uploaded.json()
                    activated = http.patch(f"/projects/{project_id}/status", json={"status": "active"})
                    self.assertEqual(activated.status_code, 200, activated.text)
                    worker_path = f"/home/kali/workspace/{reference['path']}"
                    output = subprocess.run(
                        ["docker", "exec", container, "python3", "-c",
                         "import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_bytes().hex())", worker_path],
                        check=True, capture_output=True, text=True,
                    )
                    self.assertEqual(output.stdout.strip(), raw.hex())
                    self.assertEqual(store.get_project(project_id).crypto["inputs"], [reference])
            finally:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
