from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_verify import VERSION, verify_candidate
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, ReasonResult, WorkerResult
from echo_agent.models import FactKind, Project, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore


RAW = b"n=55\ne=3\nc=13\n"


class Client:
    def __init__(self, project: Project, files: CryptoProjectFiles) -> None:
        self.project, self.files = copy.deepcopy(project), files

    def get_project(self, project_id: int) -> Project:
        return copy.deepcopy(self.project)

    def save_project(self, project: Project) -> Project:
        self.project = copy.deepcopy(project)
        return copy.deepcopy(project)

    def verify_crypto_candidate(self, project_id: int, candidate_fact_id: int) -> dict:
        return verify_candidate(self.project, candidate_fact_id, self.files)

    def add_project_log(self, *args, **kwargs) -> None:
        pass


class Reasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, answer="7", next_intents=[])


class UnusedWorker:
    def execute_intent(self, project, intent, mode=None) -> WorkerResult:
        raise AssertionError("verification must not call the solver worker")


def project_in(root: Path, raw: bytes = RAW) -> tuple[Project, CryptoProjectFiles, Path]:
    files = CryptoProjectFiles(root / "projects")
    inputs, _ = files.ensure_layout(7)
    path = inputs / "rsa.txt"
    path.write_bytes(raw)
    ref = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
    project = Project(id=7, name="rsa", status=ProjectStatus.ACTIVE, task_type="crypto",
                      objective="recover plaintext", crypto={"schema_version": 1, "inputs": [ref]})
    project.add_fact("start", fact_id=0)
    project.add_fact("recover plaintext", fact_id=1)
    initial = project.add_intent("read input", from_fact_id=0, to_fact_id=1,
                                 crypto={"method": "inspect.inputs", "input_ids": ["input-1"]})
    initial.mark_running()
    initial.mark_completed()
    return project, files, path


