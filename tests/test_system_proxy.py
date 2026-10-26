from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.system_proxy import (
    _identity_is_proxy,
    _loopback_ports_in_proc,
    configured_proxy_environment,
    container_proxy_environment,
    container_proxy_signature,
    proxy_build_arguments,
    proxy_docker_arguments,
    proxy_environment_arguments,
    proxy_exec_arguments,
    proxy_signature,
    system_proxy_settings,
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

    def test_linux_manual_proxy_uses_gnome_settings(self) -> None:
        values = {
            ("org.gnome.system.proxy", "mode"): "manual",
            ("org.gnome.system.proxy.http", "host"): "127.0.0.1",
            ("org.gnome.system.proxy.http", "port"): "7892",
            ("org.gnome.system.proxy.https", "host"): "127.0.0.1",
            ("org.gnome.system.proxy.https", "port"): "7892",
            ("org.gnome.system.proxy.socks", "host"): "127.0.0.1",
            ("org.gnome.system.proxy.socks", "port"): "7892",
        }

        def gsettings(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            value = values.get((command[2], command[3]), "")
            return subprocess.CompletedProcess(command, 0, f"{value}\n", "")

        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", side_effect=gsettings),
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
        ):
            settings = configured_proxy_environment()

        self.assertEqual(settings["https_proxy"], "http://echo-agent-host:7892")

    def test_linux_automatic_proxy_is_ignored(self) -> None:
        completed = subprocess.CompletedProcess(["gsettings"], 0, "'auto'\n", "")
        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
            patch.dict("os.environ", {}, clear=True),
            patch("echo_agent.system_proxy._local_proxy_settings", return_value={}),
        ):
            self.assertEqual(system_proxy_settings(), {})

    def test_linux_loopback_listener_is_used_when_gnome_mode_is_none(self) -> None:
        completed = subprocess.CompletedProcess(["gsettings"], 0, "'none'\n", "")
        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
            patch.dict("os.environ", {}, clear=True),
            patch("echo_agent.system_proxy._loopback_listen_ports", return_value={7891, 7892}),
            patch("echo_agent.system_proxy._proxy_process_listen_ports", return_value=set()),
            patch("echo_agent.system_proxy._looks_like_http_proxy", side_effect=lambda port: port == 7892),
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.ensure_container_proxy_reachable") as forward,
        ):
            environment = configured_proxy_environment()

        self.assertEqual(environment["https_proxy"], "http://echo-agent-host:7892")
        self.assertNotIn("all_proxy", environment)
        forward.assert_called_once()

    def test_hidden_listener_still_uses_known_loopback_port(self) -> None:
        completed = subprocess.CompletedProcess(["gsettings"], 0, "'none'\n", "")
        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
            patch.dict("os.environ", {}, clear=True),
            patch("echo_agent.system_proxy._loopback_listen_ports", return_value=set()),
            patch("echo_agent.system_proxy._proxy_process_listen_ports", return_value=set()),
            patch("echo_agent.system_proxy._looks_like_http_proxy", side_effect=lambda port: port == 7892),
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.ensure_container_proxy_reachable") as forward,
        ):
            environment = configured_proxy_environment()

        self.assertEqual(environment["https_proxy"], "http://echo-agent-host:7892")
        forward.assert_called_once()

    def test_linux_loopback_socks_listener_is_used_when_http_probe_fails(self) -> None:
        completed = subprocess.CompletedProcess(["gsettings"], 0, "'none'\n", "")
        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
            patch.dict("os.environ", {}, clear=True),
            patch("echo_agent.system_proxy._loopback_listen_ports", return_value={7891}),
            patch("echo_agent.system_proxy._proxy_process_listen_ports", return_value={7891}),
            patch("echo_agent.system_proxy._looks_like_http_proxy", return_value=False),
            patch("echo_agent.system_proxy._looks_like_socks_proxy", side_effect=lambda port: port == 7891),
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.ensure_container_proxy_reachable"),
        ):
            environment = configured_proxy_environment()

        self.assertEqual(environment["all_proxy"], "socks5h://echo-agent-host:7891")
        self.assertNotIn("http_proxy", environment)

    def test_proc_tcp_keeps_only_loopback_listeners(self) -> None:
        text = "\n".join([
            "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode",
            "0: 0100007F:1ED4 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 12345 1 0000000000000000 100 0 0 10 0",
            "1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 12346 1 0000000000000000 100 0 0 10 0",
            "2: 0100007F:1ED5 00000000:0000 01 00000000:00000000 00:00000000 00000000 0 0 12347 1 0000000000000000 100 0 0 10 0",
        ])
        self.assertEqual(_loopback_ports_in_proc(text), {7892})

    def test_proxy_process_identity_ignores_unrelated_listeners(self) -> None:
        self.assertTrue(_identity_is_proxy("mihomo", "/usr/bin/mihomo -d /tmp"))
        self.assertFalse(_identity_is_proxy("sshd", "/usr/sbin/sshd"))

    def test_linux_environment_proxy_is_used_when_gnome_has_none(self) -> None:
        completed = subprocess.CompletedProcess(["gsettings"], 0, "'none'\n", "")
        with (
            patch.dict("sys.modules", {"_scproxy": None}),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
            patch.dict(
                "os.environ",
                {"https_proxy": "http://127.0.0.1:7890", "all_proxy": "socks5://127.0.0.1:7891"},
                clear=True,
            ),
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.ensure_container_proxy_reachable"),
        ):
            environment = configured_proxy_environment()
        self.assertEqual(environment["https_proxy"], "http://echo-agent-host:7890")
        self.assertNotIn("all_proxy", environment)

    def test_enabled_without_proxy_does_not_clear_container_environment(self) -> None:
        with (
            patch("echo_agent.system_proxy.system_proxy_enabled", return_value=True),
            patch("echo_agent.system_proxy.system_proxy_settings", return_value={}),
        ):
            self.assertEqual(proxy_exec_arguments(), [])

    def test_loopback_proxy_is_forwarded_on_the_docker_bridge(self) -> None:
        from echo_agent.system_proxy import _forwarded_ports, ensure_container_proxy_reachable

        binds: list[tuple[str, int]] = []

        class Listener:
            def setsockopt(self, *_args: object) -> None:
                return None

            def bind(self, address: tuple[str, int]) -> None:
                binds.append(address)

            def listen(self, _backlog: int) -> None:
                return None

        _forwarded_ports.clear()
        with (
            patch("echo_agent.system_proxy._docker_bridge_gateway", return_value="172.17.0.1"),
            patch("echo_agent.system_proxy._tcp_open", side_effect=lambda host, _port: host == "127.0.0.1"),
            patch("echo_agent.system_proxy.socket.socket", return_value=Listener()),
            patch("echo_agent.system_proxy.threading.Thread"),
        ):
            ensure_container_proxy_reachable({"https_proxy": "http://echo-agent-host:7890"})
        _forwarded_ports.clear()
        self.assertEqual(binds, [("172.17.0.1", 7890)])

    def test_proxy_signature_ignores_unrelated_container_environment(self) -> None:
        environment = container_proxy_environment(HTTP_SETTINGS)
        self.assertEqual(
            proxy_signature(environment)[1],
            ("echo-agent-host:host-gateway",),
        )
        inspected = "\n".join([
            '["PATH=/usr/bin", "https_proxy=http://echo-agent-host:7890", '
            '"HTTPS_PROXY=http://echo-agent-host:7890", "http_proxy=http://echo-agent-host:7890", '
            '"HTTP_PROXY=http://echo-agent-host:7890", '
            '"no_proxy=localhost,127.0.0.1,::1,host.docker.internal,gateway.docker.internal,echo-agent-host", '
            '"NO_PROXY=localhost,127.0.0.1,::1,host.docker.internal,gateway.docker.internal,echo-agent-host"]',
            '["host.docker.internal:127.0.0.1", "echo-agent-host:host-gateway"]',
        ]) + "\n"
        completed = subprocess.CompletedProcess(["docker"], 0, inspected, "")
        with (
            patch("echo_agent.system_proxy.container_exists", return_value=True),
            patch("echo_agent.system_proxy.subprocess.run", return_value=completed),
        ):
            self.assertEqual(container_proxy_signature("echo-agent-task-7"), proxy_signature(environment))

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
        self.assertIn("--entrypoint", run_command)
        entrypoint_at = run_command.index("--entrypoint")
        self.assertEqual(run_command[entrypoint_at:entrypoint_at + 4], [
            "--entrypoint", "/opt/echo-agent/entrypoint.sh", "worker:test", "sleep",
        ])

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

    def test_existing_container_is_recreated_when_proxy_changes(self) -> None:
        runtime = DockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        project = type("ProjectStub", (), {"docker": "echo-agent-task-7", "task_type": "general"})()
        commands: list[list[str]] = []

        def run(_runtime: DockerProjectRuntime, command: list[str], **_: object) -> None:
            commands.append(command)

        with (
            patch.object(DockerProjectRuntime, "_prepare_project_resources", return_value=[]),
            patch.object(DockerProjectRuntime, "_project_workspace_mounts", return_value=[]),
            patch.object(DockerProjectRuntime, "_extension_directory_mounts", return_value=[]),
            patch.object(DockerProjectRuntime, "_extension_item_mounts", return_value=[]),
            patch.object(DockerProjectRuntime, "_ensure_image"),
            patch.object(DockerProjectRuntime, "_container_state", return_value="running"),
            patch.object(DockerProjectRuntime, "_proxy_configuration_changed", return_value=True),
            patch.object(DockerProjectRuntime, "_run", run),
        ):
            runtime.ensure_project_container(project)  # type: ignore[arg-type]

        self.assertEqual(commands[0][:3], ["docker", "rm", "-f"])
        self.assertEqual(commands[1][:2], ["docker", "run"])

    def test_stopped_container_is_recreated_when_proxy_changes(self) -> None:
        runtime = DockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        project = type("ProjectStub", (), {"docker": "echo-agent-task-7", "task_type": "general"})()
        commands: list[list[str]] = []

        def run(_runtime: DockerProjectRuntime, command: list[str], **_: object) -> None:
            commands.append(command)

        with (
            patch.object(DockerProjectRuntime, "_container_state", return_value="exited"),
            patch.object(DockerProjectRuntime, "_proxy_configuration_changed", return_value=True),
            patch.object(DockerProjectRuntime, "ensure_project_container") as ensure,
            patch.object(DockerProjectRuntime, "_run", run),
        ):
            runtime.start_project_container(project)  # type: ignore[arg-type]

        self.assertEqual(commands[0][:3], ["docker", "rm", "-f"])
        ensure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
