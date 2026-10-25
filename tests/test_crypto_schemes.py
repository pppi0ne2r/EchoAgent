from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_contract import crypto_goal
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_verify import VERSION, _EC_CURVES, _ec_mul, answer_is_verified, verify_candidate
from echo_agent.models import FactKind, Project, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore
from tests.test_crypto_s09 import project_in


class Runtime:
    def __init__(self):
        self.transitions = []

    def prepare_shared_resources(self):
        pass

    def apply_status_transition(self, project, old_status, log=None):
        self.transitions.append((old_status, project.status))


def manifest_project(root: Path, manifest: dict, answer: str, *, candidate: dict | None = None,
                     goal: dict | None = None):
    files = CryptoProjectFiles(root / "projects")
    inputs, _ = files.ensure_layout(7)
    (inputs / "manifest.json").write_text(json.dumps(manifest))
    ref = files.describe_input(7, "input-1", "challenge", "inputs/manifest.json")
    crypto = {"schema_version": 1, "inputs": [ref]}
    if goal is not None:
        crypto["goal"] = goal
    project = Project(id=7, name="check", status=ProjectStatus.ACTIVE, task_type="crypto",
                      objective="verify", crypto=crypto)
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    payload = {"answer": answer, **(candidate or {})}
    fact = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate", "source_input_ids": ["input-1"], "candidate": payload})
    return project, files, fact


class DsaSchemeTests(unittest.TestCase):
    SOURCE = ("p = 607\nq = 101\ng = 7\ny = powmod(g, x, p)\n"
              "'''\ny = 597\n'''\n")

    def test_dsa_source_candidate_passes_and_wrong_x_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, _ = manifest_project(Path(directory), {}, "A")
            # Replace the manifest input with the DSA source bytes.
            path = files.resolve_ref(7, project.crypto["inputs"][0], "input")
            path.write_bytes(self.SOURCE.encode())
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "challenge", "inputs/manifest.json")
            candidate = project.facts[2]
            candidate.crypto["candidate"]["m"] = "65"
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["scheme"], "dsa")
            self.assertTrue(record["checks"]["public_key_matches"])
            self.assertEqual(record["checks"]["answer_relation"], "plaintext")

            candidate.crypto["candidate"]["m"] = "66"
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_dsa_json_manifest_and_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {"algorithm": "dsa", "p": 607, "q": 101, "g": 7, "y": 597}, "A",
                candidate={"m": "65"})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            candidate.crypto["candidate"].pop("m")
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("private key", record["checks"]["reason"])


class EcdsaSchemeTests(unittest.TestCase):
    def test_secp256k1_public_key_check(self) -> None:
        curve = _EC_CURVES["secp256k1"]
        private = 65
        point = _ec_mul(private, (curve["gx"], curve["gy"]), curve["p"], curve["a"])
        manifest = {"algorithm": "ecdsa", "curve": "secp256k1",
                    "qx": str(point[0]), "qy": str(point[1])}
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), manifest, "A", candidate={"m": str(private)})
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertTrue(record["checks"]["public_key_matches"])
            candidate.crypto["candidate"]["m"] = str(private + 1)
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")


class HashXorEncodingTests(unittest.TestCase):
    def test_hash_scheme(self) -> None:
        digest = hashlib.sha256(b"hello").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {"algorithm": "hash", "hash": "sha256", "digest": digest}, "hello")
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
            candidate.crypto["candidate"]["answer"] = "hell0"
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_xor_known_and_implicit_key(self) -> None:
        ciphertext = bytes(value ^ ord("k") for value in b"hello")
        for manifest in (
            {"algorithm": "xor", "ciphertext": ciphertext.hex(), "key": "6b"},
            {"algorithm": "xor", "ciphertext": ciphertext.hex()},
        ):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                project, files, candidate = manifest_project(Path(directory), manifest, "hello")
                self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")
                candidate.crypto["candidate"]["answer"] = "hell0"
                self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_encoding_schemes(self) -> None:
        cases = [
            ("base64", "aGVsbG8="),
            ("hex", "68656c6c6f"),
            ("url", "a%20b"),
        ]
        for encoding, data in cases:
            answer = "a b" if encoding == "url" else "hello"
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                project, files, candidate = manifest_project(
                    Path(directory), {"algorithm": "encoding", "encoding": encoding, "data": data}, answer)
                self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")

    def test_unsupported_scheme_is_reported_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {"algorithm": "ecc", "data": "x"}, "x")
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("unsupported verification scheme: ecc", record["checks"]["reason"])


class CompletionModeTests(unittest.TestCase):
    def test_goal_validation_and_evidence_mode(self) -> None:
        self.assertEqual(crypto_goal({"verification_mode": "evidence"})["verification_mode"], "evidence")
        with self.assertRaises(ValueError):
            crypto_goal({"verification_mode": "anything"})
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {"algorithm": "hash", "hash": "sha256",
                                  "digest": hashlib.sha256(b"x").hexdigest()}, "x",
                goal={"verification_mode": "evidence"})
            self.assertFalse(answer_is_verified(project, "evidence-answer", lambda fact_id: None))
            project.add_fact("answer is evidence-answer", kind=FactKind.EVIDENCE,
                             crypto={"claim_status": "observed"})
            self.assertTrue(answer_is_verified(project, "evidence-answer", lambda fact_id: None))

    def test_acceptance_receipt_and_put_cannot_forge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, files, _ = project_in(root)
            candidate = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"scheme": "rsa", "answer": "7", "m": "7"}})
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store)
            with TestClient(app) as http:
                response = http.post("/projects/7/acceptances", json={
                    "candidate_fact_id": candidate.id, "operator": "tester", "note": "platform accepted"})
                self.assertEqual(response.status_code, 201, response.text)
                accepted = response.json()
                self.assertEqual(accepted["answer"], "7")
                loaded = store.get_project(7)
                self.assertTrue(answer_is_verified(loaded, "7", lambda fact_id: None))

                payload = loaded.to_dict()
                payload["crypto"]["acceptances"] = [{**accepted, "answer": "forged",
                                                     "candidate_sha256": "0" * 64}]
                response = http.put("/projects/7", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                reloaded = store.get_project(7)
                self.assertEqual(reloaded.crypto["acceptances"][0]["answer"], "7")
                self.assertFalse(answer_is_verified(reloaded, "forged", lambda fact_id: None))

    def test_receipt_is_invalid_after_candidate_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, _, _ = project_in(root)
            candidate = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
                "claim_status": "candidate", "source_input_ids": ["input-1"],
                "candidate": {"scheme": "rsa", "answer": "7", "m": "7"}})
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store)
            with TestClient(app) as http:
                self.assertEqual(http.post("/projects/7/acceptances", json={
                    "candidate_fact_id": candidate.id, "operator": "tester"}).status_code, 201)
                loaded = store.get_project(7)
                loaded.facts[candidate.id].crypto["candidate"]["answer"] = "8"
                loaded.facts[candidate.id].crypto["candidate"]["m"] = "8"
                store.save_project(loaded)
                self.assertFalse(answer_is_verified(store.get_project(7), "7", lambda fact_id: None))


if __name__ == "__main__":
    unittest.main()
