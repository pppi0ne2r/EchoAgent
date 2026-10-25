"""Opt-in real Docker lifecycle check for S04 (not part of the default suite)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.models import ProjectStatus
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.storage import SQLiteProjectStore


class CryptoS04DockerSmoke(unittest.TestCase):
    def test_completed_project_keeps_inputs_and_artifacts(self) -> None:
        container = f"echo-agent-s04-{uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            files = CryptoProjectFiles(root / "projects")
            project = store.create_project(name="s04-smoke", docker=container, task_type="crypto")
            inputs, _ = files.ensure_layout(project.id)
            (inputs / "rsa.txt").write_bytes(b"n=55\n")
            input_ref = files.describe_input(project.id, "input-1", "parameters", "inputs/rsa.txt")
            project.crypto["inputs"].append(input_ref)
            store.save_project(project)
            runtime = DockerProjectRuntime(
                image="echo-agent-worker:crypto-s03",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                project_data_root=root / "projects",
            )
            try:
                project.activate()
                runtime.apply_status_transition(project, ProjectStatus.INACTIVE)
                store.save_project(project)
                command = [
                    "docker", "exec", container, "sh", "-lc",
                    "mkdir -p /home/kali/workspace/intents/0 && "
                    "cp /home/kali/workspace/inputs/rsa.txt "
                    "/home/kali/workspace/intents/0/result.txt && "
                    "! printf bad >> /home/kali/workspace/inputs/rsa.txt 2>/dev/null",
                ]
                subprocess.run(command, check=True, capture_output=True, text=True)
                artifact = files.describe_artifact(project.id, 0, "artifacts/intents/0/result.txt")
                project.start()
                store.save_project(project)
                project.complete()
                runtime.apply_status_transition(project, ProjectStatus.RUNNING)
                store.save_project(project)
                reloaded = SQLiteProjectStore(root / "state.db").get_project(project.id)
                self.assertEqual(reloaded.status, ProjectStatus.COMPLETED)
                self.assertEqual(files.verify_ref(project.id, input_ref, "input").read_bytes(), b"n=55\n")
                self.assertEqual(files.verify_ref(project.id, artifact, "artifact").read_bytes(), b"n=55\n")
                state = subprocess.run(["docker", "inspect", container], capture_output=True, text=True)
                self.assertNotEqual(state.returncode, 0)
            finally:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
