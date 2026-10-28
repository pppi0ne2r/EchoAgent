from __future__ import annotations

import unittest

from echo_agent.dispatcher.agents import answer_source_context
from echo_agent.dispatcher.objectives import required_finding_count
from echo_agent.dispatcher.parsing import (
    ReasonFormatError,
    extract_mode_json_output,
    extract_mode_json_outputs,
    parse_reason_result,
    parse_worker_result,
    repair_reason_output,
)


class DispatcherParsingTests(unittest.TestCase):
    def test_reason_output_uses_last_json_object_when_codex_prints_extra_data(self) -> None:
        output = "\n".join(
            [
                "codex",
                '{"completed": false, "next_intent_descriptions": ["old"]}',
                '{"completed": false, "next_intent_descriptions": ["new"]}',
                "tokens used",
                "3,037",
            ]
        )

        result = parse_reason_result(output)

        self.assertFalse(result.completed)
        self.assertEqual(result.next_intent_descriptions, ["new"])

    def test_worker_output_uses_last_json_object_after_progress_lines(self) -> None:
        output = "\n".join(
            [
                "progress: running",
                '{"answer": null, "fact_description": "first"}',
                "tokens used",
                '{"answer": "done", "fact_description": "final"}',
            ]
        )

        result = parse_worker_result(output)

        self.assertEqual(result.answer, "done")
        self.assertEqual(result.fact_description, "final")

    def test_worker_output_ignores_placeholder_fact_description(self) -> None:
        result = parse_worker_result('{"answer": null, "fact_description": "初始执行得到的新事实"}')

        self.assertIsNone(result.answer)
        self.assertIsNone(result.fact_description)

    def test_worker_output_ignores_prompt_template_fact_description(self) -> None:
        result = parse_worker_result(
            '{"answer": null, "fact_description": "写入你实际验证得到的具体事实；不要照抄本句"}'
        )

        self.assertIsNone(result.answer)
        self.assertIsNone(result.fact_description)

    def test_worker_output_ignores_placeholder_answer(self) -> None:
        result = parse_worker_result('{"answer": "真实 flag 字符串", "fact_description": null}')

        self.assertIsNone(result.answer)
        self.assertIsNone(result.fact_description)

    def test_reason_output_keeps_accepted_fact_ids(self) -> None:
        result = parse_reason_result(
            '{"completed": true, "answer": "已确认3个漏洞", "accepted_fact_ids": [6, 6, 13], "next_intents": []}'
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.accepted_fact_ids, (6, 13))

    def test_reason_output_rejects_non_integer_fact_ids(self) -> None:
        with self.assertRaises(ValueError):
            parse_reason_result(
                '{"completed": true, "answer": "已确认3个漏洞", "accepted_fact_ids": ["6"], "next_intents": []}'
            )

    def test_reason_output_accepts_structured_intents(self) -> None:
        result = parse_reason_result(
            '{"completed": false, "next_intents": ['
            '{"from": 2, "to": null, "description": "枚举目录"},'
            '{"from": 2, "to": 1, "description": "尝试利用上传点"}'
            "]}"
        )

        self.assertEqual(result.next_intent_descriptions, ["枚举目录", "尝试利用上传点"])
        self.assertEqual(
            result.next_intents,
            [
                {"from": 2, "to": None, "description": "枚举目录", "action_key": ""},
                {"from": 2, "to": 1, "description": "尝试利用上传点", "action_key": ""},
            ],
        )


    def test_reason_output_reads_action_key_and_stop_reason(self) -> None:
        result = parse_reason_result(
            '{"completed": false, "stop_reason": "没有新方向", "next_intents": ['
            '{"from": 2, "to": null, "action_key": " Check_Upload ", "description": "枚举"}]}'
        )

        self.assertEqual(result.stop_reason, "没有新方向")
        self.assertEqual(result.next_intents[0]["action_key"], "Check_Upload")

    def test_reason_output_rejects_non_string_action_key(self) -> None:
        with self.assertRaises(ValueError):
            parse_reason_result(
                '{"completed": false, "next_intents": [{"description": "枚举", "action_key": 1}]}'
            )

    def test_reason_output_keeps_root_object_when_prefix_contains_nested_intents(self) -> None:
        output = """progress\n{\"completed\": false, \"answer\": null, \"next_intents\": [{\"from\": 2, \"to\": null, \"description\": \"分析上传\"}, {\"from\": 2, \"to\": null, \"description\": \"枚举目录\"}]}\n"""
        result = parse_reason_result(output)
        self.assertFalse(result.completed)
        self.assertEqual(result.next_intent_descriptions, ["分析上传", "枚举目录"])

    def test_reason_output_rejects_string_completed(self) -> None:
        with self.assertRaises(ValueError):
            parse_reason_result('{"completed": "false", "next_intents": [{"description": "枚举"}]}')

    def test_reason_output_rejects_non_integer_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            parse_reason_result('{"completed": false, "next_intents": [{"from": 2, "to": "goal", "description": "枚举"}]}')

    def test_worker_output_ignores_trailing_codex_event(self) -> None:
        import json
        message = json.dumps({"answer": None, "fact_description": "real fact"}, ensure_ascii=False)
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
        output = "\n".join([
            json.dumps(event, ensure_ascii=False),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}),
        ])
        result = parse_worker_result(output)
        self.assertEqual(result.fact_description, "real fact")
        self.assertIsNone(result.answer)

    def test_worker_output_reads_refuted_outcome(self) -> None:
        result = parse_worker_result('{"answer": null, "fact_description": "上传点不可利用", "outcome": "refuted"}')
        self.assertEqual(result.outcome, "refuted")
        self.assertEqual(result.fact_description, "上传点不可利用")


    def test_worker_output_without_json_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_worker_result("progress only, no json object")

    def test_extract_mode_json_unwraps_event_text_message_and_content(self) -> None:
        import json

        text_event = {
            "type": "item.completed",
            "item": {"text": json.dumps({"answer": None, "fact_description": "from text"}, ensure_ascii=False)},
        }
        message_event = {"message": json.dumps({"answer": None, "fact_description": "from message"}, ensure_ascii=False)}
        content_event = {
            "type": "response.output_text.done",
            "content": json.dumps(
                {"completed": False, "next_intents": [{"description": "枚举目录"}]},
                ensure_ascii=False,
            ),
        }
        explore_output = "\n".join([
            json.dumps(text_event, ensure_ascii=False),
            json.dumps(message_event, ensure_ascii=False),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        ])
        extracted = extract_mode_json_output(explore_output, "explore")
        self.assertIsNotNone(extracted)
        self.assertIn("from message", extracted or "")
        self.assertNotIn("from text", extracted or "")

        reason_output = "\n".join([
            json.dumps(content_event, ensure_ascii=False),
            json.dumps({"type": "turn.completed"}),
        ])
        extracted_reason = extract_mode_json_output(reason_output, "reason")
        self.assertIsNotNone(extracted_reason)
        self.assertIn('"completed": false', (extracted_reason or "").lower())
        self.assertIn("枚举目录", extracted_reason or "")

    def test_extract_mode_json_prefers_later_bare_json_over_earlier_event(self) -> None:
        import json

        event = {
            "item": {"text": json.dumps({"answer": None, "fact_description": "old"}, ensure_ascii=False)},
        }
        output = "\n".join([
            json.dumps(event, ensure_ascii=False),
            json.dumps({"answer": None, "fact_description": "new"}, ensure_ascii=False),
        ])
        extracted = extract_mode_json_output(output, "explore")
        self.assertIn("new", extracted or "")
        self.assertNotIn("old", extracted or "")



    def test_worker_output_keeps_earlier_facts_before_the_last_result(self) -> None:
        import json

        first = {"answer": None, "fact_description": "font cache rce", "outcome": "verified"}
        second = {"answer": None, "fact_description": "upload missing", "outcome": "refuted"}
        output = "\n".join([
            json.dumps(first, ensure_ascii=False),
            json.dumps(second, ensure_ascii=False),
        ])
        slices = extract_mode_json_outputs(output, "explore")
        self.assertEqual(len(slices), 2)
        result = parse_worker_result(output)
        self.assertEqual(result.fact_description, "upload missing")
        self.assertEqual(result.outcome, "refuted")
        self.assertEqual(len(result.prior_results), 1)
        self.assertEqual(result.prior_results[0].fact_description, "font cache rce")
        self.assertEqual(result.prior_results[0].outcome, "verified")

    def test_worker_output_ignores_refuted_prompt_template(self) -> None:
        result = parse_worker_result(
            '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}'
        )

        self.assertIsNone(result.fact_description)
        self.assertIsNone(result.answer)
        self.assertIsNone(result.outcome)
        self.assertEqual(result.prior_results, [])

    def test_worker_output_keeps_real_json_beside_prompt_template(self) -> None:
        import json

        template = {
            "answer": None,
            "fact_description": "该方向已验证不存在可利用点",
            "outcome": "refuted",
        }
        real = {"answer": None, "fact_description": "内网 MySQL 5.7.29:3306", "outcome": "verified"}
        output = "\n".join(
            [
                json.dumps(template, ensure_ascii=False),
                json.dumps(real, ensure_ascii=False),
                json.dumps(template, ensure_ascii=False),
            ]
        )

        result = parse_worker_result(output)

        self.assertEqual(result.fact_description, "内网 MySQL 5.7.29:3306")
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(result.prior_results, [])


    def test_reason_output_drops_exact_prompt_template_intents(self) -> None:
        import json

        template = {
            "completed": False,
            "next_intents": [
                {
                    "from": 2,
                    "to": None,
                    "action_key": "check-upload",
                    "description": "分析是否存在文件上传漏洞",
                },
                {
                    "from": 2,
                    "to": None,
                    "action_key": "enum-backup",
                    "description": "枚举 Web 目录和敏感备份文件",
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "prompt template intents"):
            parse_reason_result(json.dumps(template, ensure_ascii=False))

        mixed = {
            "completed": False,
            "next_intents": template["next_intents"]
            + [
                {
                    "from": 2,
                    "to": None,
                    "action_key": "check-upload",
                    "description": "只请求 /upload 一次并在十秒内停止",
                }
            ],
        }
        result = parse_reason_result(json.dumps(mixed, ensure_ascii=False))
        self.assertEqual(len(result.next_intents or []), 1)
        self.assertEqual(result.next_intents[0]["description"], "只请求 /upload 一次并在十秒内停止")
        self.assertEqual(result.next_intents[0]["action_key"], "check-upload")

        completed = dict(template)
        completed["completed"] = True
        completed["answer"] = "flag{ok}"
        result = parse_reason_result(json.dumps(completed, ensure_ascii=False))
        self.assertTrue(result.completed)
        self.assertEqual(result.next_intents, [])

        empty = {"completed": False, "next_intents": []}
        result = parse_reason_result(json.dumps(empty, ensure_ascii=False))
        self.assertEqual(result.next_intents, [])

    def test_reason_output_without_json_is_a_format_error(self) -> None:
        with self.assertRaises(ReasonFormatError):
            parse_reason_result("我先分析一下，暂时不给 JSON")

    def test_reason_template_match_ignores_key_case_and_extra_space(self) -> None:
        import json

        payload = {
            "completed": False,
            "next_intents": [
                {
                    "from": 2,
                    "to": None,
                    "action_key": "CHECK_UPLOAD",
                    "description": "  分析是否存在文件上传漏洞  ",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "prompt template intents"):
            parse_reason_result(json.dumps(payload, ensure_ascii=False))




    def test_repair_reason_output_accepts_fenced_trailing_comma(self) -> None:
        raw = """思考过程
```json
{"completed": false, "next_intents": [{"description": "枚举",}]}
```
"""
        repaired = repair_reason_output(raw)
        self.assertIsNotNone(repaired)
        result = parse_reason_result(repaired or "")
        self.assertEqual(result.next_intents[0]["description"], "枚举")

    def test_repair_reason_output_rejects_plain_prose(self) -> None:
        self.assertIsNone(repair_reason_output("这里只有推理，没有 JSON"))

    def test_answer_source_ignores_result_json_only(self) -> None:
        raw = '{"answer": "flag{onlyjson}", "fact_description": null, "outcome": "verified"}'
        self.assertIsNone(answer_source_context(raw, "flag{onlyjson}"))

    def test_answer_source_uses_text_outside_result_json(self) -> None:
        raw = 'HTTP/1.1 200\nbody flag{real}\n{"answer": "flag{real}", "fact_description": null, "outcome": "verified"}'
        context = answer_source_context(raw, "flag{real}")
        self.assertIsNotNone(context)
        self.assertIn("flag{real}", context or "")
        self.assertNotIn("fact_description", context or "")


if __name__ == "__main__":
    unittest.main()


class FindingObjectiveTests(unittest.TestCase):
    def test_only_vulnerability_objectives_require_multiple_facts(self) -> None:
        self.assertEqual(required_finding_count("寻找至少3个漏洞。"), 3)
        self.assertEqual(required_finding_count("find at least 2 vulnerabilities"), 2)
        self.assertEqual(required_finding_count("get flag"), 1)
        self.assertEqual(required_finding_count("寻找漏洞"), 1)
        self.assertEqual(required_finding_count("至少3次重试"), 1)
