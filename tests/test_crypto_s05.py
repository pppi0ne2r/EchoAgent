from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.dispatcher.cli import build_parser, main as cli_main
from echo_agent.dispatcher.prompts import _format_project_context, _format_worker_context
from echo_agent.server.app import UvicornServerHandle, create_app
from echo_agent.storage import SQLiteProjectStore


class RuntimeStub:
    def prepare_shared_resources(self) -> None:
        pass

    def apply_status_transition(self, project, previous_status, log=None) -> None:
        pass


class CryptoS05Tests(unittest.TestCase):
    def test_upload_bytes_manifest_and_url_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
                app = create_app(store)
            with TestClient(app) as http:
                rejected = http.post("/projects", json={"task_type": "crypto", "crypto": {
                    "schema_version": 1, "inputs": [{"id": "fake", "role": "parameters",
                    "path": "inputs/fake.txt", "sha256": "0" * 64, "size_bytes": 4}],
                }})
                self.assertEqual(rejected.status_code, 400)

                created = http.post("/projects", json={"name": "rsa", "task_type": "crypto",
                    "objective": "factor n"})
                self.assertEqual(created.status_code, 201)
                project_id = created.json()["id"]
                self.assertIsNone(created.json()["target_url"])
                raw = b"n=0x0037\n\x00\xff\n"
                upload = http.post(
                    f"/projects/{project_id}/inputs",
                    params={"filename": "rsa.bin", "role": "parameters", "input_id": "rsa-1",
                            "number_radix": 16, "byte_order": "big", "byte_length": 2},
                    content=raw,
                    headers={"Content-Type": "application/octet-stream"},
                )
                self.assertEqual(upload.status_code, 201, upload.text)
                ref = upload.json()
                self.assertEqual(ref["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(ref["size_bytes"], len(raw))
                self.assertEqual((ref["number_radix"], ref["byte_order"], ref["byte_length"]), (16, "big", 2))
                self.assertEqual(ref["filename"], "rsa.bin")
                files = CryptoProjectFiles(Path(directory) / "projects")
                self.assertEqual(files.verify_ref(project_id, ref, "input").read_bytes(), raw)
                loaded = store.get_project(project_id)
                self.assertEqual(loaded.crypto["inputs"], [ref])
                self.assertIn(f"/home/kali/workspace/{ref['path']}", _format_worker_context(loaded))
                self.assertIn(f"/home/kali/workspace/{ref['path']}", _format_project_context(loaded))

                self.assertEqual(http.post(f"/projects/{project_id}/inputs",
                    params={"filename": "other.bin", "input_id": "rsa-1"},
                    content=b"other", headers={"Content-Type": "application/octet-stream"}).status_code, 400)
                self.assertEqual(len(store.get_project(project_id).crypto["inputs"]), 1)
                self.assertEqual(len(list((Path(directory) / "projects" / str(project_id) / "inputs").iterdir())), 1)
                self.assertEqual(http.post(f"/projects/{project_id}/inputs",
                    params={"filename": "string.txt"}, json={"path": "C:/local/file"}).status_code, 415)
                forged = loaded.to_dict()
                forged["crypto"]["inputs"] = []
                self.assertEqual(http.put(f"/projects/{project_id}", json=forged).status_code, 400)
                self.assertEqual(http.post(f"/projects/{project_id}/inputs",
                    params={"filename": "../escape.txt"}, content=b"bad",
                    headers={"Content-Type": "application/octet-stream"}).status_code, 400)
                self.assertEqual(http.post(f"/projects/{project_id}/inputs",
                    params={"filename": "wrong.bin", "number_radix": 7}, content=b"7",
                    headers={"Content-Type": "application/octet-stream"}).status_code, 400)
                self.assertEqual(http.patch(f"/projects/{project_id}/status",
                    json={"status": "active"}).status_code, 200)
                self.assertEqual(store.get_project(project_id).status.value, "active")

                web = http.post("/projects", json={"name": "web", "target_url": "https://example.test",
                    "objective": "find flag"})
                self.assertEqual(web.status_code, 201)
                web_id = web.json()["id"]
                self.assertNotIn("crypto", web.json())
                self.assertEqual(http.post(f"/projects/{web_id}/inputs",
                    params={"filename": "x.txt"}, content=b"x",
                    headers={"Content-Type": "application/octet-stream"}).status_code, 400)
                self.assertEqual(http.patch(f"/projects/{web_id}/status",
                    json={"status": "active"}).status_code, 200)
                self.assertEqual(http.get(f"/projects/{web_id}/run").status_code, 200)

    def test_limit_and_cli_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
                app = create_app(store)
            with TestClient(app) as http:
                project_id = http.post("/projects", json={"task_type": "crypto"}).json()["id"]
                with patch("echo_agent.server.app.MAX_INPUT_BYTES", 8):
                    response = http.post(f"/projects/{project_id}/inputs",
                        params={"filename": "large.bin"}, content=b"123456789",
                        headers={"Content-Type": "application/octet-stream"})
                self.assertEqual(response.status_code, 413)
                self.assertEqual(store.get_project(project_id).crypto["inputs"], [])
                self.assertEqual(list((Path(directory) / "projects" / str(project_id) / "inputs").glob("*")), [])
        parsed = build_parser().parse_args(["create-project", "rsa", "--task-type", "crypto",
            "--input", "parameters=rsa.txt", "--activate"])
        self.assertEqual(parsed.input, ["parameters=rsa.txt"])
        self.assertIsNone(parsed.target_url)
        parsed_upload = build_parser().parse_args(["upload-input", "12", "cipher.bin", "--role", "ciphertext"])
        self.assertEqual(parsed_upload.role, "ciphertext")

    def test_cli_uploads_local_file_bytes_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "parameters.txt"
            raw = b"n=55\ne=3\n"
            source.write_bytes(raw)
            store = SQLiteProjectStore(root / "state.db")
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
                app = create_app(store)
            server = UvicornServerHandle("127.0.0.1", 0, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(ValueError):
                    cli_main(["--server-url", address, "create-project", "bad",
                        "--task-type", "crypto", "--input", "missing-role"])
                self.assertEqual(store.list_projects(), [])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(["--server-url", address, "create-project", "rsa-cli",
                        "--task-type", "crypto", "--objective", "factor n", "--input",
                        f"parameters={source}", "--number-radix", "10", "--activate"]), 0)
                project = store.list_projects()[0]
                self.assertEqual(project.status.value, "active")
                reference = project.crypto["inputs"][0]
                self.assertEqual(reference["number_radix"], 10)
                self.assertEqual(CryptoProjectFiles(root / "projects").verify_ref(project.id, reference, "input").read_bytes(), raw)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(["--server-url", address, "upload-input",
                        str(project.id), str(source), "--role", "source"]), 0)
                self.assertEqual(len(store.get_project(project.id).crypto["inputs"]), 2)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
