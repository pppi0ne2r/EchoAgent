"""BUG-031: RSA+AES composite verification from trusted original inputs."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_inputs import extract_rsa_aes, looks_like_rsa_aes
from echo_agent.crypto_verify import (VERSION, _check_goal, _verify_rsa_aes,
                                      verify_candidate)
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.models import FactKind, Project, ProjectStatus

M = int.from_bytes(b"0123456789abcdef", "big")
N = M + 1
C = pow(M, 3, N)
IV = bytes(range(16))
CIPHERTEXT = bytes(range(16, 32))
PROGRAM = "from Crypto.Cipher import AES\nimport os\n\ne = 3\niv = os.urandom(16)\n"
OUTPUT = f"N = {N}\nc = {C}\niv = {IV!r}\nciphertext = {CIPHERTEXT.hex()}\n"


def _project(root: Path):
    files = CryptoProjectFiles(root / "projects")
    inputs_dir, _ = files.ensure_layout(7)
    (inputs_dir / "task.py").write_bytes(PROGRAM.encode())
    (inputs_dir / "output.txt").write_bytes(OUTPUT.encode())
    program_ref = files.describe_input(7, "input-1", "source", "inputs/task.py")
    output_ref = files.describe_input(7, "input-2", "challenge", "inputs/output.txt")
    project = Project(id=7, name="composite", status=ProjectStatus.ACTIVE, task_type="crypto",
                      objective="composite", crypto={"schema_version": 1,
                                                     "inputs": [program_ref, output_ref]})
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    fact = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate",
        "source_input_ids": [output_ref["id"]],
        "candidate": {"answer": "flag{test}", "scheme": "rsa", "m": str(M)},
    })
    return project, files, fact, program_ref, output_ref


class RsaAesParsingTests(unittest.TestCase):
    def test_parses_constants_split_across_original_inputs(self) -> None:
        values, sources = extract_rsa_aes([
            ("input-1", PROGRAM.encode()),
            ("input-2", OUTPUT.encode()),
        ])
        self.assertEqual(values["N"], N)
        self.assertEqual(values["c"], C)
        self.assertEqual(values["e"], 3)
        self.assertEqual(values["iv"], IV)
        self.assertEqual(values["ciphertext"], CIPHERTEXT)
        self.assertTrue(any(item["parameter"] == "e" and item["input_id"] == "input-1"
                            for item in sources))

    def test_detection_requires_the_aes_ciphertext(self) -> None:
        self.assertTrue(looks_like_rsa_aes([("input-2", OUTPUT.encode())]))
        self.assertFalse(looks_like_rsa_aes([("input-1", PROGRAM.encode())]))

    def test_missing_parameter_is_reported(self) -> None:
        with self.assertRaises(ValueError) as raised:
            extract_rsa_aes([("input-2", b"N = 55\nc = 13\n")])
        self.assertIn("missing", str(raised.exception))


class RsaAesVerificationTests(unittest.TestCase):
    def test_composite_candidate_passes(self) -> None:
        inputs = [("input-1", PROGRAM.encode()), ("input-2", OUTPUT.encode())]
        with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=b"flag{test}"), \
                patch("echo_agent.crypto_verify._aes_cbc_encrypt", return_value=CIPHERTEXT):
            status, checks = _verify_rsa_aes(inputs, {"answer": "flag{test}", "m": str(M)})
        self.assertEqual(status, "pass", checks)
        self.assertTrue(checks["rsa_ciphertext_matches"])
        self.assertTrue(checks["aes_roundtrip"])
        self.assertTrue(checks["answer_matches"])
        self.assertEqual(checks["key_hex"], b"0123456789abcdef".hex())

    def test_wrong_rsa_plaintext_and_wrong_answer_fail(self) -> None:
        inputs = [("input-1", PROGRAM.encode()), ("input-2", OUTPUT.encode())]
        with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=b"flag{test}"), \
                patch("echo_agent.crypto_verify._aes_cbc_encrypt", return_value=CIPHERTEXT):
            status, _ = _verify_rsa_aes(inputs, {"answer": "flag{test}", "m": str(M + 2)})
            self.assertEqual(status, "fail")
            status, _ = _verify_rsa_aes(inputs, {"answer": "flag{wrong}", "m": str(M)})
            self.assertEqual(status, "fail")

    def test_roundtrip_mismatch_fails(self) -> None:
        inputs = [("input-1", PROGRAM.encode()), ("input-2", OUTPUT.encode())]
        with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=b"flag{test}"), \
                patch("echo_agent.crypto_verify._aes_cbc_encrypt", return_value=b"\x00" * 16):
            status, checks = _verify_rsa_aes(inputs, {"answer": "flag{test}", "m": str(M)})
        self.assertEqual(status, "fail")
        self.assertFalse(checks["aes_roundtrip"])

    def test_aes_library_absence_is_inconclusive(self) -> None:
        inputs = [("input-1", PROGRAM.encode()), ("input-2", OUTPUT.encode())]
        with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=None):
            status, checks = _verify_rsa_aes(inputs, {"answer": "flag{test}", "m": str(M)})
        self.assertEqual(status, "inconclusive")
        self.assertEqual(checks["reason"], "optional AES verifier package is unavailable")

    def test_goal_does_not_require_unique_root_for_composite(self) -> None:
        status, reason = _check_goal(
            {"answer_kind": "plaintext", "solution_policy": "unique"},
            "rsa-aes",
            {"answer_kind": "plaintext"},
            {"answer_matches": True},
        )
        self.assertEqual((status, reason), ("pass", ""))

    def test_version_and_prompt_cover_the_composite_scheme(self) -> None:
        self.assertEqual(VERSION, "crypto-verify-v8")
        project = Project(id=7, name="c", status=ProjectStatus.ACTIVE, task_type="crypto",
                          objective="composite", crypto={"schema_version": 1, "inputs": []})
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始", from_fact_id=0)
        prompt = PromptBuilder().build_first_run_prompt(project, intent)
        self.assertIn("rsa-aes", prompt)
        self.assertIn("复合题", prompt)


class RsaAesCandidateTests(unittest.TestCase):
    def test_verify_candidate_upgrades_rsa_and_reads_the_program_constant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, fact, program_ref, output_ref = _project(Path(directory))
            with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=b"flag{test}"), \
                    patch("echo_agent.crypto_verify._aes_cbc_encrypt", return_value=CIPHERTEXT):
                record = verify_candidate(project, fact.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["scheme"], "rsa-aes")
            self.assertEqual({ref["id"] for ref in record["input_refs"]},
                             {program_ref["id"], output_ref["id"]})
            self.assertEqual(record["backend"], "host")

    def test_verify_candidate_reports_missing_aes_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, fact, _, _ = _project(Path(directory))
            with patch("echo_agent.crypto_verify._aes_cbc_decrypt", return_value=None):
                record = verify_candidate(project, fact.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("AES verifier package", record["checks"]["reason"])


if __name__ == "__main__":
    unittest.main()
