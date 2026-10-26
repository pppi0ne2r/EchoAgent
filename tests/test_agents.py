from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.types import DispatchMode


class CodexDockerAgentCommandTests(unittest.TestCase):
    def test_proxy_base_url_is_configured_as_explicit_provider(self) -> None:
        agent = CodexDockerAgent(
            model="test-model",
            openai_api_key="test-key",
            openai_base_url="https://proxy.example/v1",
        )

        command = agent._codex_command_for_mode(DispatchMode.FIRST_RUN)

        self.assertIn('model_provider="echo_agent_proxy"', command)
        self.assertIn(
            'model_providers.echo_agent_proxy.base_url="https://proxy.example/v1"',
            command,
        )
        self.assertIn("model_providers.echo_agent_proxy.supports_websockets=false", command)
        self.assertIn("model_providers.echo_agent_proxy.stream_idle_timeout_ms=60000", command)
        self.assertEqual(command[-3:], ("--model", "test-model", "-"))

    def test_docker_command_passes_base_url_as_explicit_env_value(self) -> None:
        agent = CodexDockerAgent(
            model="test-model",
            openai_api_key="test-key",
            openai_base_url="https://proxy.example/v1",
        )
        project = type("ProjectStub", (), {"docker": "worker-container"})()

        command = agent._docker_command(project, DispatchMode.EXPLORE)  # type: ignore[arg-type]
        safe_command = agent._safe_command(command)

        self.assertIn("OPENAI_API_KEY=test-key", command)
        self.assertIn("OPENAI_BASE_URL=https://proxy.example/v1", command)
        self.assertNotIn("OPENAI_API_KEY=test-key", safe_command)
        self.assertNotIn("OPENAI_BASE_URL=https://proxy.example/v1", safe_command)

    def test_default_openai_provider_is_unchanged_without_base_url(self) -> None:
        agent = CodexDockerAgent(model="test-model", openai_api_key="test-key")

        command = agent._codex_command_for_mode(DispatchMode.EXPLORE)

        self.assertFalse(any(item.startswith("model_provider=") for item in command))
        self.assertEqual(command[-3:], ("--model", "test-model", "-"))

    def test_custom_provider_command_is_not_overridden(self) -> None:
        agent = CodexDockerAgent(
            codex_command=(
                "codex",
                "exec",
                "-c",
                'model_provider="custom"',
                "-",
            ),
            openai_base_url="https://proxy.example/v1",
        )

        command = agent._codex_command_for_mode(DispatchMode.REASON)

        self.assertEqual(command.count('model_provider="custom"'), 1)
        self.assertFalse(any("echo_agent_proxy" in item for item in command))

    def test_process_output_keeps_stderr_out_of_result(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        logs: list[str] = []

        output = agent._run_process(
            (
                "sh",
                "-c",
                "cat >/dev/null; echo 'progress line 1' >&2; echo 'progress line 2' >&2; echo '{\"answer\":\"done\",\"fact_description\":\"ok\"}'",
            ),
            prompt="hello",
            mode=DispatchMode.EXPLORE,
            env=None,
            log=lambda message, _level, _metadata: logs.append(message),
        )

        self.assertEqual(output, '{"answer":"done","fact_description":"ok"}')
        self.assertIn("codex output:\nprogress line 1\nprogress line 2", logs)
        self.assertIn('codex result:\n{"answer":"done","fact_description":"ok"}', logs)

    def test_nonzero_reason_process_salvages_json_from_stderr(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        logs: list[tuple[str, str]] = []

        output = agent._run_process(
            (
                "sh",
                "-c",
                "cat >/dev/null; echo '{\"completed\": false, \"next_intents\": [{\"from\": 2, \"to\": null, \"description\": \"枚举目录\"}]}' >&2; echo 'ERROR: stream disconnected' >&2; exit 1",
            ),
            prompt="hello",
            mode=DispatchMode.REASON,
            env=None,
            log=lambda message, level, _metadata: logs.append((level, message)),
        )

        self.assertIn('"completed": false', output)
        self.assertIn('"next_intents"', output)
        self.assertTrue(any(level == "warning" and "salvaged JSON output" in message for level, message in logs))

    def test_nonzero_process_salvages_json_nested_in_event_stream(self) -> None:
        import json

        message = json.dumps(
            {"answer": None, "fact_description": "real fact", "outcome": "verified"},
            ensure_ascii=False,
        )
        script = """
import json, sys
message = %r
event = {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
print(json.dumps(event, ensure_ascii=False))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
sys.exit(1)
""" % message
        agent = CodexDockerAgent(timeout_seconds=5)
        logs: list[tuple[str, str]] = []

        output = agent._run_process(
            ("python3", "-c", script),
            prompt="hello",
            mode=DispatchMode.EXPLORE,
            env=None,
            log=lambda log_message, level, _metadata: logs.append((level, log_message)),
        )

        self.assertIn("real fact", output)
        self.assertNotIn("turn.completed", output)
        self.assertTrue(any(level == "warning" and "salvaged JSON output" in message for level, message in logs))


    def test_hard_timeout_salvages_json_printed_before_hang(self) -> None:
        script = """
import json, sys, time
sys.stdin.read()
print(json.dumps({"answer": None, "fact_description": "font cache rce", "outcome": "verified"}, ensure_ascii=False), flush=True)
print(json.dumps({"answer": None, "fact_description": "upload missing", "outcome": "refuted"}, ensure_ascii=False), flush=True)
time.sleep(5)
"""
        agent = CodexDockerAgent(timeout_seconds=1, no_output_timeout_seconds=0)
        output = agent._run_process(
            ("python3", "-c", script),
            prompt="hello",
            mode=DispatchMode.EXPLORE,
            env=None,
            log=None,
        )
        self.assertIn("font cache rce", output)
        self.assertIn("upload missing", output)

    def test_continuous_output_is_not_failed_by_hard_timeout(self) -> None:
        script = """
import sys, time
sys.stdin.read()
for index in range(8):
    print(f"still working {index}", flush=True)
    time.sleep(0.25)
"""
        agent = CodexDockerAgent(timeout_seconds=1, no_output_timeout_seconds=30)
        output = agent._run_process(
            ("python3", "-c", script),
            prompt="hello",
            mode=DispatchMode.EXPLORE,
            env=None,
            log=None,
        )
        self.assertIn("still working 7", output)

    def test_quiet_process_still_fails_on_no_output_after_hard_timeout(self) -> None:
        script = """
import sys, time
sys.stdin.read()
print("partial before hang", flush=True)
time.sleep(5)
"""
        agent = CodexDockerAgent(timeout_seconds=1, no_output_timeout_seconds=2)
        with self.assertRaises(RuntimeError) as caught:
            agent._run_process(
                ("python3", "-c", script),
                prompt="hello",
                mode=DispatchMode.EXPLORE,
                env=None,
                log=None,
            )
        message = str(caught.exception)
        self.assertIn("no-output timed out", message)
        self.assertNotIn("after 1s", message)
        self.assertIn("partial before hang", message)

    def test_no_output_timeout_keeps_progress_tail_without_promoting_it(self) -> None:
        script = """
import sys, time
sys.stdin.read()
print("partial rce output before hang", flush=True)
time.sleep(5)
"""
        agent = CodexDockerAgent(timeout_seconds=10, no_output_timeout_seconds=1)
        with self.assertRaises(RuntimeError) as caught:
            agent._run_process(
                ("python3", "-c", script),
                prompt="hello",
                mode=DispatchMode.EXPLORE,
                env=None,
                log=None,
            )
        message = str(caught.exception)
        self.assertIn("no-output timed out", message)
        self.assertIn("partial rce output before hang", message)
        self.assertIn("output_tail", message)

    def test_process_is_killed_after_no_output_timeout(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=10, no_output_timeout_seconds=1)
        logs: list[tuple[str, str]] = []

        with self.assertRaisesRegex(RuntimeError, "no-output timed out"):
            agent._run_process(
                (
                    "sh",
                    "-c",
                    "cat >/dev/null; sleep 5",
                ),
                prompt="hello",
                mode=DispatchMode.EXPLORE,
                env=None,
                log=lambda message, level, _metadata: logs.append((level, message)),
            )

        self.assertTrue(
            any(level == "error" and "no-output timed out" in message for level, message in logs)
        )

    def test_mode_specific_no_output_timeout_overrides_default(self) -> None:
        agent = CodexDockerAgent(
            no_output_timeout_seconds=900,
            mode_no_output_timeout_seconds={"explore": 1},
        )

        self.assertEqual(agent._no_output_timeout_for_mode(DispatchMode.EXPLORE), 1)
        self.assertEqual(agent._no_output_timeout_for_mode(DispatchMode.FIRST_RUN), 900)

    def test_mode_specific_timeout_overrides_default(self) -> None:
        agent = CodexDockerAgent(
            timeout_seconds=30,
            mode_timeout_seconds={"first_run": 1},
        )

        self.assertEqual(agent._timeout_for_mode(DispatchMode.FIRST_RUN), 1)
        self.assertEqual(agent._timeout_for_mode(DispatchMode.EXPLORE), 30)

    def test_nonzero_exit_without_json_marks_upstream_overload_transient(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        with self.assertRaisesRegex(RuntimeError, "transient upstream failure"):
            agent._run_process(
                (
                    "sh",
                    "-c",
                    "cat >/dev/null; echo 'provider overloaded while reconnecting' >&2; exit 1",
                ),
                prompt="hello",
                mode=DispatchMode.EXPLORE,
                env=None,
                log=None,
            )



    def test_codex_waits_for_pyghidra_mcp_inside_container(self) -> None:
        agent = CodexDockerAgent()
        project = type("ProjectStub", (), {"docker": "worker-container"})()
        seen: dict[str, list[str]] = {}

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("echo_agent.dispatcher.agents.subprocess.run", side_effect=fake_run):
            agent._wait_for_pyghidra_mcp(project, log=None)  # type: ignore[arg-type]

        self.assertEqual(seen["command"][:4], ["docker", "exec", "-i", "worker-container"])
        self.assertIn("127.0.0.1", seen["command"][-1])
        self.assertIn("8000", seen["command"][-1])

    def test_prepare_runtime_writes_pyghidra_mcp_config(self) -> None:
        agent = CodexDockerAgent(workspace="/home/kali/workspace")
        project = type("ProjectStub", (), {"docker": "worker-container"})()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("echo_agent.dispatcher.agents.subprocess.run", fake_run):
            agent._prepare_runtime(project, "intents/4", log=None)  # type: ignore[arg-type]

        self.assertEqual(len(calls), 1)
        script = calls[0][-1]
        self.assertIn("mcp_servers.pyghidra-mcp", script)
        self.assertIn("http://127.0.0.1:8000/mcp", script)
        self.assertIn("/home/kali/.codex/intents/4/config.toml", script)
        self.assertIn("flock -w 10", script)

    def test_intent_command_uses_isolated_codex_home_and_workspace(self) -> None:
        agent = CodexDockerAgent(workspace="/home/kali/workspace")
        project = type("ProjectStub", (), {"docker": "worker-container"})()

        command = agent._docker_command(project, DispatchMode.EXPLORE, runtime_key="intents/4")  # type: ignore[arg-type]

        self.assertIn("CODEX_HOME=/home/kali/.codex/intents/4", command)
        self.assertIn("TMUX_TMPDIR=/home/kali/.codex/intents/4/tmux", command)
        self.assertIn("/home/kali/workspace/intents/4", command)
        self.assertEqual(
            agent.intent_runtime_paths(4),
            ("/home/kali/.codex/intents/4", "/home/kali/workspace/intents/4"),
        )

    def test_preflight_runs_once_per_container(self) -> None:
        agent = CodexDockerAgent()
        project = type("ProjectStub", (), {"docker": "worker-container"})()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with patch("echo_agent.dispatcher.agents.subprocess.run", fake_run):
            agent._run_preflight(project, log=None)  # type: ignore[arg-type]
            agent._run_preflight(project, log=None)  # type: ignore[arg-type]

        self.assertEqual(len(calls), 1)

    def test_cancel_running_kills_registered_process_group(self) -> None:
        agent = CodexDockerAgent()
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        agent._register_process(process)

        self.assertTrue(agent.cancel_running())
        process.wait(timeout=2)

        self.assertIsNotNone(process.returncode)
        self.assertNotEqual(process.returncode, 0)

    def test_nonzero_process_does_not_salvage_prompt_template(self) -> None:
        import json

        template = json.dumps(
            {"answer": None, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"},
            ensure_ascii=False,
        )
        script = (
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({template!r})\n"
            "raise SystemExit(1)\n"
        )
        agent = CodexDockerAgent(timeout_seconds=5)

        with self.assertRaises(RuntimeError) as caught:
            agent._run_process(
                ("python3", "-c", script),
                prompt="hello",
                mode=DispatchMode.EXPLORE,
                env=None,
                log=None,
            )

        self.assertIn("exit code", str(caught.exception))

    def test_hard_timeout_keeps_real_fact_and_drops_prompt_template(self) -> None:
        script = """
import json, sys, time
sys.stdin.read()
print(json.dumps({"answer": None, "fact_description": "hidden ssrf endpoint", "outcome": "verified"}, ensure_ascii=False), flush=True)
print(json.dumps({"answer": None, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}, ensure_ascii=False), flush=True)
time.sleep(5)
"""
        agent = CodexDockerAgent(timeout_seconds=1, no_output_timeout_seconds=0)

        output = agent._run_process(
            ("python3", "-c", script),
            prompt="hello",
            mode=DispatchMode.EXPLORE,
            env=None,
            log=None,
        )

        self.assertIn("hidden ssrf endpoint", output)
        self.assertNotIn("该方向已验证不存在可利用点", output)

    def test_process_stdin_uses_utf8_encoding(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        recorded: dict[str, object] = {}

        def fake_popen(command, **kwargs):
            recorded.update(kwargs)
            raise RuntimeError("stop before consume")

        with patch("echo_agent.dispatcher.agents.subprocess.Popen", fake_popen):
            with self.assertRaisesRegex(RuntimeError, "stop before consume"):
                agent._run_process(
                    ("true",),
                    prompt="侦察目标",
                    mode=DispatchMode.EXPLORE,
                    env=None,
                    log=None,
                )

        self.assertEqual(recorded["encoding"], "utf-8")
        self.assertEqual(recorded["errors"], "replace")
        self.assertIs(recorded["text"], True)


if __name__ == "__main__":
    unittest.main()
