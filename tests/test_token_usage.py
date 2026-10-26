from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.token_usage import SESSION_READER_SCRIPT, parse_tokens_used_total
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.models import Project
from echo_agent.server.app import create_app
from echo_agent.server.dispatcher_runner import StoreDispatcherClient
from echo_agent.storage import SQLiteProjectStore


def _usage(**overrides):
    payload = {
        "mode": "explore",
        "intent_id": 0,
        "model": "grok-4.7",
        "session_id": "session-1",
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": 120,
        "source": "session",
    }
    payload.update(overrides)
    return payload


class TokenUsageStoreTests(unittest.TestCase):
    def test_summary_dedup_reset_and_log_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="pwn", objective="获取 flag")
            first = store.add_token_usage(project.id, _usage())
            duplicate = store.add_token_usage(project.id, _usage(total_tokens=999))
            second = store.add_token_usage(project.id, _usage(
                mode="reason",
                intent_id=None,
                session_id="session-2",
                input_tokens=10,
                cached_input_tokens=0,
                output_tokens=2,
                reasoning_output_tokens=0,
                total_tokens=12,
            ))
            text_row = store.add_token_usage(project.id, _usage(
                mode="writeup",
                intent_id=None,
                session_id="",
                source="text",
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=8,
            ))

            self.assertEqual(first["id"], duplicate["id"])
            self.assertEqual(duplicate["total_tokens"], 120)
            self.assertIsNone(second["intent_id"])
            self.assertEqual(text_row["source"], "text")
            summary = store.token_usage_summary(project.id)
            self.assertEqual(summary["total_tokens"], 140)
            self.assertEqual(summary["input_tokens"], 110)
            self.assertEqual(summary["cached_input_tokens"], 40)
            self.assertEqual(summary["by_mode"]["explore"]["runs"], 1)
            self.assertEqual(summary["by_mode"]["reason"]["total_tokens"], 12)
            self.assertIsNone(store.add_token_usage(project.id, _usage(total_tokens=0)))

            store.add_project_log(project.id, "keep me")
            store.clear_project_logs(project.id)
            self.assertEqual(store.token_usage_summary(project.id)["total_tokens"], 140)
            self.assertEqual(store.list_project_logs(project.id), [])

            store.reset_project_progress(project.id)
            self.assertEqual(store.token_usage_summary(project.id)["runs"], [])
            self.assertEqual(
                [item["message"] for item in store.list_project_logs(project.id)],
                ["已清空进度，下次运行将重新开始"],
            )

            store.add_token_usage(project.id, _usage(session_id="after-reset"))
            store.delete_project(project.id)
            with store._connect() as conn:
                remaining = conn.execute("SELECT COUNT(*) AS count FROM project_token_usage").fetchone()["count"]
            self.assertEqual(remaining, 0)

    def test_api_returns_summary_and_rejects_invalid_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="web", objective="找到漏洞")
            app = create_app(store)

            async def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
                payload = b"" if body is None else json.dumps(body).encode("utf-8")
                messages: list[dict] = []

                async def receive():
                    return {"type": "http.request", "body": payload, "more_body": False}

                async def send(message):
                    messages.append(message)

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": method,
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode("utf-8"),
                    "query_string": b"",
                    "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 123),
                    "server": ("127.0.0.1", 80),
                }
                await app(scope, receive, send)
                status = next(item["status"] for item in messages if item["type"] == "http.response.start")
                raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
                return status, json.loads(raw.decode("utf-8"))

            missing_status, _missing = asyncio.run(call("GET", "/projects/999/token-usage"))
            saved_status, saved = asyncio.run(call("POST", f"/projects/{project.id}/token-usage", _usage(session_id="api-1")))
            invalid_status, _invalid = asyncio.run(call("POST", f"/projects/{project.id}/token-usage", _usage(mode="Bad Mode")))
            summary_status, summary = asyncio.run(call("GET", f"/projects/{project.id}/token-usage"))
            self.assertEqual(missing_status, 404)
            self.assertEqual(saved_status, 200)
            self.assertEqual(saved["total_tokens"], 120)
            self.assertEqual(invalid_status, 400)
            self.assertEqual(summary_status, 200)
            self.assertEqual(summary["total_tokens"], 120)
            self.assertEqual(summary["by_mode"]["explore"]["cached_input_tokens"], 40)


