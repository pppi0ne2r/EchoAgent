from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
