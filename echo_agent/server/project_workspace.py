from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from echo_agent.models import Project
from echo_agent.server.attachments import CONTAINER_ATTACHMENT_DIR, write_attachment_file

CONTAINER_WORK_DIR = "/home/kali/workspace"


def project_directory_name(name: str, project_id: int) -> str:
    parts = []
    for part in name.replace("\\", "/").split("/"):
        cleaned = "".join(char for char in part if char.isprintable() and char not in "\x00:")
        cleaned = cleaned.strip(" .")
        if cleaned in {"", ".", ".."}:
            continue
        parts.append(cleaned)
    cleaned = "-".join(parts).strip(" .")
    if not cleaned:
        return f"project-{project_id}"
    return cleaned[:80]


@dataclass(frozen=True, slots=True)
class ProjectWorkspace:
    root: Path
    legacy_uploads: Path | None = None

    def bind_store(self, store: object) -> None:
        def after_log(project_id: int, record: dict[str, object]) -> None:
            self.append_log(store.get_project(project_id), record)  # type: ignore[attr-defined]

        def after_clear(project_id: int) -> None:
            directory = self.find(project_id)
            if directory is None:
                return
            path = directory / "logs" / "runtime.jsonl"
            if path.exists():
                path.write_text("", encoding="utf-8")

        store.after_save = self.write_record  # type: ignore[attr-defined]
        store.after_log = after_log  # type: ignore[attr-defined]
        store.after_clear_logs = after_clear  # type: ignore[attr-defined]

    def ensure_projects(self, projects: list[Project]) -> None:
        for project in projects:
            self.write_record(project)

    def backfill_logs(self, store: object, limit: int = 5000) -> None:
        for project in store.list_projects():  # type: ignore[attr-defined]
            directory = self.ensure(project)
            path = directory / "logs" / "runtime.jsonl"
            if path.exists() and path.stat().st_size > 0:
                continue
            records = store.list_project_logs(project.id, limit=limit)  # type: ignore[attr-defined]
            if not records:
                continue
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    line = {
                        "id": record.get("id"),
                        "created_at": record.get("created_at"),
                        "level": record.get("level"),
                        "message": record.get("message"),
                        "metadata": record.get("metadata") or {},
                    }
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def ensure(self, project: Project) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        current = self.find(project.id)
        desired = self.root / project_directory_name(project.name, project.id)
        if current is None:
            directory = desired if not desired.exists() else self.root / f"{desired.name}-{project.id}"
            directory.mkdir(parents=True, exist_ok=True)
        elif current != desired and self._owner_id(desired) in {None, project.id} and not desired.exists():
            current.rename(desired)
            directory = desired
        else:
            directory = current
        for name in ("attachments", "logs", "records", "work"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        if self._owner_id(directory) != project.id:
            marker = directory / "records" / "project.json"
            marker.write_text(json.dumps({"id": project.id}, ensure_ascii=False) + "\n", encoding="utf-8")
        self._import_legacy_uploads(project.id, directory / "attachments")
        return directory

    def find(self, project_id: int) -> Path | None:
        if not self.root.is_dir():
            return None
        for child in self.root.iterdir():
            if child.is_dir() and self._owner_id(child) == project_id:
                return child
        return None

    def write_record(self, project: Project) -> Path:
        directory = self.ensure(project)
        target = directory / "records" / "project.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        return directory


    def prune_attachments(self, project: Project) -> None:
        directory = self.find(project.id)
        if directory is None:
            return
        attachments = directory / "attachments"
        if not attachments.is_dir():
            return
        prefix = f"{CONTAINER_ATTACHMENT_DIR}/"
        kept = {
            line.strip()[len(prefix):]
            for line in (project.target_url or "").splitlines()
            if line.strip().startswith(prefix)
        }
        for child in attachments.iterdir():
            if child.is_file() and child.name not in kept:
                child.unlink()

    def save_upload(self, project: Project, filename: str, content: bytes) -> tuple[Path, str]:
        directory = self.ensure(project)
        host_path = write_attachment_file(directory / "attachments", filename, content)
        return host_path, f"{CONTAINER_ATTACHMENT_DIR}/{host_path.name}"

    def append_log(self, project: Project, record: dict[str, object]) -> None:
        directory = self.ensure(project)
        line = {
            "id": record.get("id"),
            "created_at": record.get("created_at"),
            "level": record.get("level"),
            "message": record.get("message"),
            "metadata": record.get("metadata") or {},
        }
        path = directory / "logs" / "runtime.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def clear_logs(self, project: Project) -> None:
        directory = self.find(project.id)
        if directory is None:
            return
        path = directory / "logs" / "runtime.jsonl"
        if path.exists():
            path.write_text("", encoding="utf-8")

    def remove(self, project_id: int) -> None:
        directory = self.find(project_id)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        if self.legacy_uploads is not None:
            shutil.rmtree(self.legacy_uploads / str(project_id), ignore_errors=True)

    def attachment_dir(self, project: Project) -> Path | None:
        directory = self.ensure(project) / "attachments"
        if not directory.is_dir() or not any(path.is_file() for path in directory.iterdir()):
            return None
        return directory.resolve()

    def work_dir(self, project: Project) -> Path:
        return (self.ensure(project) / "work").resolve()

    def _owner_id(self, directory: Path) -> int | None:
        marker = directory / "records" / "project.json"
        if not marker.is_file():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        project_id = data.get("id")
        if isinstance(project_id, int):
            return project_id
        return None

    def _import_legacy_uploads(self, project_id: int, attachments: Path) -> None:
        if self.legacy_uploads is None:
            return
        legacy = self.legacy_uploads / str(project_id)
        if not legacy.is_dir():
            return
        for child in list(legacy.iterdir()):
            if not child.is_file():
                continue
            target = attachments / child.name
            if target.exists():
                continue
            child.replace(target)
        try:
            legacy.rmdir()
        except OSError:
            return
