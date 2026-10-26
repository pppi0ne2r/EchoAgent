from __future__ import annotations

import fnmatch
import io
import json
import re
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from echo_agent.config import ProjectResource


EXTENSION_KINDS = ("knowledge", "tool", "skill")
MAX_IMPORT_BYTES = 128 * 1024 * 1024
_YSOSERIAL_URL = "https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar"
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")


@dataclass(slots=True)
class ExtensionItem:
    id: str
    kind: str
    name: str
    url: str
    error: str = ""
    container_path: str = ""
    archive_single_file: bool = False
    executable: bool = False
    read_only: bool = True
    github_repo: str = ""
    asset_pattern: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "url": self.url,
            "error": self.error,
        }
        if self.container_path:
            payload["container_path"] = self.container_path
        if self.archive_single_file:
            payload["archive_single_file"] = True
        if self.executable:
            payload["executable"] = True
        if not self.read_only:
            payload["read_only"] = False
        if self.github_repo:
            payload["github_repo"] = self.github_repo
        if self.asset_pattern:
            payload["asset_pattern"] = self.asset_pattern
        return payload


def builtin_extensions() -> list[ExtensionItem]:
    return [
        ExtensionItem(
            id="tool-ysoserial",
            kind="tool",
            name="ysoserial",
            url=_YSOSERIAL_URL,
            github_repo="frohoff/ysoserial",
            asset_pattern="ysoserial-all.jar",
        ),
        ExtensionItem(
            id="tool-katana",
            kind="tool",
            name="katana",
            url="https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_arm64.zip",
            container_path="/home/kali/.local/bin/katana",
            archive_single_file=True,
            executable=True,
            github_repo="projectdiscovery/katana",
            asset_pattern="katana_*_linux_arm64.zip",
        ),
        ExtensionItem(
            id="tool-nuclei",
            kind="tool",
            name="nuclei",
            url="https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_arm64.zip",
            container_path="/home/kali/.local/bin/nuclei",
            archive_single_file=True,
            executable=True,
            github_repo="projectdiscovery/nuclei",
            asset_pattern="nuclei_*_linux_arm64.zip",
        ),
        ExtensionItem(
            id="tool-dalfox",
            kind="tool",
            name="dalfox",
            url="https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-arm64.tar.gz",
            container_path="/home/kali/.local/bin/dalfox",
            archive_single_file=True,
            executable=True,
            github_repo="hahwul/dalfox",
            asset_pattern="dalfox*linux-aarch64.tar.gz",
        ),
        ExtensionItem(
            id="tool-cloudfox",
            kind="tool",
            name="cloudfox",
            url="https://github.com/BishopFox/cloudfox/releases/download/v2.0.1/cloudfox-linux-arm64.zip",
            container_path="/home/kali/.local/bin/cloudfox",
            archive_single_file=True,
            executable=True,
            github_repo="BishopFox/cloudfox",
            asset_pattern="cloudfox*linux-arm64.zip",
        ),
        ExtensionItem(
            id="knowledge-nuclei-templates",
            kind="knowledge",
            name="nuclei-templates",
            url="https://github.com/projectdiscovery/nuclei-templates/archive/refs/heads/main.zip",
            container_path="/home/kali/.local/nuclei-templates",
            read_only=False,
        ),
        ExtensionItem(
            id="skill-ctf-skills",
            kind="skill",
            name="ctf-skills",
            url="https://github.com/ljagiello/ctf-skills",
            github_repo="ljagiello/ctf-skills",
        ),
    ]


def extension_resource(item: ExtensionItem) -> ProjectResource:
    if item.container_path:
        return ProjectResource(
            name=item.name,
            path=_host_relative(item.container_path),
            container_path=item.container_path,
            url=item.url,
            archive=_is_archive(item.url),
            archive_single_file=item.archive_single_file,
            executable=item.executable,
            read_only=item.read_only,
        )
    filename = _named_download(item.url, item.name)
    archive = _is_archive(item.url)
    slug = _slug(item.name)
    if item.kind == "tool":
        relative = Path("tools") / (slug if archive else filename)
        container = f"/home/kali/tools/{relative.name}"
        executable = item.executable or archive or not filename.endswith((".jar", ".txt", ".md", ".json", ".xml"))
        read_only = item.read_only
    elif item.kind == "knowledge":
        relative = Path("knowledges") / (slug if archive else filename)
        container = f"/home/kali/knowledges/{relative.name}"
        executable = item.executable
        read_only = item.read_only
    else:
        relative = Path("skills") / slug if archive else Path("skills") / slug / "SKILL.md"
        container = f"/home/kali/.agents/skills/{slug}" if archive else f"/home/kali/.agents/skills/{slug}/SKILL.md"
        executable = False
        read_only = True
    return ProjectResource(
        name=item.name,
        path=relative,
        container_path=container,
        url=item.url,
        archive=archive,
        archive_single_file=False,
        executable=executable,
        read_only=read_only,
    )


