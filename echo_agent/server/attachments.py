from __future__ import annotations

from pathlib import Path

MAX_ATTACHMENT_BYTES = 128 * 1024 * 1024
CONTAINER_ATTACHMENT_DIR = "/home/kali/attachments"


def safe_attachment_name(filename: str) -> str:
    base = filename.replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    base = "".join(char for char in base if char.isprintable() and char not in "\\/")
    base = base.strip()
    if not base or base in {".", ".."} or "/" in base or "\\" in base:
        raise ValueError("附件文件名无效")
    return base[:180]


def write_attachment_file(directory: Path, filename: str, content: bytes) -> Path:
    if not content:
        raise ValueError("附件是空文件")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("附件不能超过 128MB")
    name = safe_attachment_name(filename)
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()
    host_path = directory / name
    if host_path.resolve().parent != directory.resolve():
        raise ValueError("附件文件名无效")
    host_path.write_bytes(content)
    return host_path


def save_attachment(root: Path, project_id: int, filename: str, content: bytes) -> tuple[Path, str]:
    if project_id < 0:
        raise ValueError("project id must be non-negative")
    host_path = write_attachment_file(root / str(project_id), filename, content)
    return host_path, f"{CONTAINER_ATTACHMENT_DIR}/{host_path.name}"
