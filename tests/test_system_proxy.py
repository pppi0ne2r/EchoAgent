from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.system_proxy import (
    configured_proxy_environment,
    container_proxy_environment,
    proxy_build_arguments,
    proxy_docker_arguments,
    proxy_environment_arguments,
    proxy_exec_arguments,
)


HTTP_SETTINGS = {
    "HTTPEnable": 1,
    "HTTPProxy": "127.0.0.1",
    "HTTPPort": 7890,
    "HTTPSEnable": 1,
    "HTTPSProxy": "127.0.0.1",
    "HTTPSPort": 7890,
    "SOCKSEnable": 1,
    "SOCKSProxy": "127.0.0.1",
    "SOCKSPort": 7890,
}


class SystemProxyTests(unittest.TestCase):
    def test_disabled_proxy_adds_nothing(self) -> None:
        self.assertEqual(container_proxy_environment({}), {})
        self.assertEqual(proxy_docker_arguments({}), [])
        self.assertEqual(proxy_build_arguments({}), [])
        self.assertEqual(proxy_environment_arguments({}), [])

    def test_setting_off_ignores_system_proxy(self) -> None:
        with (
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=False),
            patch("echo_agent.system_proxy.system_proxy_settings", return_value=HTTP_SETTINGS),
        ):
            self.assertEqual(configured_proxy_environment(), {})
            cleared = proxy_exec_arguments()
        self.assertIn("https_proxy=", cleared)
        self.assertNotIn("echo-agent-host:7890", cleared)

    def test_setting_on_reads_system_proxy(self) -> None:
        with (
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.system_proxy_settings", return_value=HTTP_SETTINGS),
        ):
            environment = configured_proxy_environment()
        self.assertEqual(environment["https_proxy"], "http://echo-agent-host:7890")

    def test_loopback_http_proxy_uses_host_gateway_name(self) -> None:
        environment = container_proxy_environment(HTTP_SETTINGS)

        self.assertEqual(environment["http_proxy"], "http://echo-agent-host:7890")
        self.assertEqual(environment["HTTPS_PROXY"], "http://echo-agent-host:7890")
        self.assertNotIn("all_proxy", environment)
        self.assertIn("echo-agent-host", environment["NO_PROXY"])
        self.assertIn("127.0.0.1", environment["no_proxy"])

    def test_socks_proxy_is_used_only_without_http_proxy(self) -> None:
        environment = container_proxy_environment({
            "SOCKSEnable": 1,
            "SOCKSProxy": "127.0.0.1",
            "SOCKSPort": 1080,
        })

        self.assertEqual(environment["ALL_PROXY"], "socks5h://echo-agent-host:1080")
        self.assertNotIn("http_proxy", environment)

    def test_non_loopback_proxy_keeps_its_address(self) -> None:
        environment = container_proxy_environment({
            "HTTPEnable": True,
            "HTTPProxy": "10.1.2.3",
            "HTTPPort": 8080,
        })

        self.assertEqual(environment["https_proxy"], "http://10.1.2.3:8080")
        arguments = proxy_docker_arguments(environment)
        self.assertEqual(arguments[:2], ["--add-host", "echo-agent-host:host-gateway"])
        self.assertIn("https_proxy=http://10.1.2.3:8080", arguments)


class SystemProxyCommandTests(unittest.TestCase):
    def test_container_run_and_build_include_proxy_when_enabled(self) -> None:
        runtime = DockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        environment = container_proxy_environment(HTTP_SETTINGS)
        with patch("echo_agent.server.docker_runtime.configured_proxy_environment", return_value=environment):
            run_command = runtime._docker_run_command("echo-agent-task-7", [])
            build_command = runtime._build_command()

        self.assertIn("host.docker.internal:127.0.0.1", run_command)
        self.assertIn("gateway.docker.internal:127.0.0.1", run_command)
        self.assertIn("echo-agent-host:host-gateway", run_command)
        self.assertIn("https_proxy=http://echo-agent-host:7890", run_command)
        self.assertIn("--build-arg", build_command)
        self.assertIn("https_proxy=http://echo-agent-host:7890", build_command)
        self.assertIn("echo-agent-host:host-gateway", build_command)

    def test_container_commands_stay_unchanged_without_proxy(self) -> None:
        runtime = DockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        with patch("echo_agent.server.docker_runtime.configured_proxy_environment", return_value={}):
            run_command = runtime._docker_run_command("echo-agent-task-7", [])
            build_command = runtime._build_command()

        self.assertNotIn("echo-agent-host:host-gateway", run_command)
        self.assertNotIn("https_proxy", " ".join(run_command))
        self.assertNotIn("--build-arg", build_command)

    def test_exec_receives_proxy_environment_without_add_host(self) -> None:
        agent = CodexDockerAgent(model="test-model", openai_api_key="test-key")
        project = type("ProjectStub", (), {"docker": "worker-container", "task_type": "general"})()
        with (
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.system_proxy_settings", return_value=HTTP_SETTINGS),
        ):
            command = agent._docker_command(project, DispatchMode.EXPLORE)  # type: ignore[arg-type]
            preflight = agent._preflight_command(project)  # type: ignore[arg-type]

        self.assertIn("https_proxy=http://echo-agent-host:7890", command)
        self.assertIn("HTTPS_PROXY=http://echo-agent-host:7890", command)
        self.assertNotIn("--add-host", command)
        self.assertIn("https_proxy=http://echo-agent-host:7890", preflight)
        self.assertNotIn("--add-host", preflight)


if __name__ == "__main__":
    unittest.main()