BUILTIN_VERSION = 3


def _resource_size(path: Path) -> int | None:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return None
    total = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        total += child.stat().st_size
    return total


class ExtensionCatalog:
    def __init__(self, path: Path, resource_root: Path) -> None:
        self.path = path
        self.resource_root = resource_root
        self._lock = threading.Lock()
        self._ensure_file()

    def list_items(self) -> list[ExtensionItem]:
        with self._lock:
            return self._read()

    def public_items(self, exists) -> list[dict[str, object]]:
        items = []
        for item in self.list_items():
            resource = extension_resource(item)
            host_path = self.host_path(resource)
            downloaded = bool(exists(host_path))
            size = _resource_size(host_path) if downloaded else None
            items.append(
                {
                    **item.to_dict(),
                    "container_path": resource.container_path,
                    "downloaded": downloaded,
                    "size": size,
                    "version": release_label(item.url),
                    "status": "failed" if item.error else "ready" if downloaded else "missing",
                }
            )
        return items

    def add(
        self,
        kind: str,
        name: str,
        url: str,
        *,
        github_repo: str = "",
        asset_pattern: str = "",
        local_file: Path | None = None,
    ) -> ExtensionItem:
        cleaned_kind = kind.strip()
        cleaned_name = _clean_name(name)
        if local_file is None:
            cleaned_url = _clean_url(url)
        else:
            path = local_file.resolve()
            root = self.resource_root.resolve()
            if not path.is_file() or root not in path.parents:
                raise ValueError("本地文件无效")
            cleaned_url = path.as_uri()
        if cleaned_kind not in EXTENSION_KINDS:
            raise ValueError("kind must be knowledge, tool, or skill")
        with self._lock:
            items = self._read()
            if any(item.kind == cleaned_kind and item.name == cleaned_name for item in items):
                raise ValueError(f"{cleaned_name} already exists")
            item = ExtensionItem(
                id=_item_id(cleaned_kind, cleaned_name, [existing.id for existing in items]),
                kind=cleaned_kind,
                name=cleaned_name,
                url=cleaned_url,
                github_repo=github_repo,
                asset_pattern=asset_pattern,
            )
            items.append(item)
            self._write(items)
            return item

    def add_tool_repo(self, repo: str) -> ExtensionItem:
        parsed = parse_github_repo(repo)
        name = parsed.split("/", 1)[1]
        try:
            url, pattern = resolve_tool_release(parsed)
        except ExtensionResolveError as exc:
            if not _tool_release_uses_source(exc):
                raise
            return self._add_default_branch_archive("tool", parsed)
        return self.add("tool", name, url, github_repo=parsed, asset_pattern=pattern)

    def add_from_link(self, kind: str, link: str) -> ExtensionItem:
        cleaned_kind = kind.strip()
        if cleaned_kind not in EXTENSION_KINDS:
            raise ValueError("kind must be knowledge, tool, or skill")
        text = link.strip()
        if not text:
            raise ValueError("请填写链接")
        if cleaned_kind == "tool":
            return self.add_tool_repo(text)
        url = _clean_url(text)
        _reject_webpage_link(url)
        return self.add(cleaned_kind, name_from_download_url(url), url)

    def import_skill_repository(
        self,
        link: str,
        *,
        skip_existing: bool = False,
        ignore_ids: set[str] | None = None,
    ) -> list[ExtensionItem]:
        repo = github_repo_homepage(link)
        if not repo:
            raise ValueError("请填写 GitHub 仓库主页，例如 https://github.com/ljagiello/ctf-skills")
        packages = _skill_packages_from_zip(download_github_zipball(repo))
        names = [name for name, _payload in packages]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"{duplicates[0]} 在仓库里出现了多次")
        ignored = ignore_ids or set()
        existing = [item for item in self.list_items() if item.kind == "skill" and item.id not in ignored]
        existing_names = {item.name for item in existing}
        existing_slugs = {_slug(item.name) for item in existing}
        slugs = [_slug(name) for name in names]
        if len(slugs) != len(set(slugs)):
            raise ValueError("这些 Skill 会安装到同一个目录")
        selected: list[tuple[str, bytes]] = []
        for (name, payload), slug in zip(packages, slugs):
            if name in existing_names or slug in existing_slugs:
                if skip_existing:
                    continue
                raise ValueError(f"{name} already exists")
            selected.append((name, payload))
        packages = selected
        created: list[ExtensionItem] = []
        written: list[Path] = []
        try:
            destinations: list[Path] = []
            for name, payload in packages:
                destination = self.resource_root / "imports" / "skill" / _slug(name) / f"{_slug(name)}.zip"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                written.append(destination)
                destinations.append(destination)
            for (name, _payload), destination in zip(packages, destinations):
                created.append(self.add("skill", name, "", local_file=destination))
        except Exception:
            for item in created:
                try:
                    self.delete(item.id)
                except KeyError:
                    pass
            for path in written:
                path.unlink(missing_ok=True)
                parent = path.parent
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            raise
        return created

    def materialize_skill_repository(self, item_id: str) -> list[ExtensionItem]:
        item = next((entry for entry in self.list_items() if entry.id == item_id), None)
        if item is None:
            raise KeyError(item_id)
        if item.kind != "skill" or not github_repo_homepage(item.url):
            raise ValueError("这不是 GitHub Skill 仓库")
        created = self.import_skill_repository(item.url, skip_existing=True, ignore_ids={item_id})
        self.delete(item_id)
        return created

    def import_knowledge_repository(self, link: str) -> ExtensionItem:
        repo = github_repo_homepage(link)
        if not repo:
            raise ValueError("请填写 GitHub 仓库主页，例如 https://github.com/swisskyrepo/PayloadsAllTheThings")
        return self._add_default_branch_archive("knowledge", repo)

    def _add_default_branch_archive(self, kind: str, repo: str) -> ExtensionItem:
        name = _clean_name(repo.split("/", 1)[1])
        if any(
            item.kind == kind and (item.name == name or _slug(item.name) == _slug(name))
            for item in self.list_items()
        ):
            raise ValueError(f"{name} already exists")
        return self.add(kind, name, github_default_branch_archive_url(repo))

    def import_local(self, kind: str, filename: str, content: bytes) -> ExtensionItem:
        cleaned_kind = kind.strip()
        if cleaned_kind not in EXTENSION_KINDS:
            raise ValueError("kind must be knowledge, tool, or skill")
        safe_name = _safe_upload_name(filename)
        if not content:
            raise ValueError("文件是空的")
        if len(content) > MAX_IMPORT_BYTES:
            raise ValueError("文件不能超过 128MB")
        if cleaned_kind == "skill":
            skill_text = _skill_text_from_upload(safe_name, content)
            message = _skill_markdown_error(skill_text)
            if message:
                raise ValueError(message)
            display_name = _frontmatter_field(_frontmatter_block(skill_text), "name")
        else:
            display_name = _local_name_from_filename(safe_name)
        cleaned_name = _clean_name(display_name)
        if any(item.kind == cleaned_kind and item.name == cleaned_name for item in self.list_items()):
            raise ValueError(f"{cleaned_name} already exists")
        destination = self.resource_root / "imports" / cleaned_kind / _slug(cleaned_name) / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return self.add(cleaned_kind, cleaned_name, "", local_file=destination)

    def delete(self, item_id: str) -> None:
        with self._lock:
            items = self._read()
            kept = [item for item in items if item.id != item_id]
            if len(kept) == len(items):
                raise KeyError(item_id)
            self._write(kept)

    def refresh_release_url(self, item_id: str) -> ExtensionItem:
        current = next((item for item in self.list_items() if item.id == item_id), None)
        if current is None:
            raise KeyError(item_id)
        if not current.github_repo:
            return current
        url = resolve_latest_release_url(current.github_repo, current.asset_pattern)
        if url == current.url:
            return current
        with self._lock:
            items = self._read()
            for item in items:
                if item.id != item_id:
                    continue
                if item.github_repo != current.github_repo or item.asset_pattern != current.asset_pattern:
                    return item
                item.url = url
                self._write(items)
                return item
            raise KeyError(item_id)

    def set_error(self, item_id: str, error: str) -> None:
        with self._lock:
            items = self._read()
            for item in items:
                if item.id == item_id:
                    item.error = error
                    break
            self._write(items)

    def pairs(self) -> list[tuple[ExtensionItem, ProjectResource]]:
        return [(item, extension_resource(item)) for item in self.list_items()]

    def host_path(self, resource: ProjectResource) -> Path:
        path = Path(resource.path)
        if path.is_absolute():
            return path
        return self.resource_root / path

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(builtin_extensions(), version=BUILTIN_VERSION)
            return
        stored = self._stored_version()
        if stored >= BUILTIN_VERSION:
            return
        items = self._read()
        known = {item.id: item for item in items}
        for preset in builtin_extensions():
            current = known.get(preset.id)
            if current is None:
                items.append(preset)
                known[preset.id] = preset
                continue
            if stored < 2 and not current.github_repo and preset.github_repo and current.url == preset.url:
                current.github_repo = preset.github_repo
                current.asset_pattern = preset.asset_pattern
        self._write(items, version=BUILTIN_VERSION)

    def _read(self) -> list[ExtensionItem]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        items: list[ExtensionItem] = []
        if not isinstance(raw_items, list):
            return items
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "")
            name = str(raw.get("name") or "")
            url = str(raw.get("url") or "")
            item_id = str(raw.get("id") or "")
            if kind not in EXTENSION_KINDS or not name or not url or not item_id:
                continue
            read_only = raw.get("read_only", True)
            items.append(
                ExtensionItem(
                    id=item_id,
                    kind=kind,
                    name=name,
                    url=url,
                    error=str(raw.get("error") or ""),
                    container_path=str(raw.get("container_path") or ""),
                    archive_single_file=bool(raw.get("archive_single_file") or False),
                    executable=bool(raw.get("executable") or False),
                    read_only=read_only if isinstance(read_only, bool) else True,
                    github_repo=str(raw.get("github_repo") or ""),
                    asset_pattern=str(raw.get("asset_pattern") or ""),
                )
            )
        return items

    def _stored_version(self) -> int:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        try:
            return int(payload.get("builtins_version") or 0)
        except (TypeError, ValueError):
            return 0

    def _write(self, items: list[ExtensionItem], version: int | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if version is None:
            version = self._stored_version() if self.path.exists() else BUILTIN_VERSION
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"builtins_version": version, "items": [item.to_dict() for item in items]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)



