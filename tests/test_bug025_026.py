from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_inputs import extract_rsa
from echo_agent.crypto_verify import verify_candidate
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, ReasonResult, WorkerResult
from echo_agent.models import FactKind, IntentOutcome, IntentStatus, Project, ProjectStatus
from tests.test_crypto_s09 import Client, UnusedWorker


PLAINTEXT = b"flag{abc_123}"
P = 2**61 - 1
Q = 2**107 - 1


def challenge_raw(*, e: int = 65537, p: int = P, q: int = Q, plaintext: bytes = PLAINTEXT) -> tuple[bytes, int, int]:
    m = int.from_bytes(plaintext, "big")
    c = pow(m, e, p * q)
    raw = f"e:{e}\n\np:{p}\n\nq:{q}\n\nc = pow(flag,e,p*q) \n#{c}\n".encode()
    return raw, m, c


def challenge_project(root: Path, *, e: int = 65537, p: int = P, q: int = Q,
                      plaintext: bytes = PLAINTEXT, goal: dict | None = None):
    raw, m, c = challenge_raw(e=e, p=p, q=q, plaintext=plaintext)
    files = CryptoProjectFiles(root / "projects")
    inputs, _ = files.ensure_layout(7)
    (inputs / "rsa.txt").write_bytes(raw)
    ref = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
    crypto = {"schema_version": 1, "inputs": [ref]}
    if goal is not None:
        crypto["goal"] = goal
    project = Project(id=7, name="rsa", status=ProjectStatus.ACTIVE, task_type="crypto",
                      objective="recover flag", crypto=crypto)
    project.add_fact("start", fact_id=0)
    project.add_fact("recover flag", fact_id=1)
    return project, files, m, c, raw


def add_candidate(project: Project, answer: str, m: int | None = None):
    candidate = {"scheme": "rsa", "answer": answer}
    if m is not None:
        candidate["m"] = str(m)
    return project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate", "source_input_ids": ["input-1"], "candidate": candidate})


class AnswerReasoner:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, answer=self.answer, next_intents=[])


class Bug025CandidateContractTests(unittest.TestCase):
    def test_observation_kind_candidate_is_normalized_and_answer_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, m, _, _ = challenge_project(Path(directory))
            intent = project.add_intent("solve", from_fact_id=0, crypto={
                "method": "rsa.decrypt", "input_ids": ["input-1"], "scope": {"input_ids": ["input-1"]}})
            intent.mark_running()
            result = WorkerResult(answer="abc_123", fact_description="flag inner string", outcome="verified",
                                  crypto={"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                                          "fact": {"claim_status": "candidate", "observation_kind": "candidate"},
                                          "candidate": {"scheme": "rsa", "answer": "abc_123", "m": str(m)}})
            Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

            self.assertEqual(intent.status, IntentStatus.COMPLETED)
            self.assertNotEqual(intent.outcome, IntentOutcome.ERROR)
            candidate = project.facts[2]
            self.assertEqual(candidate.kind, FactKind.CANDIDATE)
            self.assertEqual(candidate.crypto["observation_kind"], "evidence")
            self.assertEqual(candidate.crypto["candidate"]["answer"], "abc_123")
            self.assertEqual(Project.from_dict(project.to_dict()).facts[2].kind, FactKind.CANDIDATE)

    def test_prompts_and_worker_guidance_constrain_observation_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, _, _, _ = challenge_project(Path(directory))
            intent = project.add_intent("inspect", from_fact_id=0, crypto={
                "method": "inspect.inputs", "input_ids": ["input-1"]})
            builder = PromptBuilder()
            prompts = (builder.build_first_run_prompt(project, intent),
                       builder.build_explore_prompt(project, intent),
                       builder.build_reason_prompt(project))
            for prompt in prompts:
                self.assertIn("observation_kind 只允许 evidence 或 refuted", prompt)
                self.assertIn("禁止写 observation_kind=candidate", prompt)
        agents = Path(__file__).resolve().parent.parent / "container" / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        self.assertIn("observation_kind=evidence", text)
        self.assertIn("只允许 evidence/refuted", text)


