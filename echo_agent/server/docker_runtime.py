from __future__ import annotations

import subprocess
import tarfile
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from echo_agent.config import DispatcherSettings, ProjectResource
from echo_agent.config import ServerSettings
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_budget import CRYPTO_PIDS_LIMIT, DEFAULT_MEMORY_MB
from echo_agent.models import Project, ProjectStatus
from echo_agent.server.attachments import CONTAINER_ATTACHMENT_DIR
from echo_agent.server.project_workspace import CONTAINER_WORK_DIR, ProjectWorkspace


class DockerRuntimeError(RuntimeError):
    pass


DockerLogCallback = Callable[[str, str, dict[str, object] | None], None]


@dataclass(frozen=True, slots=True)
class DockerProjectRuntime:
    image: str
    context: Path
    dockerfile: Path
    build_network: str | None = "host"
    resource_root: Path = Path(".echo_agent/resources")
    project_data_root: Path = Path(".echo_agent/projects")
    resources: Sequence[ProjectResource] = ()
    keepalive_command: Sequence[str] = ("sleep", "infinity")
    attachment_root: Path | None = None
    workspace: ProjectWorkspace | None = None

    @classmethod
    def from_settings(
        cls,
        settings: DispatcherSettings | None = None,
        project_data_root: Path | None = None,
    ) -> "DockerProjectRuntime":
        settings = settings or DispatcherSettings.from_env()
        return cls(
            image=settings.project_docker_image,
            context=settings.project_docker_context,
            dockerfile=settings.project_dockerfile,
            build_network=settings.project_docker_build_network,
            resource_root=settings.project_resource_root,
            project_data_root=project_data_root or ServerSettings.from_env().database_path.parent / "projects",
            resources=settings.project_resources,
        )

    def apply_status_transition(
        self,
        project: Project,
        previous_status: ProjectStatus | None,
        log: DockerLogCallback | None = None,
    ) -> None:
        if previous_status == project.status:
            return
        if project.status == ProjectStatus.ACTIVE:
            self.ensure_project_container(project, log=log)
            return
        if project.status == ProjectStatus.RUNNING:
            self.start_project_container(project, log=log)
            return
        if project.status == ProjectStatus.STOPED:
            self.stop_project_container(project, log=log)
            return
        if project.status == ProjectStatus.COMPLETED:
            self.remove_project_container(project, log=log)

    def ensure_project_container(self, project: Project, log: DockerLogCallback | None = None) -> None:
        container = self._container_name(project)
        mounts = list(self._prepare_project_resources(project, log=log))
        mounts.extend(self._project_workspace_mounts(project))
        if self.workspace is None:
            attachment = self._attachment_mount(project)
            if attachment is not None:
                mounts.append(attachment)
        self._ensure_image(log=log)

        self._log(log, "docker container inspect started", docker=container)
        state = self._container_state(container)
        self._log(log, "docker container inspect finished", docker=container, state=state or "missing")
        if state is None:
            self._log(log, "docker container create/start started", docker=container, image=self.image)
            self._run(
                self._docker_run_command(container, mounts, crypto=project.task_type == "crypto"),
                log=log,
                output_prefix="docker run",
            )
            self._log(log, "docker container create/start finished", docker=container, image=self.image)
            return
        if state != "running":
            self._log(log, "docker container start started", docker=container, previous_state=state)
            self._run(["docker", "start", container], log=log, output_prefix="docker start")
            self._log(log, "docker container start finished", docker=container)
            return
        self._log(log, "docker container already running", docker=container)

    def prepare_shared_resources(self, log: DockerLogCallback | None = None) -> None:
        if not self.resources:
            self._log(log, "shared resource preparation skipped; no resources configured")
            return
        root_template = str(self.resource_root)
        if "{project_id}" in root_template or "{project_name}" in root_template:
            self._log(
                log,
                "shared resource preparation skipped; resource root is project templated",
                root=root_template,
            )
            return
        self._prepare_resources_at_root(Path(root_template), log=log)

    def _docker_run_command(self, container: str, mounts: Sequence[tuple[Path, str, bool]],
                            *, crypto: bool = False) -> list[str]:
        command = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--add-host",
                    "host.docker.internal:127.0.0.1",
                    "--add-host",
                    "gateway.docker.internal:127.0.0.1",
        ]
        if crypto:
            command.extend(["--init", "--memory", f"{DEFAULT_MEMORY_MB}m",
                            "--memory-swap", f"{DEFAULT_MEMORY_MB}m",
                            "--pids-limit", str(CRYPTO_PIDS_LIMIT), "--cpus", "2"])
        for host_path, container_path, read_only in mounts:
            mode = "ro" if read_only else "rw"
            command.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        command.extend([self.image, *self.keepalive_command])
        return command

    def start_project_container(self, project: Project, log: DockerLogCallback | None = None) -> None:
        container = self._container_name(project)
        state = self._container_state(container)
        if state is None:
            self._log(log, "docker container missing before run; creating it", docker=container)
            self.ensure_project_container(project, log=log)
            return
        if state != "running":
            self._log(log, "docker container start started", docker=container, previous_state=state)
            self._run(["docker", "start", container], log=log, output_prefix="docker start")
            self._log(log, "docker container start finished", docker=container)
            return
        self._log(log, "docker container already running", docker=container)

    def stop_project_container(self, project: Project, log: DockerLogCallback | None = None) -> None:
        container = self._container_name(project)
        if self._container_state(container) == "running":
            self._log(log, "docker container stop started", docker=container)
            self._run(["docker", "stop", container], log=log, output_prefix="docker stop")
            self._log(log, "docker container stop finished", docker=container)
            return
        self._log(log, "docker container is not running", docker=container)

    def force_stop_project_container(self, project: Project, log: DockerLogCallback | None = None) -> None:
        container = self._container_name(project)
        if self._container_state(container) == "running":
            self._log(log, "docker container force stop started", docker=container)
            self._run(["docker", "stop", "-t", "0", container], log=log, output_prefix="docker force stop")
            self._log(log, "docker container force stop finished", docker=container)
            return
        self._log(log, "docker container is not running", docker=container)

    def remove_project_container(self, project: Project, log: DockerLogCallback | None = None) -> None:
        container = self._container_name(project)
        if self._container_state(container) is not None:
            self._log(log, "docker container remove started", docker=container)
            self._run(["docker", "rm", "-f", container], log=log, output_prefix="docker rm")
            self._log(log, "docker container remove finished", docker=container)
            return
        self._log(log, "docker container already removed", docker=container)

    def _container_state(self, container: str) -> str | None:
        completed = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            if self._docker_object_missing(completed.stdout, completed.stderr):
                return None
            detail = "\n".join(
                part.strip() for part in (completed.stderr, completed.stdout) if part and part.strip()
            )
            raise DockerRuntimeError(detail or f"docker inspect failed for {container}")
        return completed.stdout.strip()

    @staticmethod
    def _docker_object_missing(stdout: str, stderr: str) -> bool:
        text = f"{stdout}\n{stderr}".lower()
        return any(
            marker in text
            for marker in ("no such container", "no such object", "does not exist")
        )

    def _ensure_image(self, log: DockerLogCallback | None = None) -> None:
        self._log(log, "docker image inspect started", image=self.image)
        if self._image_exists():
            self._log(log, "docker image inspect finished", image=self.image, state="exists")
            self._log(log, "docker image build skipped; image already exists", image=self.image)
            return
        self._log(log, "docker image inspect finished", image=self.image, state="missing")
        build_command = self._build_command()
        self._log(log, "docker image build started", command=build_command, image=self.image)
        self._run(build_command, log=log, output_prefix="docker build")
        self._log(log, "docker image build finished", image=self.image)

    def _image_exists(self) -> bool:
        completed = subprocess.run(
            ["docker", "image", "inspect", self.image],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return True
        stderr = completed.stderr.strip()
        if "No such image" in stderr or "No such object" in stderr:
            return False
        raise DockerRuntimeError(stderr or f"docker image inspect failed for {self.image}")

    def _run(
        self,
        command: Sequence[str],
        log: DockerLogCallback | None = None,
        output_prefix: str = "docker",
    ) -> None:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        recent_output: deque[str] = deque(maxlen=20)
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            recent_output.append(line)
            self._log(log, f"{output_prefix}: {self._truncate(line)}", metadata={"command": list(command)})

        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            detail = "\n".join(recent_output) or f"exit code {return_code}"
            raise DockerRuntimeError(f"{' '.join(command)} failed: {detail}")

    def _build_command(self) -> list[str]:
        command = ["docker", "build"]
        if self.build_network:
            command.extend(["--network", self.build_network])
        command.extend(["-t", self.image, "-f", str(self.dockerfile), str(self.context)])
        return command

    def _project_workspace_mounts(self, project: Project) -> list[tuple[Path, str, bool]]:
        if self.workspace is None:
            return []
        mounts: list[tuple[Path, str, bool]] = [(self.workspace.work_dir(project), CONTAINER_WORK_DIR, False)]
        attachments = self.workspace.attachment_dir(project)
        if attachments is not None:
            mounts.append((attachments, CONTAINER_ATTACHMENT_DIR, True))
        return mounts

    def _attachment_mount(self, project: Project) -> tuple[Path, str, bool] | None:
        if self.attachment_root is None:
            return None
        directory = (self.attachment_root / str(project.id)).resolve()
        if not directory.is_dir() or not any(path.is_file() for path in directory.iterdir()):
            return None
        return (directory, CONTAINER_ATTACHMENT_DIR, True)

    def _prepare_project_resources(
        self,
        project: Project,
        log: DockerLogCallback | None = None,
    ) -> list[tuple[Path, str, bool]]:
        mounts: list[tuple[Path, str, bool]] = []
        if self.resources:
            root = self._project_resource_root(project)
            mounts.extend(self._prepare_resources_at_root(root, log=log))
        if project.task_type == "crypto":
            try:
                files = CryptoProjectFiles(self.project_data_root)
                inputs, intents = files.ensure_layout(project.id)
                for ref in project.crypto.get("inputs", []):
                    files.verify_ref(project.id, ref, "input")
                destinations = {container_path for _, container_path, _ in mounts}
                crypto_destinations = {
                    "/home/kali/workspace/inputs",
                    "/home/kali/workspace/intents",
                }
                if any(
                    destination == crypto_path
                    or destination.startswith(f"{crypto_path}/")
                    or crypto_path.startswith(f"{destination.rstrip('/')}/")
                    for destination in destinations
                    for crypto_path in crypto_destinations
                ):
                    raise ValueError("crypto workspace mount conflicts with a configured resource")
            except (ValueError, FileNotFoundError) as exc:
                raise DockerRuntimeError(str(exc)) from exc
            mounts.extend([
                (inputs, "/home/kali/workspace/inputs", True),
                (intents, "/home/kali/workspace/intents", False),
            ])
            self._log(log, "crypto project files prepared", metadata={"project_id": project.id})
        return mounts

    def _prepare_resources_at_root(
        self,
        root: Path,
        log: DockerLogCallback | None = None,
    ) -> list[tuple[Path, str, bool]]:
        root.mkdir(parents=True, exist_ok=True)
        mounts: list[tuple[Path, str, bool]] = []
        for resource in self.resources:
            host_path = self._resource_host_path(root, resource)
            if resource.url:
                self._download_resource(resource, host_path, log=log)
            else:
                host_path.mkdir(parents=True, exist_ok=True)
                self._log(
                    log,
                    "shared resource directory prepared",
                    name=resource.name,
                    host_path=str(host_path),
                    container_path=resource.container_path,
                )
            mounts.append((host_path.resolve(), resource.container_path, resource.read_only))
        self._log(log, "shared resources prepared", count=len(mounts), root=str(root))
        return mounts

    def _project_resource_root(self, project: Project) -> Path:
        return Path(str(self.resource_root).format(project_id=project.id, project_name=project.name))

    @staticmethod
    def _resource_host_path(root: Path, resource: ProjectResource) -> Path:
        path = Path(str(resource.path))
        if path.is_absolute():
            return path
        return root / path

    def _download_resource(
        self,
        resource: ProjectResource,
        host_path: Path,
        log: DockerLogCallback | None = None,
    ) -> None:
        if self._resource_exists(host_path):
            self._log(
                log,
                "shared resource already exists",
                name=resource.name,
                host_path=str(host_path),
                container_path=resource.container_path,
            )
            return
        host_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = host_path.with_name(f"{host_path.name}.download")
        self._log(
            log,
            "shared resource download started",
            name=resource.name,
            url=resource.url,
            host_path=str(host_path),
            archive=resource.archive,
        )
        try:
            urllib.request.urlretrieve(str(resource.url), tmp_path)
            if resource.archive:
                self._extract_archive(tmp_path, host_path, single_file=resource.archive_single_file)
                tmp_path.unlink()
            else:
                tmp_path.replace(host_path)
            if resource.executable:
                self._chmod_executable(host_path)
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            raise DockerRuntimeError(f"download resource {resource.name!r} failed: {exc}") from exc
        self._log(
            log,
            "shared resource download finished",
            name=resource.name,
            host_path=str(host_path),
            container_path=resource.container_path,
        )

    @staticmethod
    def _resource_exists(host_path: Path) -> bool:
        if not host_path.exists():
            return False
        if host_path.is_dir():
            return any(host_path.iterdir())
        return True

    def _extract_archive(self, archive_path: Path, host_path: Path, single_file: bool = False) -> None:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                names = [name for name in archive.namelist() if name and not name.endswith("/")]
                if single_file:
                    self._extract_single_zip_file(archive, names, host_path)
                    return
                host_path.mkdir(parents=True, exist_ok=True)
                prefix = self._single_archive_prefix(names)
                for name in names:
                    target_name = name[len(prefix):] if prefix and name.startswith(prefix) else name
                    if not target_name:
                        continue
                    target_path = self._safe_archive_target(host_path, target_name)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target_path.open("wb") as target:
                        target.write(source.read())
            return
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as archive:
                members = [member for member in archive.getmembers() if member.isfile()]
                if single_file:
                    self._extract_single_tar_file(archive, members, host_path)
                    return
                host_path.mkdir(parents=True, exist_ok=True)
                prefix = self._single_archive_prefix([member.name for member in members])
                for member in members:
                    target_name = member.name[len(prefix):] if prefix and member.name.startswith(prefix) else member.name
                    if not target_name:
                        continue
                    target_path = self._safe_archive_target(host_path, target_name)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        continue
                    with source, target_path.open("wb") as target:
                        target.write(source.read())
            return
        raise DockerRuntimeError(f"unsupported resource archive: {archive_path}")

    @staticmethod
    def _extract_single_zip_file(archive: zipfile.ZipFile, names: Sequence[str], host_path: Path) -> None:
        if not names:
            raise DockerRuntimeError("archive does not contain any files")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(names[0]) as source, host_path.open("wb") as target:
            target.write(source.read())

    @staticmethod
    def _extract_single_tar_file(archive: tarfile.TarFile, members: Sequence[tarfile.TarInfo], host_path: Path) -> None:
        if not members:
            raise DockerRuntimeError("archive does not contain any files")
        source = archive.extractfile(members[0])
        if source is None:
            raise DockerRuntimeError("archive member cannot be extracted")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        with source, host_path.open("wb") as target:
            target.write(source.read())

    @staticmethod
    def _chmod_executable(host_path: Path) -> None:
        if host_path.is_dir():
            for path in host_path.rglob("*"):
                if path.is_file():
                    path.chmod(path.stat().st_mode | 0o755)
            return
        host_path.chmod(host_path.stat().st_mode | 0o755)

    @staticmethod
    def _safe_archive_target(host_path: Path, target_name: str) -> Path:
        target_path = host_path / target_name
        resolved_root = host_path.resolve()
        resolved_target = target_path.resolve()
        if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
            raise DockerRuntimeError(f"unsafe resource archive path: {target_name}")
        return target_path

    @staticmethod
    def _single_archive_prefix(names: Sequence[str]) -> str:
        roots = {name.split("/", 1)[0] for name in names if "/" in name}
        if len(roots) != 1:
            return ""
        root = next(iter(roots))
        if all(name == root or name.startswith(f"{root}/") for name in names):
            return f"{root}/"
        return ""

    @staticmethod
    def _log(
        log: DockerLogCallback | None,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
        **extra_metadata: object,
    ) -> None:
        if log is not None:
            payload = dict(metadata or {})
            payload.update(extra_metadata)
            log(message, level, payload or None)

    @staticmethod
    def _truncate(value: str, limit: int = 1000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit - 3]}..."

    @staticmethod
    def _container_name(project: Project) -> str:
        if not project.docker:
            raise DockerRuntimeError(f"project {project.id} does not have a docker container name")
        return project.docker