class ExtensionResolveError(RuntimeError):
    pass


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def resolve_latest_release_url(repo: str, pattern: str, *, opener=None) -> str:
    if not pattern or any(char in pattern for char in "/\\\n\r"):
        raise ExtensionResolveError(f"invalid asset pattern: {pattern}")
    _tag, assets = fetch_latest_release(repo, opener=opener)
    return select_release_asset(assets, pattern, repo=repo, tag=_tag)


def resolve_tool_release(repo: str, *, opener=None) -> tuple[str, str]:
    parsed = parse_github_repo(repo)
    _tag, assets = fetch_latest_release(parsed, opener=opener)
    return choose_tool_release(assets, repo=parsed)


def github_repo_homepage(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host != "github.com" or parsed.scheme not in {"http", "https"}:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    candidate = f"{owner}/{repo}"
    if not _REPO_RE.fullmatch(candidate):
        return None
    return candidate


def github_default_branch_archive_url(repo: str, *, opener=None) -> str:
    if not _REPO_RE.fullmatch(repo):
        raise ValueError("仓库名无效")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "echo-agent",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"查询 {repo} 失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"查询 {repo} 失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"查询 {repo} 失败：响应不是 JSON 对象")
    branch = str(payload.get("default_branch") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or any(part in {"", ".", ".."} for part in branch.split("/")):
        raise ValueError(f"{repo} 没有可用的默认分支")
    return f"https://github.com/{repo}/archive/refs/heads/{quote(branch, safe='/')}.zip"


def download_github_zipball(repo: str, *, opener=None) -> bytes:
    if not _REPO_RE.fullmatch(repo):
        raise ValueError("仓库名无效")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/zipball",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "echo-agent",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=60) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_IMPORT_BYTES:
                    raise ValueError("文件不能超过 128MB")
                chunks.append(block)
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        raise ValueError(f"下载 {repo} 失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"下载 {repo} 失败：{exc}") from exc
    data = b"".join(chunks)
    if not data:
        raise ValueError(f"下载 {repo} 失败：文件是空的")
    return data


def fetch_latest_release(repo: str, *, opener=None) -> tuple[str, list[object]]:
    if not _REPO_RE.fullmatch(repo):
        raise ExtensionResolveError(f"invalid github repo: {repo}")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "echo-agent",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ExtensionResolveError(f"查询 {repo} 最新版本失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ExtensionResolveError(f"查询 {repo} 最新版本失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ExtensionResolveError(f"查询 {repo} 最新版本失败：响应不是 JSON 对象")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ExtensionResolveError(f"{repo} 最新版本没有资源列表")
    return str(payload.get("tag_name") or ""), assets


_IGNORED_ASSET_SUFFIXES = (
    ".sha256",
    ".sha512",
    ".sha1",
    ".asc",
    ".sig",
    ".pem",
    ".deb",
    ".rpm",
    ".txt",
    ".xml",
    ".md",
)


def choose_tool_release(assets: list[object], *, repo: str) -> tuple[str, str]:
    usable: list[tuple[str, str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not name or not url:
            continue
        lower = name.lower()
        if lower.endswith(_IGNORED_ASSET_SUFFIXES):
            continue
        usable.append((name, url, lower))
    arm = [
        item
        for item in usable
        if "linux" in item[2]
        and any(token in item[2] for token in ("arm64", "aarch64"))
        and item[2].endswith((".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2"))
    ]
    non_musl = [item for item in arm if "musl" not in item[2]]
    pool = non_musl or arm
    if len(pool) == 1:
        return pool[0][1], _asset_pattern(pool[0][0])
    if len(pool) > 1:
        names = "、".join(item[0] for item in pool)
        raise ExtensionResolveError(f"{repo} 匹配到多个 linux/arm64 资源：{names}")
    if len(usable) == 1:
        return usable[0][1], _asset_pattern(usable[0][0])
    if not usable:
        raise ExtensionResolveError(f"{repo} 最新版本没有可下载资源")
    names = "、".join(item[0] for item in usable[:8])
    raise ExtensionResolveError(f"{repo} 没有唯一的 linux/arm64 资源：{names}")


def _tool_release_uses_source(exc: ExtensionResolveError) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "HTTP 404",
            "没有可下载资源",
            "没有资源列表",
            "没有唯一的 linux/arm64 资源",
        )
    )


def parse_github_repo(value: str) -> str:
    text = value.strip().strip("/")
    if not text:
        raise ValueError("请填写 GitHub 仓库链接，例如 https://github.com/projectdiscovery/katana")
    _reject_tool_release_archive(text)
    text = re.sub(r"^https?://github\.com/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.git$", "", text)
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise ValueError("请填写 GitHub 仓库链接，例如 https://github.com/projectdiscovery/katana")
    repo = f"{parts[0]}/{parts[1]}"
    if not _REPO_RE.fullmatch(repo):
        raise ValueError("仓库名无效")
    return repo


def _reject_tool_release_archive(value: str) -> None:
    candidate = value if "://" in value else f"https://github.com/{value.lstrip('/')}"
    path = unquote(urlparse(candidate).path).lower()
    if "/releases/download/" not in path and "/releases/latest/download/" not in path:
        return
    if not path.endswith(_ARCHIVE_SUFFIXES):
        return
    raise ValueError("这是 release 压缩包链接。请改填仓库主页，例如 https://github.com/projectdiscovery/katana")


def _asset_pattern(name: str) -> str:
    pattern = re.sub(r"v?\d+(?:\.\d+)+", "*", name)
    return re.sub(r"\*{2,}", "*", pattern)


def select_release_asset(assets: list[object], pattern: str, *, repo: str, tag: str = "") -> str:
    matches: list[tuple[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name and url and fnmatch.fnmatch(name, pattern):
            matches.append((name, url))
    label = f"{repo}@{tag}" if tag else repo
    if not matches:
        raise ExtensionResolveError(f"{label} 没有匹配 {pattern} 的资源")
    if len(matches) > 1:
        names = "、".join(name for name, _url in matches)
        raise ExtensionResolveError(f"{label} 匹配到多个资源：{names}")
    return matches[0][1]


def release_label(url: str) -> str:
    match = re.search(r"/releases/download/([^/]+)/", url)
    if match:
        return match.group(1)
    branch = re.search(r"/heads/([^/.]+)", url)
    if branch:
        return branch.group(1)
    name = Path(urlparse(url).path).name
    return name or ""


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned or len(cleaned) > 80 or any(char in cleaned for char in "\\/\n\r\x00"):
        raise ValueError("name must be 1-80 characters without path separators")
    return cleaned


def _clean_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")) or any(char in cleaned for char in "\n\r "):
        raise ValueError("请填写 http 或 https 链接")
    return cleaned


def _safe_upload_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or any(ord(char) < 32 for char in name) or len(name) > 120:
        raise ValueError("文件名无效")
    return name


def _local_name_from_filename(filename: str) -> str:
    lower = filename.lower()
    stem = filename
    for suffix in (".tar.gz", ".tar.bz2", ".tbz2", ".tgz"):
        if lower.endswith(suffix):
            stem = filename[: -len(suffix)]
            break
    else:
        stem = Path(filename).stem or filename
    stem = stem.strip(".-_ ")[:80].rstrip(".-_ ")
    return stem or Path(filename).name


def _skill_text_from_upload(filename: str, content: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".md"):
        return content.decode("utf-8", errors="replace")
    if lower.endswith(".zip"):
        return _skill_text_from_zip(content)
    if lower.endswith((".tar.gz", ".tgz")):
        return _skill_text_from_tar(content)
    raise ValueError("Skill 只接受 SKILL.md、zip 或 tar.gz")


def _skill_text_from_zip(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            member = _skill_archive_member(names)
            data = archive.read(member)
    except zipfile.BadZipFile as exc:
        raise ValueError("这不是有效的 zip") from exc
    return _decode_skill_markdown(data)


def _skill_text_from_tar(content: bytes) -> str:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            member_name = _skill_archive_member([member.name for member in members])
            chosen = next(member for member in members if member.name == member_name)
            source = archive.extractfile(chosen)
            if source is None:
                raise ValueError("压缩包里的 SKILL.md 无法读取")
            data = source.read()
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("这不是有效的 tar.gz") from exc
    return _decode_skill_markdown(data)


def _decode_skill_markdown(data: bytes) -> str:
    if len(data) > 1_000_000:
        raise ValueError("SKILL.md 过大")
    return data.decode("utf-8", errors="replace")


def _skill_archive_member(names: list[str]) -> str:
    files = [name for name in names if name and not name.endswith("/")]
    prefix = _single_archive_prefix(files)
    for name in files:
        relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
        relative = relative.removeprefix("./")
        if relative == "SKILL.md":
            return name
    raise ValueError("压缩包里的 SKILL.md 必须在根目录，或只套一层目录。Codex 只读取这一层")


def _single_archive_prefix(names: list[str]) -> str:
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(roots) != 1:
        return ""
    root = next(iter(roots))
    if all(name == root or name.startswith(f"{root}/") for name in names):
        return f"{root}/"
    return ""


def _frontmatter_block(text: str) -> str:
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", text.lstrip("\ufeff"), re.DOTALL)
    return match.group(1) if match else ""


def _frontmatter_field(block: str, key: str) -> str:
    for line in block.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("\"\"'")
    return ""


def _skill_markdown_error(text: str) -> str | None:
    block = _frontmatter_block(text)
    if not block or not _frontmatter_field(block, "name") or not _frontmatter_field(block, "description"):
        return "SKILL.md 必须在开头包含 name 和 description，否则 Codex 不会加载"
    return None


def _skill_packages_from_zip(content: bytes) -> list[tuple[str, bytes]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("这不是有效的 zip") from exc
    with archive:
        members = _zip_relative_files(archive)
        by_relative = {relative: info for info, relative in members}
        if "SKILL.md" in by_relative:
            name = _skill_name_from_member(archive, by_relative["SKILL.md"])
            return [(name, _repack_zip(archive, members))]
        groups: dict[str, list[tuple[zipfile.ZipInfo, str]]] = {}
        for info, relative in members:
            if "/" not in relative:
                continue
            top, rest = relative.split("/", 1)
            if not top or top in {".", ".."}:
                continue
            groups.setdefault(top, []).append((info, rest))
        valid: list[tuple[str, bytes]] = []
        invalid_message = ""
        found_skill = False
        for top in sorted(groups):
            grouped = groups[top]
            skill_info = next((info for info, rest in grouped if rest == "SKILL.md"), None)
            if skill_info is None:
                continue
            found_skill = True
            try:
                name = _skill_name_from_member(archive, skill_info)
            except ValueError as exc:
                invalid_message = str(exc)
                continue
            valid.append((name, _repack_zip(archive, grouped)))
        if valid:
            return valid
        if invalid_message:
            raise ValueError(invalid_message)
        if not found_skill:
            raise ValueError(
                "仓库里没有可导入的 Skill。SKILL.md 需要放在仓库根目录，或每个一级子目录中，并在开头写上 name 和 description"
            )
        raise ValueError("SKILL.md 必须在开头包含 name 和 description，否则 Codex 不会加载")


def _zip_relative_files(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    infos: list[zipfile.ZipInfo] = []
    names: list[str] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        raw = info.filename.replace("\\", "/")
        if _unsafe_archive_name(raw):
            raise ValueError(f"压缩包路径不安全：{info.filename}")
        names.append(raw)
        infos.append(info)
    prefix = _single_archive_prefix(names)
    relative_members: list[tuple[zipfile.ZipInfo, str]] = []
    for info, name in zip(infos, names):
        relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
        relative = relative.removeprefix("./")
        if not relative:
            continue
        if _unsafe_archive_name(relative):
            raise ValueError(f"压缩包路径不安全：{info.filename}")
        relative_members.append((info, relative))
    return relative_members


def _unsafe_archive_name(name: str) -> bool:
    if name.startswith(("/", "\\")):
        return True
    return any(part == ".." for part in name.split("/"))


def _skill_name_from_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    text = _decode_skill_markdown(archive.read(info))
    message = _skill_markdown_error(text)
    if message:
        raise ValueError(message)
    return _clean_name(_frontmatter_field(_frontmatter_block(text), "name"))


def _repack_zip(archive: zipfile.ZipFile, members: list[tuple[zipfile.ZipInfo, str]]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for info, relative in members:
            output.writestr(relative, archive.read(info))
    return payload.getvalue()


def name_from_download_url(url: str) -> str:
    parsed = urlparse(_clean_url(url))
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("链接里没有文件名")
    host = (parsed.hostname or "").lower()
    if host == "github.com" and "archive" in parts:
        archive_at = parts.index("archive")
        if archive_at >= 2:
            return _clean_name(parts[1])
    filename = parts[-1]
    if filename.lower() == "skill.md" and len(parts) >= 2:
        return _clean_name(parts[-2])
    lower = filename.lower()
    stem = filename
    for suffix in (".tar.gz", ".tar.bz2", ".tbz2", ".tgz"):
        if lower.endswith(suffix):
            stem = filename[: -len(suffix)]
            break
    else:
        stem = Path(filename).stem or filename
    stem = stem.strip(".-_")
    if not stem:
        raise ValueError("无法从链接得到名称，请换一个带文件名的直链")
    return _clean_name(stem)


def _reject_webpage_link(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host != "github.com":
        return
    if len(parts) <= 2:
        raise ValueError(
            "这是仓库主页。知识库和 Skill 请填文件直链，例如 https://github.com/owner/repo/archive/refs/heads/main.zip"
        )
    if any(part in {"blob", "tree"} for part in parts):
        raise ValueError("这是网页地址，不能直接下载。请改用 raw 链接，或仓库的 archive zip 链接。")


def _host_relative(container_path: str) -> Path:
    path = container_path.rstrip("/")
    name = Path(path).name
    if path.startswith("/home/kali/.local/bin/"):
        return Path("local-bin") / name
    if path.startswith("/home/kali/tools/"):
        return Path("tools") / name
    if path.startswith("/home/kali/knowledges/"):
        return Path("knowledges") / name
    if path.startswith("/home/kali/.agents/skills/"):
        return Path("skills") / name
    return Path(name)


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return slug[:80] or "item"


def _item_id(kind: str, name: str, existing: list[str]) -> str:
    base = f"{kind}-{_slug(name)}".lower()
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _named_download(url: str, name: str) -> str:
    original = _download_filename(url, name).lower()
    if original.endswith(".tar.gz"):
        suffix = ".tar.gz"
    elif original.endswith(".tar.bz2"):
        suffix = ".tar.bz2"
    else:
        suffix = Path(original).suffix
    return f"{_slug(name)}{suffix}" if suffix else _slug(name)


def _download_filename(url: str, fallback: str) -> str:
    raw_name = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
    filename = Path(raw_name).name
    if not filename or filename in {".", ".."}:
        filename = _slug(fallback)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-")
    return cleaned[:120] or _slug(fallback)


def _is_archive(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_ARCHIVE_SUFFIXES)
