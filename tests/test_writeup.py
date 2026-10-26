from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.models import Project, ProjectStatus
from echo_agent.server.project_workspace import ProjectWorkspace
from echo_agent.server.writeup import WriteupService, condense_runtime_log
from echo_agent.storage import SQLiteProjectStore


class WriteupLogTests(unittest.TestCase):
    def test_condense_drops_noise_and_keeps_tail(self) -> None:
        lines = [
            json.dumps({"message": "codex agent still running"}, ensure_ascii=False),
            json.dumps({"message": "codex preflight ok"}, ensure_ascii=False),
            "not json",
            json.dumps({"message": "codex output:\n" + "x" * 5000}, ensure_ascii=False),
            json.dumps({"message": "探索完成"}, ensure_ascii=False),
        ]
        text = condense_runtime_log("\n".join(lines), limit=100_000)

        self.assertNotIn("still running", text)
        self.assertNotIn("preflight", text)
        self.assertNotIn("not json", text)
        self.assertIn("[truncated]", text)
        self.assertIn("探索完成", text)
        self.assertLess(len(text), 5000)

        bulky = "\n".join(
            json.dumps({"message": f"line-{index}-" + "a" * 80}, ensure_ascii=False)
            for index in range(30)
        )
        tail = condense_runtime_log(bulky, limit=300)
        self.assertLessEqual(len(tail), 300)
        self.assertNotIn("line-0-", tail)
        self.assertIn("line-29-", tail)


class WriteupPromptTests(unittest.TestCase):
    def test_writeup_prompt_is_read_only_and_single_file(self) -> None:
        project = Project(id=3, name="web-easy", status=ProjectStatus.ACTIVE)
        project.target_url = "http://target.example"
        project.objective = "拿到 flag"

        prompt = PromptBuilder().build_writeup_prompt(project)

        self.assertIn("不要继续做题", prompt)
        self.assertIn("不要运行利用、扫描或攻击命令", prompt)
        self.assertIn("/home/kali/workspace/writeup-input/", prompt)
        self.assertIn("/home/kali/workspace/writeup.md", prompt)
        self.assertIn("题目信息、结论、解题步骤、可复现 Payload、关键结果、flag", prompt)
        self.assertIn("可复现 Payload 必须照抄材料中的原始命令", prompt)
        self.assertIn("每一步写明目的、具体动作和观察到的结果", prompt)
        self.assertIn("不要编造", prompt)
        self.assertIn("web-easy", prompt)
        self.assertIn("http://target.example", prompt)
        self.assertIn("拿到 flag", prompt)


class _Runtime:
    def __init__(self) -> None:
        self.ensured: list[int] = []

    def ensure_project_container(self, project: Project, log=None) -> None:
        self.ensured.append(project.id)
        if log is not None:
            log("docker container already running")


