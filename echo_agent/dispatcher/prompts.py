from __future__ import annotations

import json

from echo_agent.dispatcher.parsing import TEMPLATE_REASON_INTENTS
from echo_agent.models import FactKind, Intent, IntentOutcome, IntentStatus, Project

# reason prompt 的上下文预算：最近 N 条 evidence 全量，更早的只保留摘要。
_EVIDENCE_PROMPT_LIMIT = 40
_EVIDENCE_OLDER_CHARS = 160

_EXAMPLE_ACTION_KEY, _EXAMPLE_DESCRIPTION = TEMPLATE_REASON_INTENTS[0]
_REASON_INTENT_EXAMPLE = json.dumps(
    {
        "completed": False,
        "answer": None,
        "stop_reason": "",
        "next_intents": [
            {"from": 2, "to": None, "action_key": action_key, "description": description}
            for action_key, description in TEMPLATE_REASON_INTENTS
        ],
    },
    ensure_ascii=False,
)
_WORKER_RESULT_RULES = (
    "未拿到 flag 时，每确认一个独立的新事实，或确认某条路径已经关闭，就输出一条 JSON，不要继续扩大扫描。",
    "未拿到 flag 时不要在结束时再输出重复的总结。",
    "已关闭或失败的路径必须使用 outcome=refuted，不要把否定结论写成 verified。",
    "拿到 flag 时，本次执行只能输出一条 JSON，不要在它之前或之后再输出其他 JSON。",
)


