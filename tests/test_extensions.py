from __future__ import annotations

import io
import json
import urllib.error
import tempfile
import zipfile
import unittest
from unittest.mock import patch
from pathlib import Path

from echo_agent.config import ProjectResource
from echo_agent.server.dispatcher_runner import DispatcherRunner
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.server.extensions import (
    ExtensionCatalog,
    ExtensionItem,
    ExtensionResolveError,
    builtin_extensions,
    choose_tool_release,
    extension_resource,
    github_default_branch_archive_url,
    parse_github_repo,
    release_label,
    resolve_latest_release_url,
    select_release_asset,
)
from echo_agent.storage import SQLiteProjectStore


class ExtensionCatalogTests(unittest.TestCase):
    def test_seed_and_resource_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            items = catalog.list_items()

            ysoserial = next(item for item in items if item.name == "ysoserial")
            resource = extension_resource(ysoserial)
            self.assertEqual(resource.path, Path("tools/ysoserial.jar"))
            self.assertEqual(resource.container_path, "/home/kali/tools/ysoserial.jar")
            self.assertFalse(resource.archive)
            self.assertFalse(resource.executable)
            katana = extension_resource(next(item for item in items if item.name == "katana"))
            self.assertEqual(katana.path, Path("local-bin/katana"))
            self.assertEqual(katana.container_path, "/home/kali/.local/bin/katana")
            self.assertTrue(katana.archive)
            self.assertTrue(katana.archive_single_file)
            self.assertTrue(katana.executable)
            templates = extension_resource(next(item for item in items if item.name == "nuclei-templates"))
            self.assertEqual(templates.container_path, "/home/kali/.local/nuclei-templates")
            self.assertFalse(templates.read_only)
            self.assertEqual(next(item for item in items if item.name == "katana").github_repo, "projectdiscovery/katana")
            self.assertEqual(next(item for item in items if item.name == "nuclei-templates").github_repo, "")

            skill = catalog.add("skill", "java deser", "https://example.com/skill.zip")
            skill_resource = extension_resource(skill)
            self.assertTrue(skill_resource.archive)
            self.assertEqual(skill_resource.container_path, "/home/kali/.agents/skills/java-deser")

            with self.assertRaises(ValueError):
                catalog.add("tool", "ysoserial", "https://example.com/other.jar")
            with self.assertRaises(ValueError):
                catalog.add("knowledge", "notes", "ftp://example.com/notes.md")

    def test_prepare_downloads_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ysoserial.jar"
            source.write_bytes(b"jar")
            catalog_path = root / "extensions.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "builtins_version": 2,
                        "items": [
                            {
                                "id": "tool-ysoserial",
                                "kind": "tool",
                                "name": "ysoserial",
                                "url": source.as_uri(),
                                "error": "",
                            },
                            {
                                "id": "knowledge-missing",
                                "kind": "knowledge",
                                "name": "missing",
                                "url": (root / "missing.md").as_uri(),
                                "error": "",
                            },
                            {
                                "id": "tool-katana",
                                "kind": "tool",
                                "name": "katana",
                                "url": "https://example.com/katana.zip",
                                "container_path": "/home/kali/.local/bin/katana",
                                "archive_single_file": True,
                                "executable": True,
                                "error": "",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            katana_path = root / "resources" / "local-bin" / "katana"
            katana_path.parent.mkdir(parents=True, exist_ok=True)
            katana_path.write_bytes(b"bin")
            catalog = ExtensionCatalog(catalog_path, root / "resources")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )

            failures = runtime.prepare_extensions()
            mounts = runtime._extension_directory_mounts(
                [(root / "resources" / "tools", "/home/kali/tools", False)]
            )

            self.assertEqual([item["name"] for item in failures], ["missing"])
            self.assertTrue((root / "resources" / "tools" / "ysoserial.jar").is_file())
            self.assertEqual(catalog.list_items()[0].error, "")
            self.assertIn("missing", catalog.list_items()[1].error)
            self.assertEqual(
                [container for _host, container, _read_only in mounts],
                ["/home/kali/knowledges", "/home/kali/.agents/skills", "/home/kali/pocs"],
            )
            item_mounts = runtime._extension_item_mounts(
                [(root / "resources" / "tools", "/home/kali/tools", False)]
            )
            self.assertIn(
                "/home/kali/.local/bin/katana",
                [container for _host, container, _read_only in item_mounts],
            )
            ready = runtime.extensions.public_items(runtime._resource_exists)
            self.assertEqual(ready[0]["status"], "ready")
            self.assertEqual(ready[1]["status"], "failed")


class ExtensionBuiltinUpgradeTests(unittest.TestCase):
    def test_existing_catalog_gains_missing_builtins_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "extensions.json"
            path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "tool-ysoserial",
                                "kind": "tool",
                                "name": "ysoserial",
                                "url": "https://example.com/ysoserial.jar",
                                "error": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = ExtensionCatalog(path, root / "resources")
            names = {item.name for item in catalog.list_items()}
            self.assertIn("katana", names)
            self.assertEqual(
                next(item.url for item in catalog.list_items() if item.name == "ysoserial"),
                "https://example.com/ysoserial.jar",
            )
            catalog.delete(next(item.id for item in catalog.list_items() if item.name == "katana"))
            reopened = ExtensionCatalog(path, root / "resources")
            self.assertNotIn("katana", {item.name for item in reopened.list_items()})


class ExtensionUpdateTests(unittest.TestCase):
    def test_update_replaces_existing_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "katana.bin"
            binary.write_bytes(b"new-binary")
            archive_path = root / "templates.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("repo-main/new.yaml", "new\n")
            catalog_path = root / "extensions.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "builtins_version": 2,
                        "items": [
                            {
                                "id": "tool-katana",
                                "kind": "tool",
                                "name": "katana",
                                "url": binary.as_uri(),
                                "container_path": "/home/kali/.local/bin/katana",
                                "executable": True,
                                "error": "old error",
                            },
                            {
                                "id": "knowledge-nuclei-templates",
                                "kind": "knowledge",
                                "name": "nuclei-templates",
                                "url": archive_path.as_uri(),
                                "container_path": "/home/kali/.local/nuclei-templates",
                                "read_only": False,
                                "error": "",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            installed = root / "resources" / "local-bin" / "katana"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"old-binary")
            templates = root / "resources" / "nuclei-templates"
            templates.mkdir()
            (templates / "old.yaml").write_text("old", encoding="utf-8")
            inode = templates.stat().st_ino
            catalog = ExtensionCatalog(catalog_path, root / "resources")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )

            runtime.prepare_extensions()
            self.assertEqual(installed.read_bytes(), b"old-binary")

            failures = runtime.update_extensions(item_id="tool-katana")
            self.assertEqual(failures, [])
            self.assertEqual(installed.read_bytes(), b"new-binary")
            self.assertEqual(catalog.list_items()[0].error, "")
            self.assertTrue(installed.stat().st_mode & 0o111)

            failures = runtime.update_extensions(kind="knowledge")
            self.assertEqual(failures, [])
            self.assertEqual(templates.stat().st_ino, inode)
            self.assertEqual((templates / "new.yaml").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((templates / "old.yaml").exists())

            with self.assertRaises(KeyError):
                runtime.update_extensions(item_id="missing")


class ExtensionStartTests(unittest.TestCase):
    def test_project_start_stops_when_extension_download_fails(self) -> None:
        class FailingRuntime(DockerProjectRuntime):
            def __init__(self) -> None:
                super().__init__(
                    image="worker:test",
                    context=Path("container"),
                    dockerfile=Path("container/Dockerfile"),
                )

            def prepare_extensions(self, log=None):
                return [{"name": "ysoserial", "error": "404"}]

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="demo", target_url="http://target", objective="拿到 flag")
            runner = DispatcherRunner(store, runtime=FailingRuntime())

            with self.assertRaises(ValueError) as caught:
                runner.start(project.id)

            messages = [item["message"] for item in store.list_project_logs(project.id)]
            self.assertIn("ysoserial", str(caught.exception))
            self.assertTrue(any(message.startswith("扩展下载失败") and "ysoserial" in message for message in messages))
            self.assertEqual(runner.list_states(), [])




class ExtensionReleaseTests(unittest.TestCase):
    def test_asset_patterns_pick_one_linux_arm64_file(self) -> None:
        cases = {
            "ysoserial-all.jar": ["ysoserial-all.jar", "ysoserial-all.jar.asc"],
            "katana_*_linux_arm64.zip": [
                "katana_1.8.0_linux_arm64.zip",
                "katana_1.8.0_linux_amd64.zip",
                "katana_1.8.0_macOS_arm64.zip",
            ],
            "nuclei_*_linux_arm64.zip": [
                "nuclei_3.11.1_linux_arm.zip",
                "nuclei_3.11.1_linux_arm64.zip",
                "nuclei_3.11.1_checksums.txt",
            ],
            "dalfox*linux-aarch64.tar.gz": [
                "dalfox-v3.2.4-linux-aarch64-musl.tar.gz",
                "dalfox-v3.2.4-linux-aarch64.tar.gz",
                "dalfox-v3.2.4-linux-aarch64.tar.gz.sha256",
            ],
            "cloudfox*linux-arm64.zip": [
                "cloudfox-linux-arm64.zip",
                "cloudfox-linux-amd64.zip",
                "cloudfox-macos-arm64.zip",
            ],
        }
        for pattern, names in cases.items():
            assets = [{"name": name, "browser_download_url": f"https://example.com/{name}"} for name in names]
            chosen = select_release_asset(assets, pattern, repo="example/tool", tag="v9")
            self.assertTrue(chosen.rsplit("/", 1)[-1] in {"ysoserial-all.jar", "katana_1.8.0_linux_arm64.zip", "nuclei_3.11.1_linux_arm64.zip", "dalfox-v3.2.4-linux-aarch64.tar.gz", "cloudfox-linux-arm64.zip"})

    def test_multiple_matches_are_rejected(self) -> None:
        assets = [
            {"name": "tool-a.zip", "browser_download_url": "https://example.com/a"},
            {"name": "tool-b.zip", "browser_download_url": "https://example.com/b"},
        ]
        with self.assertRaises(ExtensionResolveError):
            select_release_asset(assets, "tool-*.zip", repo="example/tool")

    def test_http_error_keeps_saved_url(self) -> None:
        class Response:
            def __enter__(self):
                raise urllib.error.HTTPError("https://api.github.com", 403, "rate limit", hdrs=None, fp=None)

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset = next(item for item in builtin_extensions() if item.name == "katana")
            path = root / "extensions.json"
            path.write_text(
                json.dumps({"builtins_version": 2, "items": [preset.to_dict()]}),
                encoding="utf-8",
            )
            catalog = ExtensionCatalog(path, root / "resources")
            with self.assertRaises(ExtensionResolveError):
                resolve_latest_release_url(preset.github_repo, preset.asset_pattern, opener=lambda *_args, **_kwargs: Response())
            self.assertEqual(catalog.list_items()[0].url, preset.url)

    def test_existing_default_url_starts_tracking_release_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset = next(item for item in builtin_extensions() if item.name == "katana")
            custom = next(item for item in builtin_extensions() if item.name == "nuclei")
            path = root / "extensions.json"
            path.write_text(
                json.dumps(
                    {
                        "builtins_version": 1,
                        "items": [
                            {**preset.to_dict(), "github_repo": "", "asset_pattern": ""},
                            {
                                "id": custom.id,
                                "kind": custom.kind,
                                "name": custom.name,
                                "url": "https://example.com/nuclei.zip",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            catalog = ExtensionCatalog(path, root / "resources")
            katana = next(item for item in catalog.list_items() if item.name == "katana")
            nuclei = next(item for item in catalog.list_items() if item.name == "nuclei")
            self.assertEqual(katana.github_repo, "projectdiscovery/katana")
            self.assertEqual(katana.url, preset.url)
            self.assertEqual(nuclei.github_repo, "")
            self.assertEqual(nuclei.url, "https://example.com/nuclei.zip")

    def test_update_replaces_url_from_release_and_failure_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_file = root / "old.zip"
            new_file = root / "new.bin"
            old_file.write_bytes(b"old-binary")
            new_file.write_bytes(b"new-binary")
            preset = next(item for item in builtin_extensions() if item.name == "katana")
            path = root / "extensions.json"
            payload = preset.to_dict()
            payload["url"] = old_file.as_uri()
            path.write_text(json.dumps({"builtins_version": 2, "items": [payload]}), encoding="utf-8")
            installed = root / "resources" / "local-bin" / "katana"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"installed")
            catalog = ExtensionCatalog(path, root / "resources")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )

            with patch("echo_agent.server.extensions.resolve_latest_release_url", side_effect=ExtensionResolveError("查询失败")):
                failures = runtime.update_extensions(item_id="tool-katana")
            self.assertEqual(failures[0]["error"], "查询失败")
            self.assertEqual(installed.read_bytes(), b"installed")
            self.assertEqual(catalog.list_items()[0].url, old_file.as_uri())

            with patch("echo_agent.server.extensions.resolve_latest_release_url", return_value=new_file.as_uri()):
                failures = runtime.update_extensions(item_id="tool-katana")
            self.assertEqual(failures, [])
            self.assertEqual(installed.read_bytes(), b"new-binary")
            self.assertEqual(catalog.list_items()[0].url, new_file.as_uri())
            self.assertEqual(catalog.list_items()[0].github_repo, preset.github_repo)



class ToolRepoTests(unittest.TestCase):
    def test_release_label_reads_version_from_download_url(self) -> None:
        self.assertEqual(
            release_label("https://github.com/projectdiscovery/katana/releases/download/v1.8.0/katana_1.8.0_linux_arm64.zip"),
            "v1.8.0",
        )
        self.assertEqual(
            release_label("https://github.com/projectdiscovery/nuclei-templates/archive/refs/heads/main.zip"),
            "main",
        )

    def test_repo_name_and_arm64_asset_are_derived(self) -> None:
        self.assertEqual(parse_github_repo("projectdiscovery/katana"), "projectdiscovery/katana")
        self.assertEqual(
            parse_github_repo("https://github.com/projectdiscovery/katana/releases/tag/v1.8.0"),
            "projectdiscovery/katana",
        )
        with self.assertRaises(ValueError) as caught:
            parse_github_repo(
                "https://github.com/projectdiscovery/katana/releases/download/v1.8.0/katana_1.8.0_linux_arm64.zip"
            )
        self.assertIn("请改填仓库主页", str(caught.exception))
        self.assertIn("https://github.com/projectdiscovery/katana", str(caught.exception))
        assets = [
            {"name": "katana_1.8.0_linux_amd64.zip", "browser_download_url": "https://example.com/amd64"},
            {"name": "katana_1.8.0_linux_arm64.zip", "browser_download_url": "https://example.com/arm64"},
            {"name": "katana_1.8.0_checksums.txt", "browser_download_url": "https://example.com/sums"},
        ]
        url, pattern = choose_tool_release(assets, repo="projectdiscovery/katana")
        self.assertEqual(url, "https://example.com/arm64")
        self.assertEqual(pattern, "katana_*_linux_arm64.zip")

        dalfox = [
            {"name": "dalfox-v3.2.4-linux-aarch64-musl.tar.gz", "browser_download_url": "https://example.com/musl"},
            {"name": "dalfox-v3.2.4-linux-aarch64.tar.gz", "browser_download_url": "https://example.com/gnu"},
            {"name": "dalfox-v3.2.4-linux-aarch64.tar.gz.sha256", "browser_download_url": "https://example.com/sha"},
        ]
        url, pattern = choose_tool_release(dalfox, repo="hahwul/dalfox")
        self.assertEqual(url, "https://example.com/gnu")
        self.assertEqual(pattern, "dalfox-*-linux-aarch64.tar.gz")

        jar = [{"name": "ysoserial-all.jar", "browser_download_url": "https://example.com/yso.jar"}]
        url, pattern = choose_tool_release(jar, repo="frohoff/ysoserial")
        self.assertEqual(url, "https://example.com/yso.jar")
        self.assertEqual(pattern, "ysoserial-all.jar")

        with self.assertRaises(ExtensionResolveError):
            choose_tool_release(
                [
                    {"name": "tool-linux-arm64.zip", "browser_download_url": "https://example.com/a"},
                    {"name": "tool-linux-aarch64.tar.gz", "browser_download_url": "https://example.com/b"},
                ],
                repo="example/tool",
            )

    def test_add_tool_uses_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with patch(
                "echo_agent.server.extensions.resolve_tool_release",
                return_value=("https://example.com/katana_1.8.0_linux_arm64.zip", "katana_*_linux_arm64.zip"),
            ):
                item = catalog.add_tool_repo("https://github.com/example/demo-tool")
            self.assertEqual(item.name, "demo-tool")
            self.assertEqual(item.github_repo, "example/demo-tool")
            self.assertEqual(item.asset_pattern, "katana_*_linux_arm64.zip")
            self.assertEqual(item.url, "https://example.com/katana_1.8.0_linux_arm64.zip")

    def test_link_only_add_derives_name(self) -> None:
        from echo_agent.server.extensions import name_from_download_url

        self.assertEqual(
            name_from_download_url("https://github.com/owner/repo/archive/refs/heads/main.zip"),
            "repo",
        )
        self.assertEqual(name_from_download_url("https://example.com/java-deser/SKILL.md"), "java-deser")
        self.assertEqual(name_from_download_url("https://example.com/files/notes.tar.gz"), "notes")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            knowledge = catalog.add_from_link(
                "knowledge",
                "https://github.com/acme/php-filter/archive/refs/heads/main.zip",
            )
            skill = catalog.add_from_link("skill", "https://example.com/java-deser/SKILL.md")
            self.assertEqual(knowledge.name, "php-filter")
            self.assertEqual(skill.name, "java-deser")
            with self.assertRaises(ValueError):
                catalog.add_from_link("knowledge", "https://github.com/acme/php-filter")
            with self.assertRaises(ValueError):
                catalog.add_from_link("skill", "https://github.com/acme/skills/blob/main/SKILL.md")
            with self.assertRaises(ValueError) as caught:
                catalog.add_from_link(
                    "tool",
                    "https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-arm64.tar.gz",
                )
            self.assertIn("请改填仓库主页", str(caught.exception))


class LocalExtensionImportTests(unittest.TestCase):
    def test_local_skill_markdown_and_zip_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            markdown = b"---\nname: java-deser\ndescription: read gadget chains\n---\n# hi\n"
            item = catalog.import_local("skill", "SKILL.md", markdown)
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )
            self.assertEqual(runtime.update_extensions(item_id=item.id), [])
            installed = root / "resources" / "skills" / "java-deser" / "SKILL.md"
            self.assertEqual(installed.read_bytes(), markdown)

            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("nested-skill/SKILL.md", "---\nname: nested-skill\ndescription: nested\n---\n")
                archive.writestr("nested-skill/scripts/run.py", "print(1)\n")
            zipped = catalog.import_local("skill", "nested-skill.zip", payload.getvalue())
            self.assertEqual(runtime.update_extensions(item_id=zipped.id), [])
            self.assertTrue((root / "resources" / "skills" / "nested-skill" / "SKILL.md").is_file())
            self.assertTrue((root / "resources" / "skills" / "nested-skill" / "scripts" / "run.py").is_file())

    def test_invalid_skill_is_rejected_before_it_can_be_missed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with self.assertRaises(ValueError) as caught:
                catalog.import_local("skill", "SKILL.md", b"# no frontmatter\n")
            self.assertIn("不会加载", str(caught.exception))
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("repo/docs/SKILL.md", "---\nname: too-deep\ndescription: no\n---\n")
            with self.assertRaises(ValueError) as caught:
                catalog.import_local("skill", "too-deep.zip", payload.getvalue())
            self.assertIn("只套一层目录", str(caught.exception))
            self.assertFalse(any(item.kind == "skill" for item in catalog.list_items()))

    def test_local_tool_and_knowledge_files_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            tool = catalog.import_local("tool", "local-helper.jar", b"jar-bytes")
            knowledge = catalog.import_local("knowledge", "notes.md", b"notes\n")
            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )
            self.assertEqual(runtime.update_extensions(item_id=tool.id), [])
            self.assertEqual(runtime.update_extensions(item_id=knowledge.id), [])
            self.assertEqual((root / "resources" / "tools" / "local-helper.jar").read_bytes(), b"jar-bytes")
            self.assertEqual((root / "resources" / "knowledges" / "notes.md").read_bytes(), b"notes\n")
            self.assertTrue(tool.url.startswith("file:"))


class SkillRepositoryImportTests(unittest.TestCase):
    def _zip(self, files: dict[str, str]) -> bytes:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return payload.getvalue()

    def _skill(self, name: str) -> str:
        return f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n"

    def _catalog(self, root: Path) -> ExtensionCatalog:
        return ExtensionCatalog(root / "extensions.json", root / "resources")

    def _runtime(self, root: Path, catalog: ExtensionCatalog) -> DockerProjectRuntime:
        return DockerProjectRuntime(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
            resource_root=root / "resources",
            extensions=catalog,
        )

    def test_repository_homepage_installs_each_top_level_skill(self) -> None:
        content = self._zip(
            {
                "repo-main/README.md": "ignore",
                "repo-main/ctf-web/SKILL.md": self._skill("ctf-web"),
                "repo-main/ctf-web/scripts/run.py": "print(1)\n",
                "repo-main/ctf-pwn/SKILL.md": self._skill("ctf-pwn"),
                "repo-main/docs/notes.md": "skip",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content) as download:
                items = catalog.import_skill_repository("https://github.com/ljagiello/ctf-skills")
            download.assert_called_once_with("ljagiello/ctf-skills")
            self.assertEqual([item.name for item in items], ["ctf-pwn", "ctf-web"])
            self.assertTrue(all(item.github_repo == "" and item.url.startswith("file:") for item in items))
            runtime = self._runtime(root, catalog)
            for item in items:
                self.assertEqual(runtime.update_extensions(item_id=item.id), [])
            self.assertIn("name: ctf-web", (root / "resources" / "skills" / "ctf-web" / "SKILL.md").read_text())
            self.assertEqual((root / "resources" / "skills" / "ctf-web" / "scripts" / "run.py").read_text(), "print(1)\n")
            self.assertTrue((root / "resources" / "skills" / "ctf-pwn" / "SKILL.md").is_file())
            self.assertFalse((root / "resources" / "skills" / "ctf-skills").exists())
            self.assertFalse((root / "resources" / "skills" / "docs").exists())

    def test_repository_without_skill_adds_nothing(self) -> None:
        content = self._zip({"repo-main/README.md": "no skill"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content):
                with self.assertRaises(ValueError) as caught:
                    catalog.import_skill_repository("https://github.com/acme/empty")
            self.assertIn("没有可导入的 Skill", str(caught.exception))
            self.assertFalse(any(item.kind == "skill" for item in catalog.list_items()))
            self.assertFalse((root / "resources" / "imports").exists())

    def test_existing_skill_name_blocks_entire_import(self) -> None:
        content = self._zip(
            {
                "repo-main/ctf-web/SKILL.md": self._skill("ctf-web"),
                "repo-main/ctf-pwn/SKILL.md": self._skill("ctf-pwn"),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            catalog.import_local("skill", "SKILL.md", self._skill("ctf-web").encode())
            before = [item.name for item in catalog.list_items()]
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content):
                with self.assertRaises(ValueError) as caught:
                    catalog.import_skill_repository("https://github.com/acme/ctf-skills")
            self.assertIn("already exists", str(caught.exception))
            self.assertEqual([item.name for item in catalog.list_items()], before)
            self.assertFalse((root / "resources" / "imports" / "skill" / "ctf-pwn").exists())

    def test_direct_skill_link_still_works_and_knowledge_homepage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            skill = catalog.add_from_link("skill", "https://example.com/java-deser/SKILL.md")
            self.assertEqual(skill.name, "java-deser")
            self.assertEqual(skill.url, "https://example.com/java-deser/SKILL.md")
            with self.assertRaises(ValueError) as caught:
                catalog.add_from_link("knowledge", "https://github.com/acme/php-filter")
            self.assertIn("仓库主页", str(caught.exception))
            with self.assertRaises(ValueError):
                catalog.add_from_link("skill", "https://github.com/acme/skills/blob/main/SKILL.md")

    def test_root_skill_archive_stays_one_skill(self) -> None:
        content = self._zip(
            {
                "repo-main/SKILL.md": self._skill("solo"),
                "repo-main/ctf-web/SKILL.md": self._skill("ctf-web"),
                "repo-main/notes.md": "keep",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content):
                items = catalog.import_skill_repository("https://github.com/acme/solo-skill.git")
            self.assertEqual([item.name for item in items], ["solo"])
            runtime = self._runtime(root, catalog)
            self.assertEqual(runtime.update_extensions(item_id=items[0].id), [])
            installed = root / "resources" / "skills" / "solo"
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertTrue((installed / "ctf-web" / "SKILL.md").is_file())
            self.assertEqual((installed / "notes.md").read_text(), "keep")
            self.assertFalse(any(item.name == "ctf-web" for item in catalog.list_items()))

    def test_invalid_skill_is_skipped_when_another_is_valid(self) -> None:
        content = self._zip(
            {
                "repo-main/ctf-web/SKILL.md": self._skill("ctf-web"),
                "repo-main/broken/SKILL.md": "# missing frontmatter\n",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content):
                items = catalog.import_skill_repository("https://github.com/acme/mixed")
            self.assertEqual([item.name for item in items], ["ctf-web"])

    def test_invalid_frontmatter_adds_nothing_when_no_skill_is_usable(self) -> None:
        content = self._zip({"repo-main/ctf-web/SKILL.md": "# missing frontmatter\n"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=content):
                with self.assertRaises(ValueError) as caught:
                    catalog.import_skill_repository("https://github.com/acme/broken")
            self.assertIn("不会加载", str(caught.exception))
            self.assertFalse(any(item.kind == "skill" for item in catalog.list_items()))

    def test_unsafe_zip_path_adds_nothing(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("repo-main/../../SKILL.md", self._skill("evil"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self._catalog(root)
            with patch("echo_agent.server.extensions.download_github_zipball", return_value=payload.getvalue()):
                with self.assertRaises(ValueError) as caught:
                    catalog.import_skill_repository("https://github.com/acme/evil")
            self.assertIn("路径不安全", str(caught.exception))
            self.assertFalse(any(item.kind == "skill" for item in catalog.list_items()))


class KnowledgeRepositoryImportTests(unittest.TestCase):
    def test_homepage_tracks_default_branch_archive(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"default_branch": "develop"}).encode()

        self.assertEqual(
            github_default_branch_archive_url("acme/notes", opener=lambda *_args, **_kwargs: Response()),
            "https://github.com/acme/notes/archive/refs/heads/develop.zip",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with patch(
                "echo_agent.server.extensions.github_default_branch_archive_url",
                return_value="https://github.com/acme/notes/archive/refs/heads/main.zip",
            ):
                item = catalog.import_knowledge_repository("https://github.com/acme/notes")
            self.assertEqual(item.kind, "knowledge")
            self.assertEqual(item.name, "notes")
            self.assertEqual(item.github_repo, "")
            self.assertEqual(item.url, "https://github.com/acme/notes/archive/refs/heads/main.zip")
            resource = extension_resource(item)
            self.assertTrue(resource.archive)
            self.assertEqual(resource.path, Path("knowledges/notes"))
            self.assertEqual(resource.container_path, "/home/kali/knowledges/notes")

            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr("notes-main/README.md", "notes\n")
                archive.writestr("notes-main/payloads/xss.txt", "payload\n")

            def retrieve(url, filename):
                self.assertEqual(url, item.url)
                Path(filename).write_bytes(payload.getvalue())
                return filename, None

            runtime = DockerProjectRuntime(
                image="worker:test",
                context=Path("container"),
                dockerfile=Path("container/Dockerfile"),
                resource_root=root / "resources",
                extensions=catalog,
            )
            with patch("echo_agent.server.docker_runtime.urllib.request.urlretrieve", side_effect=retrieve):
                self.assertEqual(runtime.update_extensions(item_id=item.id), [])
            installed = root / "resources" / "knowledges" / "notes"
            self.assertEqual((installed / "README.md").read_text(), "notes\n")
            self.assertEqual((installed / "payloads" / "xss.txt").read_text(), "payload\n")

    def test_existing_knowledge_name_blocks_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with patch("echo_agent.server.extensions.github_default_branch_archive_url") as archive:
                with self.assertRaises(ValueError) as caught:
                    catalog.import_knowledge_repository("https://github.com/projectdiscovery/nuclei-templates")
            self.assertIn("already exists", str(caught.exception))
            archive.assert_not_called()

    def test_tool_without_release_uses_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with patch(
                "echo_agent.server.extensions.resolve_tool_release",
                side_effect=ExtensionResolveError("example/scripts 最新版本没有可下载资源"),
            ):
                with patch(
                    "echo_agent.server.extensions.github_default_branch_archive_url",
                    return_value="https://github.com/example/scripts/archive/refs/heads/main.zip",
                ) as archive:
                    item = catalog.add_tool_repo("https://github.com/example/scripts")
            archive.assert_called_once_with("example/scripts")
            self.assertEqual(item.name, "scripts")
            self.assertEqual(item.kind, "tool")
            self.assertEqual(item.github_repo, "")
            self.assertEqual(item.asset_pattern, "")
            resource = extension_resource(item)
            self.assertTrue(resource.archive)
            self.assertEqual(resource.container_path, "/home/kali/tools/scripts")

    def test_ambiguous_tool_release_does_not_fall_back_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = ExtensionCatalog(root / "extensions.json", root / "resources")
            with patch(
                "echo_agent.server.extensions.resolve_tool_release",
                side_effect=ExtensionResolveError("example/tool 匹配到多个 linux/arm64 资源：a.zip、b.zip"),
            ):
                with patch("echo_agent.server.extensions.github_default_branch_archive_url") as archive:
                    with self.assertRaises(ExtensionResolveError):
                        catalog.add_tool_repo("https://github.com/example/tool")
            archive.assert_not_called()
            self.assertFalse(any(item.name == "tool" for item in catalog.list_items()))


if __name__ == "__main__":
    unittest.main()