class Bug026ExtractionTests(unittest.TestCase):
    def test_symbolic_pow_challenge_file_is_extracted(self) -> None:
        raw, m, c = challenge_raw()
        canonical, sources = extract_rsa([("input-1", raw)], {"n", "e", "c"})
        values = dict(line.split("=", 1) for line in canonical.decode().splitlines())
        self.assertEqual(values, {"n": str(P * Q), "e": "65537", "c": str(c),
                                  "p": str(P), "q": str(Q)})
        formats = {entry["parameter"]: entry["format"] for entry in sources}
        self.assertEqual(formats["n"], "derived-pq")
        self.assertEqual(formats["c"], "symbolic-pow")

    def test_annotation_parameters_are_literals(self) -> None:
        raw, _, c = challenge_raw()
        canonical, _ = extract_rsa([("input-1", raw)], {"n", "e", "c"})
        self.assertIn(b"e=65537\n", canonical)
        self.assertIn(f"p={P}\n".encode(), canonical)
        self.assertIn(f"q={Q}\n".encode(), canonical)
        self.assertIn(f"c={c}\n".encode(), canonical)

    def test_symbolic_ciphertext_requires_exactly_one_decimal_comment(self) -> None:
        raw, _, _ = challenge_raw()
        without = b"\n".join(line for line in raw.splitlines() if not line.startswith(b"#"))
        with self.assertRaisesRegex(ValueError, "exactly one decimal-only comment"):
            extract_rsa([("input-1", without)], {"n", "e", "c"})
        doubled = raw + b"#5\n"
        with self.assertRaisesRegex(ValueError, "exactly one decimal-only comment"):
            extract_rsa([("input-1", doubled)], {"n", "e", "c"})

    def test_symbolic_relation_rejects_unsupported_expressions(self) -> None:
        cases = [
            (b"c = pow(flag,e,p**2)\n", "supports only"),
            (b"c = pow(flag[0],e,p*q)\n", "unknown plaintext name"),
            (b"c = pow(eval('1'),e,p*q)\n", "unknown plaintext name"),
            (b"c = pow(flag,e,__import__('os').system('x'))\n", "supports only"),
            (b"c = pow(flag,e,p*q,1)\n", "may only assign"),
        ]
        for relation, message in cases:
            raw = b"e:17\np:1009\nq:1013\n" + relation + b"#13\n"
            with self.subTest(relation=relation), self.assertRaisesRegex(ValueError, message):
                extract_rsa([("input-1", raw)], {"n", "e", "c"})

    def test_dynamic_and_unsupported_assignments_are_still_rejected(self) -> None:
        for raw, message in [
            (b"n=5*11\ne=3\nc=13\n", "may only assign"),
            (b"globals()['c']=14\n", "parameter-only"),
            (b"exec('c=14')\n", "parameter-only"),
            (b"from other import c\n", "parameter-only"),
        ]:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, message):
                extract_rsa([("input-1", raw)], {"n", "e", "c"})


class Bug026VerificationTests(unittest.TestCase):
    def test_input_factors_verify_full_plaintext_and_inner_substring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, m, _, _ = challenge_project(Path(directory))
            full = add_candidate(project, PLAINTEXT.decode(), m)
            record = verify_candidate(project, full.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["factors_source"], "input")
            self.assertTrue(record["checks"]["unique_root"])
            self.assertEqual(record["checks"]["answer_relation"], "plaintext")

            inner = add_candidate(project, "abc_123", m)
            record = verify_candidate(project, inner.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["checks"]["answer_relation"], "substring")
            self.assertEqual(record["checks"]["answer_offset"], 5)

    def test_wrong_or_ungrounded_answers_do_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, m, _, _ = challenge_project(Path(directory))
            wrong = add_candidate(project, "not-in-plaintext", m)
            self.assertEqual(verify_candidate(project, wrong.id, files)["status"], "fail")
            no_m = add_candidate(project, PLAINTEXT.decode())
            record = verify_candidate(project, no_m.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("decimal m", record["checks"]["reason"])

    def test_multi_root_requires_solution_policy_any(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, m, _, _ = challenge_project(Path(directory), e=3)
            candidate = add_candidate(project, PLAINTEXT.decode(), m)
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertFalse(record["checks"]["unique_root"])
            self.assertIn("unique", record["checks"]["reason"])

            project.crypto["goal"] = {"solution_policy": "any"}
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertFalse(record["checks"]["unique_root"])

    def test_dispatcher_completes_symbolic_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, m, _, _ = challenge_project(Path(directory))
            initial = project.add_intent("read input", from_fact_id=0, to_fact_id=1, crypto={
                "method": "inspect.inputs", "input_ids": ["input-1"]})
            initial.mark_running()
            initial.mark_completed()
            client = Client(project, files)
            dispatcher = Dispatcher(client=client, worker=UnusedWorker(), reasoner=AnswerReasoner(str(m)))

            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.REASON)
            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.EXPLORE)
            self.assertEqual(client.project.crypto["verifications"][-1]["status"], "pass")
            self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.COMPLETED)
            self.assertEqual(client.project.status, ProjectStatus.COMPLETED)
            self.assertEqual(client.project.facts[1].description, f"result\nanswer: {m}")


if __name__ == "__main__":
    unittest.main()