class TokenUsageCollectionTests(unittest.TestCase):
    def test_session_script_reads_last_cumulative_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "rollout-old.jsonl"
            newer = root / "nested"
            newer.mkdir()
            target = newer / "rollout-new.jsonl"
            older.write_text(json.dumps({
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 1}}},
            }) + "\n", encoding="utf-8")
            target.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"id": "sess-9"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}),
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 13,
                        }},
                    },
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 30,
                            "cached_input_tokens": 12,
                            "output_tokens": 6,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 36,
                        }},
                    },
                }),
            ]) + "\n", encoding="utf-8")
            import os
            os.utime(older, (1, 1))
            os.utime(target, (2, 2))
            completed = subprocess.run(
                [sys.executable, "-", str(root)],
                input=SESSION_READER_SCRIPT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["session_id"], "sess-9")
            self.assertEqual(payload["total_tokens"], 36)
            self.assertEqual(payload["cached_input_tokens"], 12)

    def test_text_footer_uses_the_last_total(self) -> None:
        text = "tokens used\n1,000\nlater\ntokens used\n360,588\n"
        self.assertEqual(parse_tokens_used_total(text), 360588)
        self.assertIsNone(parse_tokens_used_total("no usage here"))

    def test_agent_prefers_session_and_records_once(self) -> None:
        saved: list[tuple[int, dict]] = []
        agent = CodexDockerAgent(model="grok-4.7")
        agent.on_token_usage = lambda project_id, usage: saved.append((project_id, usage))
        agent._read_session_usage = lambda project, path: {
            "session_id": "sess-1",
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 2,
            "reasoning_output_tokens": 1,
            "total_tokens": 12,
        }
        project = Project(id=7, name="pwn", docker="echo-agent-task-7")
        agent._run_state.usage_captured = False
        agent._run_state.stderr_lines = ["tokens used", "99"]
        usage = agent._capture_token_usage(project, DispatchMode.EXPLORE, 0, "intents/0", None)
        again = agent._capture_token_usage(project, DispatchMode.EXPLORE, 0, "intents/0", None)
        self.assertEqual(usage["source"], "session")
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(usage["intent_id"], 0)
        self.assertEqual(usage["mode"], "explore")
        self.assertEqual(again, usage)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0], 7)

    def test_agent_falls_back_to_text_and_skips_empty_usage(self) -> None:
        saved: list[dict] = []
        agent = CodexDockerAgent(model="grok-4.7")
        agent.on_token_usage = lambda project_id, usage: saved.append(usage)
        agent._read_session_usage = lambda project, path: None
        project = Project(id=7, name="pwn", docker="echo-agent-task-7")
        agent._run_state.usage_captured = False
        agent._run_state.stderr_lines = ["codex", "tokens used", "15,052"]
        usage = agent._capture_token_usage(project, DispatchMode.REASON, None, "reason", None)
        self.assertEqual(usage["source"], "text")
        self.assertEqual(usage["total_tokens"], 15052)
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["session_id"], "")

        agent._run_state.usage_captured = False
        agent._run_state.stderr_lines = ["no footer"]
        self.assertIsNone(agent._capture_token_usage(project, DispatchMode.REASON, None, "reason", None))
        self.assertEqual(len(saved), 1)

    def test_dispatcher_and_store_client_receive_usage_sink(self) -> None:
        agent = CodexDockerAgent()
        client_calls: list[tuple[int, dict]] = []

        class Client:
            def add_token_usage(self, project_id: int, usage: dict) -> None:
                client_calls.append((project_id, usage))

        client = Client()
        dispatcher = Dispatcher(client, agent=agent)
        self.assertIs(dispatcher.agent.on_token_usage.__func__, client.add_token_usage.__func__)
        self.assertIs(dispatcher.agent.on_token_usage.__self__, client)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="pwn", objective="flag")
            bound = StoreDispatcherClient(store)
            saved = bound.add_token_usage(project.id, _usage(session_id="client-1"))
            self.assertEqual(saved["project_id"], project.id)
            self.assertEqual(store.token_usage_summary(project.id)["total_tokens"], 120)


if __name__ == "__main__":
    unittest.main()
