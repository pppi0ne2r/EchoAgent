from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

from echo_agent.config import ProjectResource
from echo_agent.models import Project
from echo_agent.server.docker_runtime import DockerProjectRuntime, DockerRuntimeError


class RecordingDockerProjectRuntime(DockerProjectRuntime):
    def __init__(self, *args, image_exists: bool, container_state: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_recorded_image_exists", image_exists)
        object.__setattr__(self, "_recorded_container_state", container_state)
        object.__setattr__(self, "commands", [])

    def _image_exists(self) -> bool:
        return self._recorded_image_exists

    def _container_state(self, container: str) -> str | None:
        return self._recorded_container_state

    def _run(self, command, log=None, output_prefix: str = "docker") -> None:
        self.commands.append(list(command))


class DockerProjectRuntimeTests(unittest.TestCase):
    def test_prepares_resource_directories_and_builds_volume_args(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=Path(directory) / "resources",
                resources=(
                    ProjectResource(
                        name="tools",
                        path=Path("tools"),
                        container_path="/home/kali/tools",
                        read_only=False,
                    ),
                    ProjectResource(
                        name="knowledges",
                        path=Path("knowledges"),
                        container_path="/home/kali/knowledges",
                        read_only=True,
                    ),
                ),
            )
            project = Project(id=7, name="task-7", docker="echo-agent-task-7")

            mounts = runtime._prepare_project_resources(project)
            command = runtime._docker_run_command("echo-agent-task-7", mounts)

            self.assertEqual(len(mounts), 2)
            self.assertTrue(mounts[0][0].is_absolute())
            self.assertTrue((Path(directory) / "resources" / "tools").is_dir())
            self.assertTrue((Path(directory) / "resources" / "knowledges").is_dir())
            self.assertEqual(mounts[0][1], "/home/kali/tools")
            self.assertFalse(mounts[0][2])
            self.assertIn("-v", command)
            self.assertIn(":/home/kali/tools:rw", " ".join(command))
            self.assertIn(":/home/kali/knowledges:ro", " ".join(command))
            self.assertEqual(command[-3:], ["worker:test", "sleep", "infinity"])
            self.assertIn("host.docker.internal:127.0.0.1", command)
            self.assertIn("gateway.docker.internal:127.0.0.1", command)

    def test_existing_image_skips_build_before_creating_container(self) -> None:
        runtime = RecordingDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
        )
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")

        runtime.ensure_project_container(project)

        self.assertEqual(len(runtime.commands), 1)
        self.assertEqual(runtime.commands[0][:4], ["docker", "run", "-d", "--name"])

    def test_missing_image_builds_before_creating_container(self) -> None:
        runtime = RecordingDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=False,
        )
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")

        runtime.ensure_project_container(project)

        self.assertEqual(len(runtime.commands), 2)
        self.assertEqual(runtime.commands[0][:2], ["docker", "build"])
        self.assertEqual(runtime.commands[1][:4], ["docker", "run", "-d", "--name"])

    def test_force_stop_uses_zero_timeout(self) -> None:
        runtime = RecordingDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
            container_state="running",
        )
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")

        runtime.force_stop_project_container(project)

        self.assertEqual(runtime.commands, [["docker", "stop", "-t", "0", "echo-agent-task-7"]])

    def test_prepare_shared_resources_downloads_and_extracts_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "resource.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("templates/example.yaml", "id: example\n")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                resources=(
                    ProjectResource(
                        name="templates",
                        path=Path("templates"),
                        container_path="/home/kali/templates",
                        url=archive_path.as_uri(),
                        archive=True,
                        read_only=False,
                    ),
                ),
            )

            runtime.prepare_shared_resources()

            self.assertTrue((root / "resources" / "templates" / "example.yaml").is_file())

    def test_prepare_shared_resources_strips_single_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "resource.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("repo-main/example.yaml", "id: example\n")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                resources=(
                    ProjectResource(
                        name="templates",
                        path=Path("templates"),
                        container_path="/home/kali/templates",
                        url=archive_path.as_uri(),
                        archive=True,
                        read_only=False,
                    ),
                ),
            )

            runtime.prepare_shared_resources()

            self.assertTrue((root / "resources" / "templates" / "example.yaml").is_file())

    def test_prepare_shared_resources_extracts_single_executable_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "tool.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("tool", "#!/bin/sh\n")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                resources=(
                    ProjectResource(
                        name="tool",
                        path=Path("local-bin/tool"),
                        container_path="/home/kali/.local/bin/tool",
                        url=archive_path.as_uri(),
                        archive=True,
                        archive_single_file=True,
                        executable=True,
                        read_only=True,
                    ),
                ),
            )

            runtime.prepare_shared_resources()

            tool_path = root / "resources" / "local-bin" / "tool"
            self.assertTrue(tool_path.is_file())
            self.assertTrue(tool_path.stat().st_mode & 0o111)



    def test_missing_container_message_from_docker29_stdout(self) -> None:
        runtime = DockerProjectRuntime(
            image="echo-agent-worker",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        missing = subprocess.CompletedProcess(
            ["docker", "inspect"],
            1,
            stdout='Error: container "echo-agent" does not exist\n',
            stderr="",
        )
        lowercase = subprocess.CompletedProcess(
            ["docker", "inspect"],
            1,
            stdout="",
            stderr="error: no such container: echo-agent\n",
        )
        unknown = subprocess.CompletedProcess(
            ["docker", "inspect"],
            1,
            stdout="",
            stderr="permission denied\n",
        )
        with patch("echo_agent.server.docker_runtime.subprocess.run", return_value=missing):
            self.assertIsNone(runtime._container_state("echo-agent"))
        with patch("echo_agent.server.docker_runtime.subprocess.run", return_value=lowercase):
            self.assertIsNone(runtime._container_state("echo-agent"))
        with patch("echo_agent.server.docker_runtime.subprocess.run", return_value=unknown):
            with self.assertRaises(DockerRuntimeError):
                runtime._container_state("echo-agent")


    def test_attachment_directory_is_mounted_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attachment = root / "7"
            attachment.mkdir()
            (attachment / "challenge.bin").write_bytes(b"bin")
            runtime = RecordingDockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                image_exists=True,
                attachment_root=root,
            )
            project = Project(id=7, name="rev", docker="echo-agent-task-7")

            runtime.ensure_project_container(project)

            command = " ".join(runtime.commands[-1])
            self.assertIn(f"{attachment.resolve()}:/home/kali/attachments:ro", command)





