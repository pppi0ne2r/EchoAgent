"""BUG-030: 题面要求 flag 为明文哈希时，answer 必须被独立验证为哈希而非明文。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from echo_agent.crypto_contract import HASH_ALGORITHMS, crypto_goal
from echo_agent.crypto_verify import VERSION, verify_candidate
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.models import Project, ProjectStatus
from tests.test_crypto_schemes import manifest_project

PLAINTEXT = "wowyoucanreallydance"
MD5 = hashlib.md5(PLAINTEXT.encode()).hexdigest()
# 验证器只验证 c = m^e 且与明文一致；这里只关心答案形态，唯一性用 solution_policy=any 放开。
_N, _E = 3233, 17
N, E, C = _N, _E, pow(0x41, _E, _N)
GOAL = {"answer_form": "hash", "answer_hash": "md5", "solution_policy": "any"}

def rsa_manifest() -> dict:
    return {"algorithm": "rsa", "n": N, "e": E, "c": C}


class HashAnswerFormContractTests(unittest.TestCase):
    def test_goal_defaults_to_literal_without_touching_the_stored_goal(self) -> None:
        # answer_form 不能写进默认值：否则旧项目的 goal_sha256 会变化，历史 pass/回执失效
        self.assertNotIn("answer_form", crypto_goal({}))
        self.assertEqual(crypto_goal({}).get("answer_form", "literal"), "literal")
        self.assertEqual(crypto_goal({"answer_form": "hash", "answer_hash": "md5"})["answer_hash"], "md5")

    def test_goal_rejects_inconsistent_hash_form(self) -> None:
        for goal in (
            {"answer_form": "hash"},
            {"answer_form": "hash", "answer_hash": "crc32"},
            {"answer_form": "hash", "answer_hash": "md5", "answer_kind": "factors"},
            {"answer_form": "hash", "answer_hash": "md5", "answer_encoding": "hex"},
            {"answer_hash": "md5"},
            {"answer_form": "digest"},
        ):
            with self.subTest(goal=goal), self.assertRaises(ValueError):
                crypto_goal(goal)

    def test_hash_form_keeps_plaintext_byte_conversion(self) -> None:
        goal = crypto_goal({"answer_form": "hash", "answer_hash": "md5",
                            "byte_length": 2, "byte_order": "big"})
        self.assertEqual(goal["byte_length"], 2)
        self.assertEqual(goal["answer_encoding"], "utf8")

    def test_hash_algorithms_cover_contract(self) -> None:
        self.assertIn("md5", HASH_ALGORITHMS)


class HashAnswerFormVerificationTests(unittest.TestCase):
    def test_rsa_hash_answer_passes_and_bare_plaintext_fails(self) -> None:
        digest = hashlib.md5(b"A").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), rsa_manifest(), digest,
                candidate={"answer_kind": "plaintext", "m": "65"},
                goal=GOAL)
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["answer_relation"], "hash")
            self.assertEqual(record["checks"]["preimage_hex"], "41")
            self.assertEqual(record["checks"]["answer_hash_expected"], digest)

            # BUG-030 的真实形态：answer 放明文、哈希放自定义字段 → 必须失败
            candidate.crypto["candidate"] = {"answer_kind": "plaintext", "answer": "A",
                                             "m": "65", "md5": digest}
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "fail", record)
            self.assertFalse(record["checks"]["answer_hash_matches"])

    def test_rsa_hash_answer_must_match_the_recovered_plaintext(self) -> None:
        # 另一个明文的哈希不能通过：验证器从 n/e/c 独立恢复明文，再计算哈希
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), rsa_manifest(), MD5,
                candidate={"m": "65"}, goal=GOAL)
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_rsa_hash_wrong_algorithm_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), rsa_manifest(), hashlib.md5(b"A").hexdigest(),
                candidate={"m": "65"}, goal={**GOAL, "answer_hash": "sha256"})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_literal_goal_still_accepts_plaintext_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), rsa_manifest(), "A", candidate={"m": "65"})
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "pass")

    def test_hash_form_can_pin_the_plaintext_byte_length(self) -> None:
        # 明文有前导零字节时，byte_length 是哈希前像的一部分，必须允许
        digest = hashlib.md5(b"\x00A").hexdigest()
        crypto_goal({"answer_form": "hash", "answer_hash": "md5", "byte_length": 2})
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), rsa_manifest(), digest,
                candidate={"m": "65"}, goal={**GOAL, "byte_length": 2})
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["preimage_hex"], "0041")

    def test_hash_scheme_with_hash_form_is_rejected(self) -> None:
        # hash/XOR/encoding 的 answer 本身就是 preimage，再套一层哈希与题面矛盾
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {"algorithm": "hash", "hash": "sha256",
                                  "digest": hashlib.sha256(b"x").hexdigest()}, "x",
                goal=GOAL)
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive", record)
            self.assertIn("exact preimage", record["checks"]["reason"])

    def test_verification_version_bumped(self) -> None:
        self.assertEqual(VERSION, "crypto-verify-v8")


class DsaHashAnswerFormTests(unittest.TestCase):
    SOURCE = ("p = 607\nq = 101\ng = 7\ny = powmod(g, x, p)\n"
              "'''\ny = 597\n'''\n")

    def test_dsa_private_key_hash_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = manifest_project(
                Path(directory), {}, hashlib.md5(b"A").hexdigest(),
                candidate={"m": "65"}, goal=GOAL)
            path = files.resolve_ref(7, project.crypto["inputs"][0], "input")
            path.write_bytes(self.SOURCE.encode())
            project.crypto["inputs"][0] = files.describe_input(
                7, "input-1", "challenge", "inputs/manifest.json")
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["answer_relation"], "hash")
            self.assertEqual(record["checks"]["preimage_hex"], "41")

            candidate.crypto["candidate"]["answer"] = "A"
            self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")


class HashAnswerFormPromptTests(unittest.TestCase):
    def test_crypto_prompts_state_answer_form_rule(self) -> None:
        project = Project(id=7, name="md5", status=ProjectStatus.ACTIVE, task_type="crypto",
                          objective="flag is md5 of plaintext",
                          crypto={"schema_version": 1, "inputs": [], "goal": {
                              "answer_form": "hash", "answer_hash": "md5"}})
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始", from_fact_id=0)
        builder = PromptBuilder()
        prompts = (
            builder.build_first_run_prompt(project, intent),
            builder.build_explore_prompt(project, intent),
            builder.build_reason_prompt(project),
        )
        for prompt in prompts:
            self.assertIn("answer_form=hash", prompt)
            self.assertIn("crypto.candidate.answer 必须写成该哈希", prompt)
            self.assertIn('"answer_hash": "md5"', prompt)
        self.assertIn("preimage_hex", prompts[2])


if __name__ == "__main__":
    unittest.main()