class PromptBuilder:
    def build_first_run_prompt(self, project: Project, intent: Intent) -> str:
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的执行 agent，当前模式是 first_run。",
                "这是项目第一次执行。你的任务是先尝试直接解决题目并得到 flag。",
                "如果能够得到 flag，本次执行只输出一条 JSON：answer 写完整 flag，fact_description 写利用路径和关键证据，正文必须包含与 answer 相同的 flag，outcome 为 verified。",
                "如果不能得到 flag，每个独立事实各输出一条 JSON，answer 必须为 null。可以输出多条。新事实使用 outcome=verified，已关闭方向使用 outcome=refuted。",
                "不要规划后续 intent。后续 intent 由 reason 模式在下一轮循环中规划。",
                "你运行在该项目对应的 Kali Docker 容器中，可以使用容器内工具完成快速验证、扫描、利用和取证。",
                "不要长时间等待无输出命令；如果某个命令持续运行或输出为空，应立即停止等待，并把当前可确认的信息写入 fact_description。",
                "除非已经明确拿到 flag，否则不要因为单个命令卡住而继续等待。",
                *_WORKER_RESULT_RULES,
                "输出内容只能是 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                '{"answer": "实际获得的完整 flag；不要照抄本句", "fact_description": "写入你实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "写入你实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}',
                "",
                _format_worker_context(project),
                "",
                "初始 intent：",
                json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
            ]
        )

    def build_explore_prompt(self, project: Project, intent: Intent) -> str:
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的执行 agent，当前模式是 explore。",
                "你的任务是只执行给定 intent，不要规划新的 intent。",
                "你运行在该项目对应的 Kali Docker 容器中，可以使用容器内工具完成验证、扫描、利用和取证。",
                "如果直接得到 flag，本次执行只输出一条 JSON：answer 写完整 flag，fact_description 写利用路径和关键证据，正文必须包含与 answer 相同的 flag，outcome 为 verified。不要再输出其他 JSON。",
                "如果不能得到 flag，每个独立事实各输出一条 JSON，answer 必须为 null。可以输出多条。不要输出额外总结。",
                "不要长时间等待无输出命令；如果某个命令持续运行或输出为空，应立即停止等待，并把当前可确认的信息写入 fact_description。",
                "除非已经明确拿到 flag，否则不要因为单个命令卡住而继续等待。",
                *_WORKER_RESULT_RULES,
                "输出内容只能是 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                '{"answer": "实际获得的完整 flag；不要照抄本句", "fact_description": "写入你本次 intent 实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "写入你本次 intent 实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}',
                "",
                _format_worker_context(project),
                "",
                "当前要执行的 intent：",
                json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
            ]
        )

    def build_reason_prompt(self, project: Project) -> str:
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的规划 agent，当前模式是 reason。",
                "你的任务是根据 evidence 判断项目是否已经完成；如果未完成，只从 allowed_from 给出下一批 intent。",
                "不要执行命令，不要进行扫描或利用，只做推理和规划。",
                "最终答案的形式不固定，不要假设一定是 flag{...}。你需要根据项目事实图判断是否已经得到题目要求的最终答案。",
                "只有当 evidence 中已经包含可以提交的最终答案时，才返回 completed=true，并将 answer 写为该最终答案。",
                f"生成的 intent description 必须描述接下来需要探索的方向或验证动作，例如“{_EXAMPLE_DESCRIPTION}”，不要写成笼统的“继续推进”。",
                "生成的 intent 应该具体、可执行，并且能由后续 explore 模式在 Kali Docker 容器中完成。",
                "不是每条 evidence 都需要生成 intent；只选择对达成目标有价值、能继续验证或利用的 evidence 生成 intent。",
                "每个新 intent 的 from 必须取 allowed_from 里的 fact id。evidence 为空时 allowed_from 只有起点，通常是 0。",
                "不能使用 findings、do_not_repeat、already_done、证伪事实、错误事实或 goal 作为 from。",
                "to 可以为 null，系统会先临时连接到目标终点 fact。",
                f"每条 intent 必须带稳定的 action_key，使用小写英文和连字符，例如 {_EXAMPLE_ACTION_KEY}。",
                "already_done 中 outcome 为 refuted 或 verified 的动作不能再规划，即使更换 action_key 或换一种说法也不行。",
                "outcome 为 error 或 empty 时，不要重复原来的 description；只有描述明显更小的新步骤才可以规划。",
                "error、empty 的 note，以及“未完成执行的输出摘录”，都不是证伪结论，只能改写成更小的新步骤。",
                "findings 里的证伪正文只用于判断，不能作为 from，也不能换一种说法重做。",
                "允许同一个 evidence 产生多个 intent，但 action_key 不能重复。",
                "只有答案已经出现在 verified 执行记录中时，才返回 completed=true。",
                "agent reported possible final answer、未完成执行的输出摘录、error 和 refuted 都不能单独作为完成依据。",
                "若 evidence 带 source: 行，那是去掉结果 JSON 后的原始输出片段，只能用来核验答案是否真实出现过。",
                "如果没有新的可执行方向，返回 completed=false、next_intents=[]，并用 stop_reason 说明原因。不要为了凑数重复旧动作。",
                "示例里的 action_key 和 description 只说明格式，不要原样输出。",
                "最终只输出一个 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                '{"completed": true, "answer": "实际可提交的最终答案", "stop_reason": "", "next_intents": []}',
                '{"completed": false, "answer": null, "stop_reason": "上传和备份面已经验证关闭", "next_intents": []}',
                _REASON_INTENT_EXAMPLE,
                "",
                _format_project_context(project),
            ]
        )