class WriteupServiceTests(unittest.TestCase):
    def _service(self, agent_factory):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        store = SQLiteProjectStore(root / "state.db")
        workspace = ProjectWorkspace(root / "workspace")
        workspace.bind_store(store)
        project = store.create_project(
            name="demo",
            target_url="http://target.example",
            objective="拿到 flag",
        )
        store.add_project_log(project.id, "codex agent still running")
        store.add_project_log(project.id, "codex output:\nflag{demo}")
        runtime = _Runtime()
        service = WriteupService(store, workspace, runtime, agent_factory=agent_factory)
        return directory, service, workspace, store, project, runtime

    def test_success_copies_writeup_and_condenses_log(self) -> None:
        calls: list[tuple[object, str]] = []

        def agent_factory():
            class Agent:
                def run(self, project, prompt, mode, log=None, intent_id=None, runtime_key=None):
                    calls.append((mode, runtime_key, prompt))
                    target = workspace.work_dir(project) / "writeup.md"
                    target.write_text("# 题目信息\nflag{demo}\n", encoding="utf-8")
                    if log is not None:
                        log("codex output:\nflag{demo}")

            return Agent()

        directory, service, workspace, store, project, runtime = self._service(agent_factory)
        with directory:
            service.start(project.id)
            service._threads[project.id].join(5)

            status = service.status(project.id)
            content = service.record_path(project).read_text(encoding="utf-8")
            condensed = (workspace.work_dir(project) / "writeup-input" / "runtime.jsonl").read_text(encoding="utf-8")
            messages = [item["message"] for item in store.list_project_logs(project.id)]
            ensured = list(runtime.ensured)

        self.assertEqual(status["status"], "ready")
        self.assertEqual(content, "# 题目信息\nflag{demo}\n")
        self.assertEqual(ensured, [project.id])
        self.assertEqual(calls[0][0], DispatchMode.WRITEUP)
        self.assertEqual(calls[0][1], "writeup")
        self.assertIn("不要继续做题", calls[0][2])
        self.assertNotIn("still running", condensed)
        self.assertIn("flag{demo}", condensed)
        self.assertIn("开始生成 WP", messages)
        self.assertIn("WP 已生成", messages)

    def test_writeup_agent_can_record_token_usage(self) -> None:
        def agent_factory():
            class Agent:
                def run(self, project, prompt, mode, log=None, intent_id=None, runtime_key=None):
                    self.on_token_usage(project.id, {
                        "mode": "writeup",
                        "intent_id": None,
                        "model": "grok-4.7",
                        "session_id": "wp-1",
                        "input_tokens": 15,
                        "cached_input_tokens": 5,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 19,
                        "source": "session",
                    })
                    (workspace.work_dir(project) / "writeup.md").write_text("ok\n", encoding="utf-8")

            return Agent()

        directory, service, workspace, store, project, _runtime = self._service(agent_factory)
        with directory:
            service.start(project.id)
            service._threads[project.id].join(5)
            summary = store.token_usage_summary(project.id)
        self.assertEqual(summary["total_tokens"], 19)
        self.assertEqual(summary["by_mode"]["writeup"]["cached_input_tokens"], 5)

    def test_nested_writeup_path_is_accepted(self) -> None:
        def agent_factory():
            class Agent:
                def run(self, project, prompt, mode, log=None, intent_id=None, runtime_key=None):
                    target = workspace.work_dir(project) / "writeup" / "writeup.md"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("nested\n", encoding="utf-8")

            return Agent()

        directory, service, workspace, _store, project, _runtime = self._service(agent_factory)
        with directory:
            service._generate(project.id)
            self.assertEqual(service.record_path(project).read_text(encoding="utf-8"), "nested\n")
            self.assertEqual(service.status(project.id)["status"], "ready")

    def test_missing_output_is_recorded_as_error(self) -> None:
        def agent_factory():
            class Agent:
                def run(self, project, prompt, mode, log=None, intent_id=None, runtime_key=None):
                    return None

            return Agent()

        directory, service, _workspace, store, project, _runtime = self._service(agent_factory)
        with directory:
            service.start(project.id)
            service._threads[project.id].join(5)
            status = service.status(project.id)
            messages = [item["message"] for item in store.list_project_logs(project.id)]

        self.assertEqual(status["status"], "error")
        self.assertIn("没有写入 writeup.md", status["error"])
        self.assertTrue(any(message.startswith("生成 WP 失败") for message in messages))

    def test_second_start_reuses_running_job(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def agent_factory():
            class Agent:
                def run(self, project, prompt, mode, log=None, intent_id=None, runtime_key=None):
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError("writeup test was not released")
                    (workspace.work_dir(project) / "writeup.md").write_text("ok\n", encoding="utf-8")

            return Agent()

        directory, service, workspace, _store, project, _runtime = self._service(agent_factory)
        with directory:
            first = service.start(project.id)
            self.assertTrue(started.wait(2))
            second = service.start(project.id)
            self.assertEqual(second["status"], "running")
            self.assertEqual(len([thread for thread in service._threads.values() if thread.is_alive()]), 1)
            release.set()
            service._threads[project.id].join(5)
            final = service.status(project.id)["status"]

        self.assertEqual(first["status"], "running")
        self.assertEqual(final, "ready")


if __name__ == "__main__":
    unittest.main()