class StatefulDockerProjectRuntime(RecordingDockerProjectRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_states", {})

    def _container_state(self, container: str) -> str | None:
        if container in self._states:
            return self._states[container]
        return self._recorded_container_state

    def _run(self, command, log=None, output_prefix: str = "docker") -> None:
        super()._run(command, log=log, output_prefix=output_prefix)
        if command[:2] == ["docker", "rm"]:
            self._states[command[-1]] = None
        elif command[:3] == ["docker", "run", "-d"]:
            self._states[command[command.index("--name") + 1]] = "running"
        elif command[:2] == ["docker", "stop"]:
            self._states[command[-1]] = "exited"


class ContainerRestartTests(unittest.TestCase):
    def test_recreate_skips_container_that_was_never_created(self) -> None:
        runtime = StatefulDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
            container_state=None,
        )
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")

        self.assertFalse(runtime.recreate_existing_container(project))
        self.assertEqual(runtime.commands, [])

    def test_recreate_running_container_and_stop_previously_stopped_container(self) -> None:
        runtime = StatefulDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
            container_state="running",
        )
        running = Project(id=7, name="task-7", docker="echo-agent-task-7")
        self.assertTrue(runtime.recreate_existing_container(running))
        self.assertEqual(runtime.commands[0][:3], ["docker", "rm", "-f"])
        self.assertEqual(runtime.commands[1][:4], ["docker", "run", "-d", "--name"])
        self.assertEqual(len(runtime.commands), 2)

        stopped = StatefulDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
            container_state="exited",
        )
        project = Project(id=8, name="task-8", docker="echo-agent-task-8")
        self.assertTrue(stopped.recreate_existing_container(project))
        self.assertEqual(stopped.commands[0][:3], ["docker", "rm", "-f"])
        self.assertEqual(stopped.commands[1][:4], ["docker", "run", "-d", "--name"])
        self.assertEqual(stopped.commands[2], ["docker", "stop", "echo-agent-task-8"])
        self.assertEqual(stopped._container_state("echo-agent-task-8"), "exited")


class ClashContainerTests(unittest.TestCase):
    def _runtime(self, state: str | None) -> StatefulDockerProjectRuntime:
        runtime = StatefulDockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            image_exists=True,
            container_state=state,
        )

        def container_env(container: str) -> list[str]:
            return ["PATH=/usr/bin"]

        object.__setattr__(runtime, "_container_env", container_env)
        return runtime

    def test_stopped_container_is_recreated_when_clash_settings_change(self) -> None:
        runtime = self._runtime("exited")
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")
        desired = {
            "ECHO_CLASH_ENABLED": "1",
            "ECHO_CLASH_SUBSCRIPTION_URL": "https://example.com/sub",
            "http_proxy": "http://127.0.0.1:7890",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "all_proxy": "socks5h://127.0.0.1:7890",
            "ALL_PROXY": "socks5h://127.0.0.1:7890",
            "no_proxy": "localhost,127.0.0.1,::1",
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
        with patch("echo_agent.clash.desired_clash_environment", return_value=desired):
            runtime.start_project_container(project)
        self.assertEqual(runtime.commands[0][:3], ["docker", "rm", "-f"])
        run = runtime.commands[1]
        self.assertEqual(run[:4], ["docker", "run", "-d", "--name"])
        self.assertIn("ECHO_CLASH_ENABLED=1", run)
        self.assertIn("http_proxy=http://127.0.0.1:7890", run)
        self.assertEqual(len(runtime.commands), 2)

    def test_running_container_is_not_recreated_for_clash_settings(self) -> None:
        runtime = self._runtime("running")
        project = Project(id=7, name="task-7", docker="echo-agent-task-7")
        with patch(
            "echo_agent.clash.desired_clash_environment",
            return_value={"ECHO_CLASH_ENABLED": "1", "ECHO_CLASH_SUBSCRIPTION_URL": "https://example.com/sub"},
        ):
            runtime.start_project_container(project)
        self.assertEqual(runtime.commands, [])


if __name__ == "__main__":
    unittest.main()