def _format_worker_context(project: Project) -> str:
    closed_actions = [
        {
            "action_key": intent.action_key,
            "description": intent.description,
            "outcome": intent.outcome.value,
        }
        for intent in _executed_intents(project)
    ]
    payload = {
        "id": project.id,
        "name": project.name,
        "target_url": project.target_url,
        "hint": project.hint,
        "objective": project.objective,
        "goal": project.facts[1].description if 1 in project.facts else "",
        "evidence": _evidence_items(project),
        "closed_actions": closed_actions,
    }
    return "\n".join(
        [
            "执行上下文只包含任务信息、已验证证据和已经关闭的动作。",
            "target_url 可以同时包含 URL、IP 和容器内附件路径，不要假定它一定是网页地址。",
            "closed_actions 只给出 action_key、description、outcome，不包含证伪事实编号。",
            "不要重做 closed_actions 里的动作，也不要根据缺失的事实编号继续扩展。",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def _format_project_context(project: Project) -> str:
    evidence = _evidence_items(project)
    executed = _executed_intents(project)
    already_done = []
    for intent in executed:
        result_text, result_kind = _intent_result(project, intent)
        already_done.append(
            {
                "id": intent.id,
                "from": intent.from_fact_id,
                "action_key": intent.action_key,
                "description": intent.description,
                "outcome": intent.outcome.value,
                "note": intent.note,
                "result": result_text,
                "result_kind": result_kind,
            }
        )
    blocked = [
        item
        for item in already_done
        if item["outcome"] in {IntentOutcome.REFUTED.value, IntentOutcome.ERROR.value, IntentOutcome.EMPTY.value}
    ]
    findings = [
        {"kind": fact.kind.value, "description": fact.description}
        for fact in sorted(project.facts.values(), key=lambda item: item.id)
        if fact.kind in {FactKind.REFUTED, FactKind.ERROR}
    ]
    evidence_ids = [item["id"] for item in evidence]
    if evidence_ids:
        allowed_from = evidence_ids
    elif 0 in project.facts:
        allowed_from = [0]
    else:
        allowed_from = []
    payload = {
        "id": project.id,
        "name": project.name,
        "target_url": project.target_url,
        "hint": project.hint,
        "objective": project.objective,
        "goal": project.facts[1].description if 1 in project.facts else "",
        "evidence": evidence,
        "allowed_from": allowed_from,
        "findings": findings,
        "already_done": already_done,
        "do_not_repeat": blocked,
    }
    return "\n".join(
        [
            "项目上下文分为已知证据、证伪结论、已经做过的动作、不要重复的失败路径、终点说明。",
            "target_url 可以同时包含 URL、IP 和容器内附件路径，不要假定它一定是网页地址。",
            "evidence 才是已验证事实。新 intent 的 from 只能取 allowed_from。",
            "evidence 为空时，allowed_from 只包含起点，避免没有证据时无法继续规划。",
            "findings 是证伪或错误事实的正文。不能作为 from，也不能换一种说法重做。",
            "already_done.result 是该动作实际得到的结论。",
            "refuted 和 verified 不能更换 action_key 或换一种说法再规划。",
            "error 和 empty 不能原样重复 description；只有更小的新步骤才可以继续。",
            "未完成执行的输出摘录只是超时前的片段，必须改成更小的步骤，不能当成证伪。",
            "do_not_repeat 不能作为新的探索起点。",
            "goal 只说明目标，不是证据。",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def _evidence_items(project: Project) -> list[dict[str, object]]:
    facts = [
        fact
        for fact in sorted(project.facts.values(), key=lambda item: item.id)
        if fact.kind == FactKind.EVIDENCE
    ]
    # 上下文预算：较早的 evidence 只保留摘要，避免 prompt 随运行无限膨胀。
    keep_from = max(0, len(facts) - _EVIDENCE_PROMPT_LIMIT)
    items: list[dict[str, object]] = []
    for index, fact in enumerate(facts):
        description = fact.description
        if index < keep_from and len(description) > _EVIDENCE_OLDER_CHARS:
            description = description[: _EVIDENCE_OLDER_CHARS] + "…（较早事实已截断）"
        items.append({"id": fact.id, "description": description})
    return items


def _executed_intents(project: Project) -> list[Intent]:
    return [
        intent
        for intent in sorted(project.intents.values(), key=lambda item: item.id)
        if intent.status in {IntentStatus.COMPLETED, IntentStatus.FAILED}
    ]


def _intent_result(project: Project, intent: Intent) -> tuple[str, str]:
    fact_ids = [fact_id for fact_id in intent.to_fact_ids if fact_id in project.facts]
    if not fact_ids and intent.to_fact_id in project.facts:
        fact_ids = [intent.to_fact_id]
    selected = [project.facts[fact_id] for fact_id in fact_ids if project.facts[fact_id].kind != FactKind.GOAL]
    if not selected and fact_ids:
        fact = project.facts[fact_ids[-1]]
        return fact.description, fact.kind.value
    if not selected:
        return "", ""
    primary = project.facts.get(intent.to_fact_id)
    kind = primary.kind.value if primary is not None and primary in selected else selected[-1].kind.value
    return "\n".join(fact.description for fact in selected), kind
