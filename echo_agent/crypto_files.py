"""Project-local, persistent file paths for cryptography tasks."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .crypto_contract import artifact_ref, input_ref, relative_path
from .crypto_verification import check_verification


MAX_INPUT_BYTES = 32 * 1024 * 1024
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_input_filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 120
        or value in {".", ".."}
        or value[-1] in {".", " "}
        or any(char in value for char in '<>:"/\\|?*')
        or any(ord(char) < 32 for char in value)
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise ValueError("invalid input filename")
    return value


@dataclass(frozen=True, slots=True)
class CryptoProjectFiles:
    root: Path

    def project_root(self, project_id: int) -> Path:
        if type(project_id) is not int or project_id < 0:
            raise ValueError("project id must be a non-negative integer")
        root = self.root.resolve()
        project = root / str(project_id)
        if not project.resolve().is_relative_to(root):
            raise ValueError("crypto project directory escapes its root")
        return project

    def ensure_layout(self, project_id: int) -> tuple[Path, Path]:
        project = self.project_root(project_id)
        inputs = project / "inputs"
        intents = project / "artifacts" / "intents"
        for directory in (inputs, intents):
            if not directory.resolve().is_relative_to(project.resolve()):
                raise ValueError("crypto project directory escapes its root")
            directory.mkdir(parents=True, exist_ok=True)
        return inputs.resolve(), intents.resolve()

    def clear_artifacts(self, project_id: int) -> None:
        project = self.project_root(project_id)
        artifacts = project / "artifacts"
        if not artifacts.exists():
            return
        resolved = artifacts.resolve()
        if not resolved.is_relative_to(project.resolve()):
            raise ValueError("crypto project directory escapes its root")
        shutil.rmtree(resolved)

    def resolve_ref(self, project_id: int, ref: dict[str, Any], kind: str) -> Path:
        if kind == "input":
            data = input_ref(ref)
        elif kind == "artifact":
            data = artifact_ref(ref)
        else:
            raise ValueError("kind must be input or artifact")
        project = self.project_root(project_id).resolve()
        path = project.joinpath(*PurePosixPath(data["path"]).parts).resolve()
        if not path.is_relative_to(project):
            raise ValueError("crypto file reference escapes its project")
        return path

    def verify_ref(self, project_id: int, ref: dict[str, Any], kind: str) -> Path:
        path = self.resolve_ref(project_id, ref, kind)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != ref["size_bytes"]:
            raise ValueError("crypto file size or sha256 does not match its reference")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                check_verification()
                size += len(chunk)
                digest.update(chunk)
        if size != ref["size_bytes"] or digest.hexdigest() != ref["sha256"]:
            raise ValueError("crypto file size or sha256 does not match its reference")
        return path

    def read_verified(self, project_id: int, ref: dict[str, Any], kind: str, *, max_bytes: int) -> bytes:
        """Read and hash the same bounded byte stream used for verification."""
        path = self.resolve_ref(project_id, ref, kind)
        if ref["size_bytes"] > max_bytes or path.stat().st_size > max_bytes:
            raise ValueError("verification input exceeds cumulative 1 MiB limit")
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("verification input exceeds cumulative 1 MiB limit")
        if len(raw) != ref["size_bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("crypto file size or sha256 does not match its reference")
        return raw

    def describe_input(self, project_id: int, input_id: str, role: str, path: str) -> dict[str, Any]:
        relative_path(path, "inputs")
        file_path = self._existing_file(project_id, path)
        return input_ref({
            "id": input_id,
            "role": role,
            "path": path,
            "sha256": self._sha256(file_path),
            "size_bytes": file_path.stat().st_size,
        })

    def describe_artifact(self, project_id: int, intent_id: int, path: str) -> dict[str, Any]:
        relative_path(path, "artifacts")
        file_path = self._existing_file(project_id, path)
        return artifact_ref({
            "path": path,
            "sha256": self._sha256(file_path),
            "size_bytes": file_path.stat().st_size,
            "producer_intent_id": intent_id,
        })

    def _existing_file(self, project_id: int, path: str) -> Path:
        project = self.project_root(project_id).resolve()
        file_path = project.joinpath(*PurePosixPath(path).parts).resolve()
        if not file_path.is_relative_to(project):
            raise ValueError("crypto file reference escapes its project")
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        return file_path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
