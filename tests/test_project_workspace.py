from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.models import Project, ProjectStatus
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.server.project_workspace import ProjectWorkspace
from echo_agent.storage import ProjectConflictError, SQLiteProjectStore


class ProjectWorkspaceTests(unittest.TestCase):
    def test_project_folder_keeps_attachment_log_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            workspace = ProjectWorkspace(root / "workspace", legacy_uploads=root / "uploads")
            workspace.bind_store(store)
            project = store.create_project(name=" ../pwn heap ", objective="拿到 shell")
            workspace.write_record(project)
            host_path, container_path = workspace.save_upload(project, "../../libc.so", b"elf")
            store.set_project_target(project.id, container_path)
            store.add_project_log(project.id, "开始分析附件", metadata={"step": 1})

            folder = root / "workspace" / "pwn heap"
            record = json.loads((folder / "records" / "project.json").read_text(encoding="utf-8"))
            log_line = json.loads((folder / "logs" / "runtime.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(host_path, folder / "attachments" / "libc.so")
            self.assertEqual(container_path, "/home/kali/attachments/libc.so")
            self.assertEqual(record["name"], "../pwn heap")
            self.assertIn("/home/kali/attachments/libc.so", record["target_url"])
            self.assertEqual(log_line["message"], "开始分析附件")
            self.assertTrue((folder / "work").is_dir())

            store.clear_project_logs(project.id)
            self.assertEqual((folder / "logs" / "runtime.jsonl").read_text(encoding="utf-8"), "")

    def test_same_name_and_rename_do_not_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = ProjectWorkspace(root / "workspace")
            first = Project(id=1, name="task", objective="one")
            second = Project(id=2, name="task", objective="two")
            workspace.write_record(first)
            workspace.write_record(second)
            self.assertTrue((root / "workspace" / "task" / "records" / "project.json").is_file())
            self.assertTrue((root / "workspace" / "task-2" / "records" / "project.json").is_file())

            renamed = Project(id=2, name="task-renamed", objective="two")
            workspace.write_record(renamed)
            self.assertFalse((root / "workspace" / "task-2").exists())
            saved = json.loads((root / "workspace" / "task-renamed" / "records" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["id"], 2)

            workspace.remove(2)
            self.assertFalse((root / "workspace" / "task-renamed").exists())
            self.assertTrue((root / "workspace" / "task").is_dir())

    def test_legacy_upload_moves_into_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "uploads" / "4"
            legacy.mkdir(parents=True)
            (legacy / "challenge.bin").write_bytes(b"bin")
            workspace = ProjectWorkspace(root / "workspace", legacy_uploads=root / "uploads")
            project = Project(id=4, name="rev-easy")
            workspace.write_record(project)
            self.assertEqual((root / "workspace" / "rev-easy" / "attachments" / "challenge.bin").read_bytes(), b"bin")
            self.assertFalse(legacy.exists())

    def test_container_mounts_project_work_and_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = ProjectWorkspace(root / "workspace")
            project = Project(id=7, name="rev", docker="echo-agent-task-7")
            workspace.save_upload(project, "challenge.bin", b"bin")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                workspace=workspace,
            )
            command = " ".join(runtime._docker_run_command("echo-agent-task-7", runtime._project_workspace_mounts(project)))
            work = (root / "workspace" / "rev" / "work").resolve()
            attachments = (root / "workspace" / "rev" / "attachments").resolve()
            self.assertIn(f"{work}:/home/kali/workspace:rw", command)
            self.assertIn(f"{attachments}:/home/kali/attachments:ro", command)


    def test_reset_progress_keeps_setup_and_clears_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            workspace = ProjectWorkspace(root / "workspace")
            workspace.bind_store(store)
            files = CryptoProjectFiles(root / "projects")
            project = store.create_project(
                name="pwn",
                target_url="160.202.254.160:14563\n/home/kali/attachments/pwn",
                hint="libc2.23",
                objective="获取 flag",
                task_type="crypto",
            )
            inputs, intents = files.ensure_layout(project.id)
            (inputs / "challenge.txt").write_bytes(b"n=55")
            project.crypto["inputs"].append(
                files.describe_input(project.id, "input-1", "parameters", "inputs/challenge.txt")
            )
            project.crypto["verifications"] = [{"status": "pass", "intent_id": 0}]
            project.crypto["acceptances"] = []
            project.add_fact("binary is pie", fact_id=2)
            project.add_intent("checksec", from_fact_id=0, to_fact_id=1)
            project.status = ProjectStatus.COMPLETED
            store.save_project(project)
            store.add_project_log(project.id, "old log")
            folder = workspace.find(project.id)
            assert folder is not None
            (folder / "work" / "intents").mkdir()
            (folder / "work" / "intents" / "note.txt").write_text("scratch", encoding="utf-8")
            (folder / "records" / "wp.md").write_text("# wp", encoding="utf-8")
            (folder / "attachments" / "pwn").write_bytes(b"elf")
            artifact = intents / "0"
            artifact.mkdir()
            (artifact / "result.txt").write_text("old", encoding="utf-8")

            reset = store.reset_project_progress(project.id)
            workspace.clear_runtime(reset)
            files.clear_artifacts(project.id)

            self.assertEqual(reset.status, ProjectStatus.ACTIVE)
            self.assertEqual(reset.name, "pwn")
            self.assertIn("/home/kali/attachments/pwn", reset.target_url or "")
            self.assertEqual(reset.hint, "libc2.23")
            self.assertEqual(reset.objective, "获取 flag")
            self.assertEqual(set(reset.facts), {0, 1})
            self.assertEqual(reset.facts[0].kind.value, "start")
            self.assertIn("libc2.23", reset.facts[0].description)
            self.assertEqual(reset.facts[1].description, "获取 flag")
            self.assertEqual(reset.intents, {})
            self.assertEqual(reset.crypto["inputs"][0]["id"], "input-1")
            self.assertNotIn("verifications", reset.crypto)
            self.assertNotIn("acceptances", reset.crypto)
            self.assertFalse((folder / "work" / "intents").exists())
            self.assertFalse((folder / "records" / "wp.md").exists())
            self.assertEqual((folder / "attachments" / "pwn").read_bytes(), b"elf")
            self.assertTrue((inputs / "challenge.txt").is_file())
            self.assertFalse((intents).exists())
            messages = [item["message"] for item in store.list_project_logs(project.id)]
            self.assertEqual(messages, ["已清空进度，下次运行将重新开始"])
            record = json.loads((folder / "records" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "active")
            self.assertEqual([fact["id"] for fact in record["facts"]], [0, 1])

            running = store.get_project(project.id)
            running.status = ProjectStatus.RUNNING
            store.save_project(running)
            with self.assertRaises(ProjectConflictError):
                store.reset_project_progress(project.id)


if __name__ == "__main__":
    unittest.main()
