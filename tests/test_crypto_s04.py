from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from echo_agent.config import ProjectResource
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.dispatcher.client import ProjectClient
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import IntentExecutionResult, WorkerObservation, WorkerResult
from echo_agent.models import Project
from echo_agent.server.app import create_app
from echo_agent.server.dispatcher_runner import StoreDispatcherClient
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.storage import SQLiteProjectStore


class CryptoS04Tests(unittest.TestCase):
    def test_legacy_project_migrates_without_changing_general_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE projects (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                        docker TEXT, target_url TEXT, hint TEXT NOT NULL DEFAULT '',
                        objective TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE facts (
                        project_id INTEGER NOT NULL, id INTEGER NOT NULL,
                        description TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'evidence',
                        PRIMARY KEY (project_id, id)
                    );
                    CREATE TABLE intents (
                        project_id INTEGER NOT NULL, id INTEGER NOT NULL,
                        from_fact_id INTEGER, to_fact_id INTEGER, description TEXT NOT NULL,
                        status TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'pending',
                        note TEXT NOT NULL DEFAULT '', action_key TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (project_id, id),
                        FOREIGN KEY (project_id, from_fact_id) REFERENCES facts(project_id, id),
                        FOREIGN KEY (project_id, to_fact_id) REFERENCES facts(project_id, id)
                    );
                    INSERT INTO projects VALUES (1, 'web', 'inactive', 'web-1', 'https://example.test', '', 'flag');
                    INSERT INTO facts VALUES (1, 0, 'start', 'start');
                    INSERT INTO facts VALUES (1, 1, 'goal', 'goal');
                    INSERT INTO intents VALUES (1, 0, 0, 1, 'probe', 'active', 'pending', '', '');
                """)
            store = SQLiteProjectStore(database)
            project = store.get_project(1)
            self.assertEqual(project.task_type, "general")
            self.assertNotIn("crypto", project.to_dict())
            self.assertNotIn("task_type", project.to_dict())
            store.save_project(project)
            self.assertEqual(store.get_project(1).intents[0].to_fact_id, 1)
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_crypto_metadata_and_multiple_fact_sources_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            files = CryptoProjectFiles(Path(directory) / "projects")
            project = store.create_project(name="rsa", task_type="crypto")
            inputs, intents = files.ensure_layout(project.id)
            (inputs / "rsa.txt").write_bytes(b"n=55\ne=3\nc=13\n")
            ref = files.describe_input(project.id, "input-1", "parameters", "inputs/rsa.txt")
            project.crypto["inputs"].append(ref)
            fact = project.add_fact("n=55, e=3, c=13", crypto={"source_input_ids": ["input-1"]})
            intent = project.add_intent(
                "trial division", from_fact_id=fact.id,
                crypto={"input_fact_ids": [fact.id], "input_ids": ["input-1"],
                        "method": "factor.trial", "params": {"n": {"type": "integer", "radix": 10, "value": "55"}}},
            )
            work = intents / str(intent.id)
            work.mkdir()
            (work / "result.txt").write_text("5 * 11 = 55", encoding="utf-8")
            artifact = files.describe_artifact(
                project.id, intent.id, f"artifacts/intents/{intent.id}/result.txt"
            )
            for description in ("factor 5", "factor 11"):
                project.add_fact(
                    description,
                    crypto={"producer_intent_id": intent.id,
                            "depends_on_fact_ids": [fact.id], "source_input_ids": ["input-1"],
                            "artifact_refs": [artifact]},
                )
            project.crypto["executions"] = [{"intent_id": intent.id, "exit_reason": "completed",
                                              "artifact_refs": [artifact]}]
            store.save_project(project)
            project.activate()
            project.start()
            project.complete()
            store.save_project(project)
            loaded = SQLiteProjectStore(Path(directory) / "state.db").get_project(project.id)
            self.assertEqual(loaded.to_dict(), project.to_dict())
            self.assertEqual(
                [loaded.facts[index].crypto["producer_intent_id"] for index in (3, 4)],
                [intent.id, intent.id],
            )
            self.assertEqual(files.verify_ref(project.id, ref, "input"), inputs / "rsa.txt")
            self.assertEqual(files.verify_ref(project.id, artifact, "artifact"), work / "result.txt")

    def test_crypto_mounts_are_separate_and_general_mounts_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerProjectRuntime(
                image="worker:test", context=Path("container"), dockerfile=Path("container/Dockerfile"),
                project_data_root=Path(directory) / "projects",
                resource_root=Path(directory) / "resources",
                resources=(ProjectResource(
                    name="tools", path=Path("tools"), container_path="/home/kali/tools",
                    read_only=False,
                ),),
            )
            general = Project(id=1, name="web")
            self.assertEqual([mount[1:] for mount in runtime._prepare_project_resources(general)], [
                ("/home/kali/tools", False),
            ])
            for project_id in (2, 3):
                project = Project(id=project_id, name="rsa", task_type="crypto")
                mounts = runtime._prepare_project_resources(project)
                self.assertEqual([mount[1:] for mount in mounts], [
                    ("/home/kali/tools", False),
                    ("/home/kali/workspace/inputs", True),
                    ("/home/kali/workspace/intents", False),
                ])
                self.assertIn(f"{project_id}", str(mounts[1][0]))
            self.assertNotEqual(
                runtime._prepare_project_resources(Project(id=2, name="rsa", task_type="crypto"))[1][0],
                runtime._prepare_project_resources(Project(id=3, name="rsa", task_type="crypto"))[1][0],
            )

    def test_references_reject_escape_and_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = CryptoProjectFiles(Path(directory) / "projects")
            inputs, _ = files.ensure_layout(1)
            (inputs / "a.txt").write_bytes(b"original")
            ref = files.describe_input(1, "in-1", "ciphertext", "inputs/a.txt")
            (inputs / "a.txt").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                files.verify_ref(1, ref, "input")
            for path in ("inputs/../outside", "C:/outside", "inputs\\outside", "/outside"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    files.describe_input(1, "in-1", "ciphertext", path)
            with self.assertRaises(ValueError):
                Project(id=2, name="bad", task_type="crypto", crypto={"schema_version": 99})

    def test_http_and_store_clients_preserve_crypto_fields(self) -> None:
        from fastapi.testclient import TestClient

        class RuntimeStub:
            def prepare_shared_resources(self) -> None:
                pass

            def apply_status_transition(self, project, previous_status, log=None) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            runtime = RuntimeStub()
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
                app = create_app(store)
            with TestClient(app) as http:
                api = ProjectClient("http://unused.test")

                def request(method, path, payload=None):
                    response = http.request(method, path, json=payload)
                    response.raise_for_status()
                    return response.json()

                api._request = request
                project = api.create_project(name="crypto", task_type="crypto")
                project.crypto["default_budget"] = {"seconds": 10}
                api.save_project(project)
                fact = api.add_fact(project.id, "input seen", crypto={"typed_value": {
                    "type": "bytes", "encoding": "hex", "value": "0007", "length": 2,
                }})
                intent = api.add_intent(project.id, "inspect", from_fact_id=fact["id"], crypto={
                    "input_fact_ids": [fact["id"]], "method": "inspect.bytes",
                })
                loaded = api.get_project(project.id)
                direct_client = StoreDispatcherClient(store, runtime=runtime)
                direct = direct_client.get_project(project.id)
                self.assertEqual(loaded.to_dict(), direct.to_dict())
                self.assertEqual(loaded.crypto["default_budget"], {"seconds": 10})
                self.assertEqual(loaded.facts[fact["id"]].crypto["typed_value"]["value"], "0007")
                self.assertEqual(loaded.intents[intent["id"]].crypto["method"], "inspect.bytes")
                unsafe_save = http.put(f"/projects/{project.id}", json={
                    "name": "crypto", "status": "inactive",
                    "facts": [item.to_dict() for item in loaded.facts.values()],
                    "intents": [item.to_dict() for item in loaded.intents.values()],
                })
                self.assertEqual(unsafe_save.status_code, 400)
                self.assertEqual(api.get_project(project.id).crypto, loaded.crypto)
                direct.crypto["default_budget"] = {"seconds": 20}
                direct_client.save_project(direct)
                self.assertEqual(api.get_project(project.id).crypto["default_budget"], {"seconds": 20})

                web = api.create_project(name="web", target_url="https://example.test")
                self.assertNotIn("crypto", web.to_dict())
                self.assertEqual(api.get_project(web.id).target_url, "https://example.test")

    def test_dispatcher_marks_all_crypto_observations_with_producer(self) -> None:
        project = Project(id=1, name="crypto", task_type="crypto")
        source = project.add_fact("input")
        intent = project.add_intent(
            "calculate", from_fact_id=source.id,
            crypto={"input_fact_ids": [source.id], "method": "factor.trial"},
        )
        intent.mark_running()
        result = WorkerResult(
            fact_description="second fact",
            prior_results=[WorkerObservation(fact_description="first fact")],
        )
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
        self.assertEqual([project.facts[index].crypto["producer_intent_id"] for index in (1, 2)], [intent.id] * 2)
        self.assertEqual(project.facts[1].crypto["depends_on_fact_ids"], [source.id])

        general = Project(id=2, name="web")
        web_source = general.add_fact("start")
        web_intent = general.add_intent("probe", from_fact_id=web_source.id)
        web_intent.mark_running()
        Dispatcher._apply_intent_result(general, IntentExecutionResult(web_intent, result))
        self.assertNotIn("crypto", general.facts[1].to_dict())


if __name__ == "__main__":
    unittest.main()