class CryptoS09Tests(unittest.TestCase):
    def test_candidate_worker_output_is_distinct_from_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            intent = project.add_intent("solve", from_fact_id=0, crypto={
                "method": "rsa.decrypt", "input_ids": ["input-1"], "scope": {"input_ids": ["input-1"]}})
            intent.mark_running()
            result = WorkerResult(answer="7", fact_description="candidate m=7", outcome="verified",
                                  crypto={"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                                          "fact": {"claim_status": "candidate"},
                                          "candidate": {"scheme": "rsa", "answer": "7", "m": "7", "p": "5", "q": "11", "d": "27"}})
            Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
            candidate = project.facts[2]
            self.assertEqual(candidate.kind, FactKind.CANDIDATE)
            self.assertFalse(candidate.is_plannable)
            self.assertEqual(candidate.crypto["candidate"]["p"], "5")
            self.assertEqual(len([fact for fact in project.facts.values() if fact.kind == FactKind.EVIDENCE]), 0)
            self.assertEqual(Project.from_dict(project.to_dict()).facts[2].kind, FactKind.CANDIDATE)

    def test_rsa_verification_checks_math_and_multiple_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, _ = project_in(Path(directory))
            candidate = project.add_fact("m=7", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"scheme": "rsa", "answer": "7", "m": "7", "p": "5", "q": "11", "d": "27"}})
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass")
            self.assertTrue(record["checks"]["factor_product_matches"])
            self.assertTrue(record["checks"]["private_exponent_matches"])
            self.assertEqual(record["checks"]["candidate_set"], ["7"])
            self.assertEqual(record["input_refs"][0]["sha256"], hashlib.sha256(RAW).hexdigest())
            candidate.crypto["candidate"]["p"] = "3"
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")
            candidate.crypto["candidate"] = {"scheme": "rsa", "answer": "8", "m": "8"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")
            candidate.crypto["candidate"] = {"scheme": "rsa", "answer_kind": "factors",
                                             "answer": "5,11", "p": "5", "q": "11"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")
            project.crypto["goal"] = {"answer_kind": "factors"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            candidate.crypto["candidate"] = {"scheme": "rsa", "answer_kind": "private_exponent",
                                             "answer": "27", "p": "5", "q": "11", "d": "27"}
            project.crypto["goal"] = {"answer_kind": "private_exponent"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            project.crypto.pop("goal")
            candidate.crypto["candidate"] = {"scheme": "rsa", "answer": "7", "m": "7"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            # Mathematical roots do not complete a unique-plaintext goal.
            multi_raw = b"n=15\ne=2\nc=4\n"
            input_path = files.resolve_ref(7, project.crypto["inputs"][0], "input")
            input_path.write_bytes(multi_raw)
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
            multiple = verify_candidate(project, candidate.id, files)
            self.assertEqual(multiple["status"], "inconclusive")
            candidate.crypto["candidate"] = {"scheme": "rsa", "answer": "2", "m": "2"}
            multiple = verify_candidate(project, candidate.id, files)
            self.assertEqual(multiple["status"], "inconclusive")
            self.assertGreater(len(multiple["checks"]["candidate_set"]), 1)
            self.assertFalse(multiple["checks"]["unique_root"])
            project.crypto["goal"] = {"solution_policy": "any"}
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")

    def test_dispatcher_requires_current_pass_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, path = project_in(Path(directory))
            client = Client(project, files)
            dispatcher = Dispatcher(client=client, worker=UnusedWorker(), reasoner=Reasoner())
            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.REASON)
            self.assertEqual(client.project.facts[2].kind, FactKind.CANDIDATE)
            self.assertEqual(client.project.intents[1].crypto["method"], "verify.candidate")
            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.EXPLORE)
            self.assertEqual(client.project.crypto["verifications"][-1]["status"], "pass")
            self.assertEqual(Project.from_dict(client.project.to_dict()).crypto["verifications"][-1]["status"], "pass")
            client.project.crypto["require_platform_acceptance"] = True
            self.assertFalse(dispatcher._crypto_answer_verified(client.project, "7"))
            client.project.crypto["require_platform_acceptance"] = False
            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.COMPLETED)
            self.assertEqual(client.project.status, ProjectStatus.COMPLETED)
            self.assertEqual(client.project.facts[1].description, "result\nanswer: 7")

            # A changed candidate or changed original bytes invalidates the old pass.
            client.project.status = ProjectStatus.ACTIVE
            client.project.facts[2].crypto["candidate"]["m"] = "8"
            self.assertFalse(dispatcher._crypto_answer_verified(client.project, "7"))
            client.project.facts[2].crypto["candidate"].pop("m")
            path.write_bytes(b"n=55\ne=3\nc=14\n")
            self.assertFalse(dispatcher._crypto_answer_verified(client.project, "7"))

    def test_artifact_hash_change_invalidates_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, _ = project_in(Path(directory))
            artifact_dir = files.project_root(7) / "artifacts" / "intents" / "0"
            artifact_dir.mkdir(parents=True)
            artifact_path = artifact_dir / "solver.py"
            artifact_path.write_bytes(b"print(7)\n")
            artifact = files.describe_artifact(7, 0, "artifacts/intents/0/solver.py")
            candidate = project.add_fact("m=7", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "producer_intent_id": 0, "artifact_refs": [artifact],
                "candidate": {"answer": "7", "scheme": "rsa", "m": "7"}})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            artifact_path.write_bytes(b"print(8)\n")
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "inconclusive")

    def test_missing_constraints_and_aes_metadata_do_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, path = project_in(Path(directory), b"n=99999999999999999999\ne=3\nc=343\n")
            candidate = project.add_fact("guess", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"scheme": "rsa", "answer": "7", "m": "7"}})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "inconclusive")
            manifest = {"algorithm": "aes", "mode": "cbc", "iv_hex": "00" * 16,
                        "ciphertext_hex": "00" * 16, "padding": "none"}
            path.write_bytes(json.dumps(manifest).encode())
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
            candidate.crypto["candidate"] = {"scheme": "aes", "answer": "flag", "key_hex": "00" * 16}
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("known_plaintext_sha256", record["checks"]["reason"])

    @unittest.skipUnless(importlib.util.find_spec("Crypto") or importlib.util.find_spec("cryptography"),
                         "optional crypto verifier is not installed")
    def test_aes_gcm_authentication_and_known_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, path = project_in(Path(directory))
            key, nonce, plaintext, aad = bytes(range(16)), bytes(range(12)), b"flag{known}", b"challenge"
            if importlib.util.find_spec("Crypto"):
                from Crypto.Cipher import AES
                encryptor = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
                encryptor.update(aad)
                encrypted_body, tag = encryptor.encrypt_and_digest(plaintext)
            else:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                encrypted = AESGCM(key).encrypt(nonce, plaintext, aad)
                encrypted_body, tag = encrypted[:-16], encrypted[-16:]
            manifest = {"algorithm": "aes", "mode": "gcm", "nonce_hex": nonce.hex(),
                        "ciphertext_hex": encrypted_body.hex(), "tag_hex": tag.hex(),
                        "aad_hex": aad.hex(), "known_plaintext_sha256": hashlib.sha256(plaintext).hexdigest()}
            path.write_bytes(json.dumps(manifest).encode())
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
            candidate = project.add_fact("AES answer", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"scheme": "aes", "answer": "flag{known}", "key_hex": key.hex()}})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            manifest["tag_hex"] = "00" * 16
            path.write_bytes(json.dumps(manifest).encode())
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_api_verification_reads_saved_file_and_candidate(self) -> None:
        class RuntimeStub:
            def prepare_shared_resources(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project, files, _ = project_in(Path(directory))
            candidate = project.add_fact("m=7", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"answer": "7", "scheme": "rsa", "m": "7"}})
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
                app = create_app(store)
            with TestClient(app) as http:
                response = http.post(f"/projects/7/candidates/{candidate.id}/verify")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "pass")
                self.assertEqual(response.json()["method"], VERSION)


if __name__ == "__main__":
    unittest.main()
