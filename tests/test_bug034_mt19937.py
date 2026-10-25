"""BUG-034: deterministic MT19937 replay and shuffle inversion."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_mt19937 import extract_mt19937, looks_like_mt19937, replay_mt19937
from echo_agent.crypto_verify import VERSION, _check_goal, verify_candidate
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.models import FactKind, Project, ProjectStatus

ROUNDS = 3
SHUFFLE_ROUNDS = 2
FLAG = b"flag{abc}(noise)"


def _fixture():
    rng = random.Random(12345)
    state = rng.getstate()
    words = list(state[1][:624])
    index = state[1][624]
    gift = b""
    for round_index in range(ROUNDS):
        first = rng.getrandbits(8)
        second = rng.getrandbits(16)
        value = (pow(first, 2 * round_index, 257) & 0xFF) ^ second
        gift += value.to_bytes(2, "big")
    chars = list(FLAG)
    for _ in range(SHUFFLE_ROUNDS):
        rng.shuffle(chars)
    cipher = "".join(chr(item) for item in chars)
    task = (
        "from random import *\n"
        "gift=b''\n"
        f"for i in range({ROUNDS}):\n"
        "    r1 = getrandbits(8)\n"
        "    r2 = getrandbits(16)\n"
        "    x=(pow(r1, 2*i, 257) & 0xff) ^ r2\n"
        "    c=long_to_bytes(x, 2)\n"
        "    gift+=c\n"
        "m=list(flag)\n"
        f"for i in range({SHUFFLE_ROUNDS}):\n"
        "    shuffle(m)\n"
        'c = "".join(list(map(chr,m)))\n'
    )
    output = f"gift = {int.from_bytes(gift, 'big')}\nc = {cipher}\n"
    return words, index, task, output


def _project(root: Path, task: str, output: str, answer: str, words, index):
    files = CryptoProjectFiles(root / "projects")
    inputs_dir, _ = files.ensure_layout(7)
    (inputs_dir / "task.py").write_bytes(task.encode())
    (inputs_dir / "output.txt").write_bytes(output.encode())
    task_ref = files.describe_input(7, "input-1", "source", "inputs/task.py")
    output_ref = files.describe_input(7, "input-2", "input", "inputs/output.txt")
    project = Project(id=7, name="mt", status=ProjectStatus.ACTIVE, task_type="crypto",
                      objective="predict", crypto={"schema_version": 1,
                                                   "inputs": [task_ref, output_ref]})
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    fact = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate",
        "source_input_ids": [output_ref["id"]],
        "candidate": {"answer": answer, "scheme": "mt19937",
                      "state": {"words": words, "index": index}},
    })
    return project, files, fact


class MtExtractionTests(unittest.TestCase):
    def test_extracts_params_and_values(self) -> None:
        words, index, task, output = _fixture()
        data = extract_mt19937([("task", task.encode()), ("output", output.encode())])
        self.assertEqual(data["params"], {
            "rounds": ROUNDS, "first_bits": 8, "second_bits": 16,
            "modulus": 257, "value_bytes": 2, "shuffle_rounds": SHUFFLE_ROUNDS,
        })
        self.assertEqual(len(data["cipher"]), len(FLAG))

    def test_detection(self) -> None:
        words, index, task, output = _fixture()
        self.assertTrue(looks_like_mt19937([("task", task.encode())]))
        self.assertTrue(looks_like_mt19937([("output", output.encode())]))


class MtReplayTests(unittest.TestCase):
    def test_replay_and_invert(self) -> None:
        words, index, task, output = _fixture()
        data = extract_mt19937([("task", task.encode()), ("output", output.encode())])
        plaintext, checks = replay_mt19937(words, index, data["params"], data["cipher"], data["gift"])
        self.assertTrue(checks["gift_matches"])
        self.assertEqual(plaintext, FLAG)


class MtCandidateTests(unittest.TestCase):
    def test_flag_substring_answer_passes(self) -> None:
        words, index, task, output = _fixture()
        with tempfile.TemporaryDirectory() as directory:
            project, files, fact = _project(Path(directory), task, output, "flag{abc}", words, index)
            record = verify_candidate(project, fact.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["scheme"], "mt19937")
            self.assertEqual(record["checks"]["answer_relation"], "substring")
            self.assertEqual(record["checks"]["plaintext_hex"], FLAG.hex())

    def test_full_plaintext_answer_passes(self) -> None:
        words, index, task, output = _fixture()
        with tempfile.TemporaryDirectory() as directory:
            project, files, fact = _project(
                Path(directory), task, output, FLAG.decode(), words, index)
            record = verify_candidate(project, fact.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["answer_relation"], "plaintext")

    def test_wrong_state_or_answer_fails(self) -> None:
        words, index, task, output = _fixture()
        with tempfile.TemporaryDirectory() as directory:
            project, files, fact = _project(Path(directory), task, output, "flag{abc}", words, index)
            fact.crypto["candidate"]["state"]["words"] = [0] * 624
            record = verify_candidate(project, fact.id, files)
            self.assertEqual(record["status"], "fail", record)
            self.assertFalse(record["checks"]["gift_matches"])

            fact.crypto["candidate"]["state"]["words"] = words
            fact.crypto["candidate"]["answer"] = "flag{wrong}"
            self.assertEqual(verify_candidate(project, fact.id, files)["status"], "fail")

    def test_goal_does_not_require_unique_root(self) -> None:
        status, reason = _check_goal(
            {"answer_kind": "plaintext", "solution_policy": "unique"},
            "mt19937",
            {"answer_kind": "plaintext"},
            {"answer_matches": True},
        )
        self.assertEqual((status, reason), ("pass", ""))

    def test_version_and_prompt(self) -> None:
        self.assertEqual(VERSION, "crypto-verify-v8")
        project = Project(id=7, name="mt", status=ProjectStatus.ACTIVE, task_type="crypto",
                          objective="predict", crypto={"schema_version": 1, "inputs": []})
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始", from_fact_id=0)
        prompt = PromptBuilder().build_first_run_prompt(project, intent)
        self.assertIn("mt19937", prompt)
        self.assertIn("crypto.candidate.state", prompt)


if __name__ == "__main__":
    unittest.main()
