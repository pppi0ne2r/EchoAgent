from __future__ import annotations

import json
from typing import Any

from echo_agent.crypto_budget import MAX_MEMORY_MB, MAX_OUTPUT_BYTES, MAX_WALL_SECONDS, resolve_crypto_budget
from echo_agent.crypto_contract import crypto_goal
from echo_agent.dispatcher.objectives import required_finding_count
from echo_agent.dispatcher.parsing import TEMPLATE_REASON_INTENTS
from echo_agent.models import FactKind, Intent, IntentOutcome, IntentStatus, Project

# reason prompt 的上下文预算：最近 N 条 evidence 全量，更早的只保留摘要。
_EVIDENCE_PROMPT_LIMIT = 40
_EVIDENCE_OLDER_CHARS = 160
# Crypto facts without typed_value/artifact_refs carry their whole content in the description,
# so they get a larger (but still bounded) summary instead of growing the prompt without limit.
_EVIDENCE_CRYPTO_OLDER_CHARS = 600

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
    "是否完成只看创建项目时填写的目的；目的原文在上下文的 objective 和 goal 中。",
    "目的尚未达到时，每确认一个独立的新事实，或确认某条路径已经关闭，就输出一条 JSON，不要继续扩大扫描。",
    "目的尚未达到时不要在结束时再输出重复的总结。",
    "已关闭或失败的路径必须使用 outcome=refuted，不要把否定结论写成 verified。",
    "目的已经达到时，本次执行只能输出一条 JSON，不要在它之前或之后再输出其他 JSON。",
)

# Shared by all crypto modes: carry a reproducible method across intents,
# not an assumption that a live connection or its parameters survived.
_CRYPTO_REPLAY_RULES = (
    "跨 intent 继承约定：继承可重放的方法、已核实的适用条件和持久脚本；fact 保留原始观察与来源，但观察的适用范围不能自动扩展到新连接。一次成功或单组参数不能证明所有会话都有同一弱点，未验证的推广仍是假设。",
    "仅当任务明确给出 nc/TCP 题目服务时，才在该目标与授权范围内交互；不要将纯离线文件任务自行扩展为网络任务。密码 worker 结束后容器可能重启，不依赖上一步的 nc、tmux、内存或活动连接继续存在。",
    "重连后先读取当前连接的参数并核实方法的适用条件，再用当前数据运行继承的方法。除非题目明确保证且已核实不变，不直接沿用旧会话的密钥、nonce、密文、oracle 样本、认证令牌或剩余查询次数；重新连接不等于能够恢复旧会话。",
    "同一密钥或同一连接下才成立的读取参数、采样、计算和获取结果，应在一个 intent 内形成有界的完整操作；不要拆成上一 intent 采样、下一 intent 重连后使用旧样本。遵守时间、内存、输出和题目请求限额，不能以保持会话为由延长硬预算。",
    "在 fact_description 区分可复用的方法、实际验证的前提、仅适用于当次连接的观察，并说明如何重新读取和执行；方法仍是假设时明确标为假设。将脚本与必要交互记录写入当前 intent 的持久目录，以 artifact_refs 引用实际文件、哈希和生产者；不虚构输入 ID 或已执行记录。",
    "离线文件、可重放脚本及已核实稳定的数据可以继续复用；会话专属检查点只在确认仍对应当前实例时使用，否则从当前参数重新开始。保留旧数据用于历史追溯，不能把它当作当前实例的参数或有效候选。",
    "如果方法不能在新连接内按预算重放，或遇到一次性状态、不可恢复的配额和断线，明确报告限制与已完成范围；不因此宣称数学无解，不无界重连，也不虚报会话已恢复。可重放方法应继续使用，不因密钥变化就否定整个方法。",
    "重放执行后仍须独立验证候选，并说明其对应的输入或会话；旧会话的 pass 不自动证明当前实例目标已完成。保存交互记录不等于现有验证器已支持该题型，无法独立核验时如实保持待验证。",
)

_CRYPTO_REASON_REPLAY_RULES = (
    "规划跨 intent 交接时保留可重放方法、已核实前提和持久脚本；未验证的推广仍是假设，旧会话观察不能自动扩展到新连接。",
    "交互操作仅能规划给任务明确提供的服务；由执行 intent 重新获取当前参数并核实前提，不假定 nc、tmux、密钥、样本或配额仍然有效。离线任务不增加网络操作。",
    "把依赖同一连接的读取、采样、计算和获取结果规划到一个有界 intent；离线或已核实稳定的数据可以复用，会话专属检查点需先核实适用性。",
    "无法在预算内重放或缺少实例数据时，规划有界的输入核查或说明限制，不将其视为数学无解；不通过更换会话标签绕过去重，也不复用旧实例的 pass 来完成当前目标。",
)


def _crypto_contract_rules(intent: Intent | None = None) -> str:
    example_id = intent.id if intent is not None else 0
    artifact = {
        "path": f"artifacts/intents/{example_id}/result.bin",
        "sha256": "0" * 64,
        "size_bytes": 2,
        "producer_intent_id": example_id,
    }
    lines = [
        "事实信任规则：已实际核实的观察必须显式填写 crypto.fact.claim_status=observed；猜测填 hypothesis，任何答案或 crypto.candidate 都按候选处理。缺少密码字段的输出只保留为待核实观察，不会自动成为 evidence。不要用 answer 字段承载中间数值，应使用 typed_value。",
        "observation_kind 枚举：crypto.fact.observation_kind 只允许 evidence 或 refuted；候选也写 evidence，候选身份只由 claim_status=candidate 表达，禁止写 observation_kind=candidate 或其他值；该字段可以省略。",
        "未完成退出时的观察保存：只有实际执行并亲自核对过输出，才在 crypto.fact 里填写 confirmed=true（每条已核实观察都应填写）；猜测、模板或未执行的内容不得填 true。exit_reason 为 timeout、budget_exhausted、solver_unknown、tool_error 或 cancelled 时，系统只把 claim_status=observed 且 confirmed=true 的末次观察保存为 evidence，其余只作为待核实观察。",
        "outcome 只取 verified、refuted、error 三个值，不要写 inconclusive、hypothesis 或其他值。尚未验证的猜测不要作为独立 JSON 输出；确需记录时用 outcome=error 且 crypto.fact.claim_status=hypothesis，系统会把它保存为待核实观察，不会进入 evidence。",
        "完成约定由任务创建者的 completion_goal 决定，不能自行改为更容易的目标。默认要求唯一明文；默认 RSA 答案为十进制整数、AES 为 UTF-8。若目标指定 hex/utf8、字节长度和字节序，按该表示提交，RSA 同时给出十进制 m。",
        "候选字段精确约定：crypto.candidate.answer_kind 默认 plaintext；因子目标必须填 factors 并提供十进制字符串 p、q，answer 写 p,q；私钥指数目标必须填 private_exponent 并提供 p、q、d，answer 写 d。answer_kind 必须匹配 completion_goal，不可自行更换。",
        "独立验证支持范围：RSA（原始文本/Python 字面量/JSON 的 n,e,c,p,q，支持 p,q 推导 n 与唯一一条 c = pow(明文符号,e,p*q) 关系加整数注释密文）；rsa-aes（原始输入同时给出 RSA 参数与 AES-CBC 的 iv/ciphertext：从原始输入解析 N/c/e/iv/ciphertext，候选在 crypto.candidate.m 给十进制 RSA 明文、answer 给解密出的明文；验证 RSA 关系、AES 解密/去填充与重加密，读取原始输入、不接受 worker 生成的 manifest 作为约束来源）；mt19937（随机数预测：原始输入含 gift/c 与生成参数，或 mt19937 JSON manifest；候选在 crypto.candidate.state 给 624 个 32-bit 状态字，或 state_ref 指向本 intent 的 state JSON artifact；验证器重放 gift 并逆 shuffle，answer 可为完整明文或其中的 flag{...} 子串）；AES（单个 JSON 清单，GCM 可验标签，CBC 需原始明文哈希）；DSA（p,q,g,y，JSON 或源码，候选私钥放 crypto.candidate.m）；ECDSA（secp256k1/secp256r1 或显式曲线参数，候选私钥放 crypto.candidate.m）；hash（manifest 含 hash、digest、data_encoding）；XOR（manifest 含 ciphertext、可选 key 或 key_length）；encoding（manifest 含 encoding、data）。不会执行源码或接受任意求解脚本作为验证器。其他 scheme 返回 unsupported verification scheme；inconclusive 表示无法验证，不表示数学无解；先核查缺少的输入，不循环尝试同一不支持的验证。",
        "复合题（rsa-aes）：当原始输入同时给出 RSA 参数与 AES-CBC 的 iv/ciphertext 时，候选只需提供十进制 m 和明文 answer；不要构建 AES manifest artifact 并希望它参与验证——独立验证只读取任务创建者上传的原始输入。若缺少 e 等参数，如实报告需要补充的原始输入，不要用 worker 自造的参数替代。",
        "随机数预测题（mt19937）：候选用 crypto.candidate.state 给出 624 个 32-bit 状态字，或用 state_ref 指向本 intent 的 state JSON artifact（如 artifacts/intents/<id>/state.json，需列入 artifact_refs）。answer 写恢复出的明文；若明文包含 flag{...} 之外的说明/噪声（例如题面附加的括号内容），answer 只写 flag{...} 部分，完整明文放 typed_value 或 fact_description。不要只提交脚本或让验证器执行源码。",
        "RSA 的 Python 参数文件只接受 n/e/c/p/q 的唯一顶层整数字面量赋值（可有注释、文档字符串或 int 注解），以及上述唯一一条 c = pow(...) 符号关系；含导入、其他调用、函数、控制流、删除或其他赋值的程序只能供分析，不能用其中的旧字面量完成独立验证。不要把 worker 自行生成的参数文件上传成原始题目输入来绕过此限制。",
        "明文答案表示：RSA 明文候选必须在 crypto.candidate.m 给出十进制 m；DSA/ECDSA 候选必须在 crypto.candidate.m 给出十进制私钥；hash/XOR/encoding 的 answer 必须是精确的 preimage/明文。answer 写成题面要求的确切字符串（可能是完整明文、flag{}、CTF{} 的内层字符串或裸串），并在描述中说明提取关系。不要假定固定 wrapper，格式由你按题面判断；框架只验证候选与明文/输入内容。",
        "答案形态（answer_form）：completion_goal.answer_form=hash 表示题面规定提交的 flag/答案是对恢复出的明文、文件或数据的哈希（如 md5、sha1、sha256）。此时 crypto.candidate.answer 必须写成该哈希的十六进制字符串（小写、不含 0x 和空格），明文/原始数据放进 crypto.candidate.m（RSA/DSA/ECDSA）或在 fact_description 里写明（AES 由解密得到），不要只放在 md5、hash 等自定义字段里——框架不会读取自定义字段做完成/回执。answer_form=literal 或未写时，answer 按题面要求的确切字符串填写。不要根据自己的猜测改变答案形态：题面没写哈希就交明文，写了哈希就交哈希。",
        "答案形态与 scheme 的关系：answer_form=hash 只适用于明文类目标（answer_kind=plaintext）与 rsa/aes/dsa/ecdsa 等密钥恢复类题型；因子目标和私钥指数目标提交的是整数，hash/XOR/encoding 的 answer 本身就是精确 preimage，不要在这三类上额外套一层哈希。",
        "完成模式：completion_goal.verification_mode 为 strict 时需要当前 pass 验证记录；为 evidence 时答案必须出现在已核实 evidence fact 中（除候选外还要把答案作为 claim_status=observed、confirmed=true 的观察保存）；为 acceptance 时只有平台回执可完成。不要自行更改该字段。",
        "多根：若输入 p,q 已知且 gcd(e,lcm(p-1,q-1))>1，明文在数学上不唯一；按题面选出目标根并在候选描述中说明其他根。完成需要任务创建者设置 solution_policy=any，不要自行更改 completion_goal。",
        '参数精确格式：整数 {"type":"integer","radix":10,"value":"55"}；字节 {"type":"bytes","encoding":"hex","value":"0007","length":2}。这是格式示例，值须替换为实际值；字节字段叫 value，不叫 hex，encoding 必须是 hex，length 是解码后字节数。',
        "输入清单已有的 SHA-256 可在 params 中直接复用为 64 位小写十六进制字符串，不必重新包装成 typed bytes；不能修改输入清单或虚构哈希。",
        "产物路径有两种表示：容器 /home/kali/workspace/intents/<id>/<file> 用于实际读写；artifact_refs[].path 和 checkpoint_ref.path 必须使用项目相对 POSIX 路径 artifacts/intents/<id>/<file>，不填容器绝对路径、Windows 路径或 ..。读取已有引用时使用上下文的 artifact_worker_paths。",
        "产物引用字段为 path、真实文件 sha256、size_bytes、producer_intent_id（整数，与路径中的 intent id 相同）；不用 producer 或 intent-0 字符串代替 producer_intent_id。",
        "产物引用格式示例（零哈希、文件名和大小只是占位，必须换成实际文件内容的结果；reason 中 id=0 也只是示例）：" + json.dumps(artifact, ensure_ascii=False),
    ]
    if intent is not None:
        lines.append(
            f"本次 intent 的实际产物目录是 /home/kali/workspace/intents/{intent.id}/；"
            f"对应引用前缀为 artifacts/intents/{intent.id}/，producer_intent_id={intent.id}。"
        )
    return "\n".join(lines)


class PromptBuilder:
    def build_first_run_prompt(self, project: Project, intent: Intent) -> str:
        if project.task_type == "crypto":
            return _build_crypto_first_run_prompt(project, intent)
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的执行 agent，当前模式是 first_run。",
                "这是项目第一次执行。先按创建项目时填写的目的直接完成。",
                "目的已经达到时，本次执行只输出一条 JSON：answer 写已经满足目的的结论，fact_description 写验证过程和证据，正文必须逐字包含 answer，outcome 为 verified。",
                "目的尚未达到时，每个独立事实各输出一条 JSON，answer 必须为 null。可以输出多条。新事实使用 outcome=verified，已关闭方向使用 outcome=refuted。",
                "不要规划后续 intent。后续 intent 由 reason 模式在下一轮循环中规划。",
                "你运行在该项目对应的 Kali Docker 容器中，可以使用容器内工具完成快速验证、扫描、利用和取证。",
                "不要长时间等待无输出命令；如果某个命令持续运行或输出为空，应立即停止等待，并把当前可确认的信息写入 fact_description。",
                "除非目的已经达到，否则不要因为单个命令卡住而继续等待。",
                *_WORKER_RESULT_RULES,
                "输出内容只能是 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                '{"answer": "满足目的的结论；不要照抄本句", "fact_description": "写入你实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
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
        if project.task_type == "crypto":
            return _build_crypto_explore_prompt(project, intent)
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的执行 agent，当前模式是 explore。",
                "你的任务是只执行给定 intent，不要规划新的 intent。",
                "你运行在该项目对应的 Kali Docker 容器中，可以使用容器内工具完成验证、扫描、利用和取证。",
                "当前 intent 已经满足目的时，本次执行只输出一条 JSON：answer 写已经满足目的的结论，fact_description 写验证过程和证据，正文必须逐字包含 answer，outcome 为 verified。不要再输出其他 JSON。",
                "目的尚未达到时，每个独立事实各输出一条 JSON，answer 必须为 null。可以输出多条。不要输出额外总结。",
                "不要长时间等待无输出命令；如果某个命令持续运行或输出为空，应立即停止等待，并把当前可确认的信息写入 fact_description。",
                "除非目的已经达到，否则不要因为单个命令卡住而继续等待。",
                *_WORKER_RESULT_RULES,
                "输出内容只能是 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                '{"answer": "满足目的的结论；不要照抄本句", "fact_description": "写入你本次 intent 实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "写入你本次 intent 实际验证得到的具体事实；不要照抄本句", "outcome": "verified"}',
                '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}',
                "",
                _format_worker_context(project),
                "",
                "当前要执行的 intent：",
                json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
            ]
        )

    def build_writeup_prompt(self, project: Project) -> str:
        return "\n".join(
            [
                "你是 CTF writeup 作者，当前任务只生成解题报告，不要继续做题。",
                "不要访问目标，不要运行利用、扫描或攻击命令。只阅读已有材料和日志。",
                "材料目录是 /home/kali/workspace/writeup-input/。",
                "project.json 是项目事实图，runtime.jsonl 是调度和执行日志。",
                "解题过程中产生的命令和文件在 /home/kali/workspace/intents/。",
                "题目附件在 /home/kali/attachments/，只在需要核对题目文件时读取。",
                "根据这些材料写一份中文 Markdown writeup，并写入绝对路径 /home/kali/workspace/writeup.md。",
                "当前工作目录可能是 /home/kali/workspace/writeup，不要把报告写到相对路径或其他文件。",
                "必须包含这些小节：题目信息、结论、解题步骤、可复现 Payload、关键结果、flag。",
                "解题步骤按实际成功路径编号。每一步写明目的、具体动作和观察到的结果，读者应能按顺序重做。",
                "与最终路径无关的失败尝试不要展开；确实影响后续步骤的失败，用一句话说明原因。",
                "可复现 Payload 必须照抄材料中的原始命令、HTTP 请求、脚本或利用载荷，放在 Markdown 代码块中。",
                "每个 payload 注明来源 intent 或文件路径，并写清前置条件，例如 cookie、参数、工作目录和执行顺序。",
                "不要把 payload 改写成无法执行的概述。材料里没有可执行载荷时，该节写未记录。",
                "只能写材料里出现过的事实、命令、payload 和 flag。材料没有的内容要明确写未记录，不要编造。",
                "如果没有获得 flag，flag 一节写未获得。",
                "写完文件后，最终回复只保留一行：WP 已写入 /home/kali/workspace/writeup.md",
                "",
                "项目名称：" + (project.name or ""),
                "目标：" + (project.target_url or ""),
                "目的：" + (project.objective or ""),
            ]
        )

    def build_reason_prompt(self, project: Project) -> str:
        if project.task_type == "crypto":
            return _build_crypto_reason_prompt(project)
        required = required_finding_count(project.objective)
        return "\n".join(
            [
                "你是一个 CTF/渗透测试题目的规划 agent，当前模式是 reason。",
                "你的任务是根据 evidence 判断项目是否已经完成；如果未完成，只从 allowed_from 给出下一批 intent。",
                "不要执行命令，不要进行扫描或利用，只做推理和规划。",
                "最终答案的形式不固定，不要假设一定是 flag{...}。是否完成只对照 goal 中创建项目时填写的目的原文。",
                *_reason_completion_rules(required),
                "猜测、扫描器告警和未实际验证的描述不能完成，应规划更小的验证步骤。",
                "目的没有要求的结果不能当成完成。",
                f"生成的 intent description 必须描述接下来需要探索的方向或验证动作，例如“{_EXAMPLE_DESCRIPTION}”，不要写成笼统的“继续推进”。",
                "生成的 intent 应该具体、可执行，并且能由后续 explore 模式在 Kali Docker 容器中完成。",
                "不是每条 evidence 都需要生成 intent；只选择对达成目标有价值、能继续验证或利用的 evidence 生成 intent。",
                "每个新 intent 的 from 必须取 allowed_from 里的 fact id。evidence 为空时 allowed_from 只有起点，通常是 0。",
                "不能使用 findings、do_not_repeat、already_done、证伪事实、错误事实或 goal 作为 from。",
                "to 必须为 null。未执行完成前不要指向 goal，系统不会把空目标临时连到终点。",
                f"每条 intent 必须带稳定的 action_key，使用小写英文和连字符，例如 {_EXAMPLE_ACTION_KEY}。",
                "already_done 中 outcome 为 refuted 或 verified 的动作不能再规划，即使更换 action_key 或换一种说法也不行。",
                "outcome 为 error 或 empty 时，不要重复原来的 description；只有描述明显更小的新步骤才可以规划。",
                "error、empty 的 note，以及“未完成执行的输出摘录”，都不是证伪结论，只能改写成更小的新步骤。",
                "findings 里的证伪正文只用于判断，不能作为 from，也不能换一种说法重做。",
                "允许同一个 evidence 产生多个 intent，但 action_key 不能重复。",
                "只有答案已经作为满足目的的结论出现在 verified 执行记录中时，才返回 completed=true。" if required <= 1 else "多漏洞完成只看 accepted_fact_ids。某一条 verified 记录里出现总结句，不能单独返回 completed=true。",
                "agent reported possible final answer、未完成执行的输出摘录、error 和 refuted 都不能单独作为完成依据。",
                "若 evidence 带 source: 行，那是去掉结果 JSON 后的原始输出片段，只能用来核验答案是否真实出现过。",
                "如果没有新的可执行方向，返回 completed=false、next_intents=[]，并用 stop_reason 说明原因。不要为了凑数重复旧动作。",
                "示例里的 action_key、description 和 fact id 只说明格式，不要原样输出。",
                "最终只输出一个 JSON 对象，不要输出 Markdown 或额外解释。",
                "",
                "JSON 格式：",
                _completed_example(required),
                '{"completed": false, "answer": null, "stop_reason": "上传和备份面已经验证关闭", "next_intents": []}',
                _REASON_INTENT_EXAMPLE,
                "",
                _format_project_context(project),
            ]
        )



def _reason_completion_rules(required: int) -> list[str]:
    if required <= 1:
        return [
            "只有 evidence 中已经有逐字满足该目的的结论时，才返回 completed=true，answer 必须原样复制该结论。",
        ]
    return [
        f"当前目的要求至少 {required} 个已验证漏洞。completed=true 时必须提交 accepted_fact_ids，列出至少 {required} 个不同的 evidence fact id。",
        "answer 只写简短总结，不能用来代替 accepted_fact_ids，也不需要逐字出现在某一条事实里。",
        "一条事实里写“第N个漏洞”或“已确认N个漏洞”不算 N 个事实。系统只按 accepted_fact_ids 里的 evidence 计数。",
        "accepted_fact_ids 只能选择 evidence。start、goal、refuted、error、未完成摘录和候选答案包装都不能列入。",
        "verified 只表示某次动作的结论成立，不表示该事实自动属于最终结果。没有列入 accepted_fact_ids 的事实不会连到目标。",
        "不要把可达性、目录枚举或过程记录凑进 accepted_fact_ids。只列入已经单独验证、并且本身就是漏洞结论的 evidence。",
    ]


def _completed_example(required: int) -> str:
    if required <= 1:
        return '{"completed": true, "answer": "实际可提交的最终答案", "stop_reason": "", "next_intents": []}'
    example_ids = list(range(6, 6 + required))
    return json.dumps(
        {
            "completed": True,
            "answer": f"已确认{required}个漏洞",
            "accepted_fact_ids": example_ids,
            "stop_reason": "",
            "next_intents": [],
        },
        ensure_ascii=False,
    )


def _build_crypto_first_run_prompt(project: Project, intent: Intent) -> str:
    return "\n".join([
        "你是密码学 CTF 题目的执行 agent，当前模式是 first_run。只做输入整理与低成本检查，不套用 Web 扫描或漏洞利用步骤。",
        *_CRYPTO_REPLAY_RULES,
        _crypto_contract_rules(intent),
        "first_run 只低成本识别文件格式、交互协议和参数来源；需要同一会话的完整计算留给后续单个 intent 重放，不在此先消耗大量查询配额，也不承诺把当前连接交给下一步。",
        "核查实例输入是否足够：源码描述交互或生成密文，不代表已提供实际参数、密文或服务地址；确有缺项时保存明确观察并说明需要补充什么，不捏造实例，不把格式分析当成恢复了 flag。",
        "先按输入清单读取 worker_path，并核对原始字节、长度和 SHA-256；文件级基数/字节序只是用户声明，解析前须核实。",
        "提取的 typed 参数严格使用上述整数/字节 JSON 格式，保留前导零，不用浮点数。",
        "可做格式检查、已给参数间的整除/互素/gcd 等低成本检查；不要在 first_run 启动无边界大数分解、暴力搜索或长期求解器。",
        "未经检查的弱点只能作为后续假设，不得写成已验证 fact。每个已确认观察立刻输出一个 JSON 对象，最后再输出总结；这样超时前的观察仍可保存。",
        "候选答案不是最终验证。候选用 crypto.fact.claim_status=candidate 与 crypto.candidate={answer,scheme,...} 输出；RSA 明文必须附 crypto.candidate.m（十进制），可选 p,q,d，AES 可附 key_hex、answer_encoding。候选的 observation_kind 只写 evidence，不要写 candidate。不要仅因可打印、像 flag 或模型声称成功而完成题目。",
        "每个 JSON 包含 answer、fact_description、outcome，以及 crypto.exit_reason 和 crypto.covered_scope；要作为证据保存的已核实观察，crypto.fact 必须写 claim_status=observed 和 confirmed=true。",
        "outcome=verified 表示 fact_description 是实际观察；outcome=refuted 表示当前有边界的假设被否定。否定观察（如 gcd(n1,n2)=1）仍可用 crypto.fact.observation_kind=evidence 保存为可复用证据。",
        "exit_reason 使用 completed、range_exhausted、timeout、budget_exhausted、solver_unknown、tool_error 或 cancelled；超时、预算耗尽、unknown 和错误不能表示无解。covered_scope 写实际完成范围，不能照抄尚未做完的计划范围。",
        "最终只输出 JSON 对象，不要 Markdown。示例仅说明格式，不要照抄数值：",
        '{"template":true,"answer":null,"fact_description":"示例事实；不要照抄","outcome":"verified","crypto":{"exit_reason":"completed","covered_scope":{"input_ids":["input-1"]},"fact":{"claim_status":"observed","confirmed":true,"source_input_ids":["input-1"],"typed_value":{"type":"integer","radix":10,"value":"55"}}}}',
        "系统生效预算：" + json.dumps(resolve_crypto_budget(project.crypto.get("default_budget"), intent.crypto.get("budget")).to_dict()),
        _format_worker_context(project),
        "初始 intent：",
        json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
    ])


def _build_crypto_explore_prompt(project: Project, intent: Intent) -> str:
    return "\n".join([
        "你是密码学 CTF 题目的执行 agent，当前模式是 explore。只执行给定 intent 的方法、输入、参数和范围，不自行扩大搜索。",
        *_CRYPTO_REPLAY_RULES,
        _crypto_contract_rules(intent),
        "执行继承脚本前核对实际文件与哈希，并检查它是否重新获取当前参数而非硬编码旧值。当前参数不满足方法前提时记录差异，不擅自扩大方法、查询范围或重连次数。",
        "先核对输入 ID 对应文件的哈希与 typed 参数；计划范围与实际完成范围分别记录。大材料写入当前 intent 的持久目录，只在 JSON 中给出引用。",
        "每得到一个已检查的观察立即输出 JSON。猜测不能写成 verified fact；候选须标记 crypto.fact.claim_status=candidate 并给 crypto.candidate 结构，等待独立验算；候选的 observation_kind 只写 evidence，不要写 candidate；RSA 明文候选须附 crypto.candidate.m（十进制）。",
        "负结果要写清输入、参数和范围。例如 gcd(n1,n2)=1 是观察证据，同时只否定这两个模数存在共享因子的假设；不否定其他 RSA 方法。",
        "仅在指定范围真实穷尽时使用 exit_reason=range_exhausted；timeout、solver_unknown、tool_error、cancelled 均不得推出无解。超时前已经确认的观察仍应输出。",
        "当前 intent 的 budget.wall_seconds、memory_mb、output_bytes 是硬上限；长任务应分块并保存可复用检查点。允许合法无输出计算，但硬截止时间仍会生效。",
        "若保存了可继续使用的检查点，可在 crypto.checkpoint_ref 中给出当前 intent 持久目录内文件的 path、sha256、size_bytes 和 producer_intent_id；没有实际文件不要声称有检查点。",
        "每个 JSON 包含 answer、fact_description、outcome、crypto.exit_reason、crypto.covered_scope。crypto.fact 可包含 source_input_ids、typed_value、artifact_refs；反驳假设而观察仍可复用时设 observation_kind=evidence。要作为证据保存的已核实观察必须写 claim_status=observed 和 confirmed=true；候选同样用 observation_kind=evidence。",
        "最终只输出 JSON 对象，不要 Markdown。示例仅说明格式，不要照抄数值：",
        '{"template":true,"answer":null,"fact_description":"示例事实；不要照抄","outcome":"refuted","crypto":{"exit_reason":"completed","covered_scope":{"input_ids":["input-1","input-2"]},"fact":{"claim_status":"observed","observation_kind":"evidence","source_input_ids":["input-1","input-2"]}}}',
        "系统生效预算：" + json.dumps(resolve_crypto_budget(project.crypto.get("default_budget"), intent.crypto.get("budget")).to_dict()),
        _format_worker_context(project),
        "当前 intent：",
        json.dumps(intent.to_dict(), ensure_ascii=False, indent=2),
    ])


def _build_crypto_reason_prompt(project: Project) -> str:
    return "\n".join([
        "你是密码学 CTF 题目的规划 agent，当前模式是 reason。只根据已确认 evidence 规划，不执行命令。",
        *_CRYPTO_REASON_REPLAY_RULES,
        _crypto_contract_rules(),
        "本模式只使用提供的上下文做规划，不调用 shell、不读写容器文件、不采样或计算新结果；需要读取文件、核实参数或做计算时，规划给 explore 执行并保存 fact，不把 stop_reason 或 intent 描述当作新证据。不要在容器里搜索框架源码或方法清单。",
        "crypto.method 是自由命名的稳定动作标识，推荐小写点分如 source.inspect 或 ntru.check，不要求匹配内置算法表；普通 method 由 worker 按 description 执行，并参与去重。verify.candidate 是保留方法，由框架执行候选校验，不能把其他动作命名成它。",
        "上下文 planning_feedback 只包含上轮被拒计划的字段错误，不是 evidence、证伪或已完成计算。优先修正指出的键名、格式、引用或预算；只修格式时可保留原 action_key，不要通过改名绕过去重。",
        "规划交互 intent 时在 description 写清重连后读取哪些当前参数、检查哪些前提、如何运行可重放方法、请求次数上限与断线处理；使用现有 params、scope 和 budget 表达任务边界，不预填尚未读取的参数或伪造 input_ids。新连接的数据尚未获得时，不能声称参数已改变或通过换会话标签绕过去重；现有契约无法表达时用 stop_reason 说明限制。",
        "goal、猜测、候选答案和超时输出摘录都不是已验证证据。先为候选规划 method=verify.candidate 的 intent，params.candidate_fact_id 指向候选 fact；只有 verifications 中该候选有 pass 记录时才可返回 completed=true 和完全相同的 answer。answer_form=hash 时 completed 的 answer 是该哈希字符串；verifications 记录的 checks.preimage_hex 是按候选恢复出的明文十六进制，可用于核对明文与哈希的对应关系。",
        "从 allowed_from 选择起点；REFUTED/ERROR fact 不可作为 from，但其中的已覆盖范围可用于缩小下一步。",
        "下一步必须是有边界的可执行检查，指定 crypto.method、input_ids、input_fact_ids、params 和 scope。from 只是图中的起点；input_fact_ids 必须列出真正依赖的事实。params 中整数/字节严格遵守上述精确 JSON 格式，不能用浮点数。",
        "读取 executions 的 exit_reason、planned_scope 与 covered_scope；已完成的某个范围只关闭该范围。timeout、solver_unknown 和工具错误不等于数学上无解。不要改写旧动作来无界重试。",
        "同一计算超时后，只有使用该次执行记录中的 checkpoint_ref 作为 resume_from，或在 budget 中明确增加 wall_seconds、memory_mb、output_bytes 之一，才可重试；仅改描述或标签无效。只在已知输入足够时规划低成本检查；预算必须在系统上限内。",
        "预算字段位置：intent 的预算必须写在 crypto.budget 内，例如 {\"crypto\":{\"method\":\"...\",\"budget\":{\"wall_seconds\":1200,\"memory_mb\":1024,\"output_bytes\":4194304}}}；不要写在 intent 顶层或写成裸字段。顶层 budget 会被迁移并记录 warning，但不应依赖该兜底。",
        "resume_from 必须来自相同输入版本、前提快照、方法、参数和范围的失败计算；条件改变或旧快照缺失时，从头规划且不携带旧检查点。可重放脚本作为 artifact_refs 使用，不把脚本当作计算检查点。",
        f"预算上限：wall_seconds<={MAX_WALL_SECONDS}，memory_mb<={MAX_MEMORY_MB}，output_bytes<={MAX_OUTPUT_BYTES}；未写时使用项目默认或系统默认。",
        "每个 action_key 用稳定的小写英文连字符；只规划与当前密码输入相关的检查。",
        "最终只输出一个 JSON 对象，不要 Markdown。示例仅说明格式，不要照抄数值：",
        '{"completed":false,"answer":null,"stop_reason":"","next_intents":[{"from":2,"to":null,"action_key":"check-shared-factor","description":"对指定两个模数计算 gcd，并记录等式与输入来源","crypto":{"method":"rsa.gcd","input_ids":["input-1","input-2"],"input_fact_ids":[2],"params":{},"scope":{"input_ids":["input-1","input-2"]}}}]}',
        _format_project_context(project),
    ])


def _crypto_worker_inputs(project: Project) -> list[dict[str, Any]]:
    return [
        {**reference, "worker_path": f"/home/kali/workspace/{reference['path']}"}
        for reference in project.crypto.get("inputs", [])
    ]


def _format_worker_context(project: Project) -> str:
    closed_actions = [
        {
            "action_key": intent.action_key,
            "description": intent.description,
            "outcome": intent.outcome.value,
        }
        for intent in _executed_intents(project)
    ]
    if project.task_type == "crypto":
        for item, intent in zip(closed_actions, _executed_intents(project)):
            item["id"] = intent.id
            item["crypto"] = intent.crypto
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
    if project.task_type == "crypto":
        payload["task_type"] = "crypto"
        payload["completion_goal"] = crypto_goal(project.crypto.get("goal", {}))
        payload["require_platform_acceptance"] = project.crypto.get("require_platform_acceptance", False)
        payload["inputs"] = _crypto_worker_inputs(project)
        payload["executions"] = project.crypto.get("executions", [])
        payload["candidates"] = [fact.to_dict() for fact in project.facts.values() if fact.kind == FactKind.CANDIDATE]
        payload["verifications"] = project.crypto.get("verifications", [])
    return "\n".join(
        [
            "执行上下文只包含任务信息、已验证证据和已经关闭的动作。",
            "target_url 可以同时包含 URL、IP 和容器内附件路径，不要假定它一定是网页地址。",
            "closed_actions 给出已执行动作；密码任务另附 intent id 和结构化参数，不包含证伪事实编号。" if project.task_type == "crypto" else "closed_actions 只给出 action_key、description、outcome，不包含证伪事实编号。",
            "不要在相同输入、参数和范围内重做 closed_actions；不同范围须明确写出新边界。" if project.task_type == "crypto" else "不要重做 closed_actions 里的动作，也不要根据缺失的事实编号继续扩展。",
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
        if project.task_type == "crypto":
            already_done[-1]["crypto"] = intent.crypto
            already_done[-1]["result_fact_ref"] = f"/projects/{project.id}/facts/{intent.to_fact_id}" if intent.to_fact_id is not None else None
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
    if project.task_type == "crypto":
        payload["task_type"] = "crypto"
        payload["completion_goal"] = crypto_goal(project.crypto.get("goal", {}))
        payload["require_platform_acceptance"] = project.crypto.get("require_platform_acceptance", False)
        payload["inputs"] = _crypto_worker_inputs(project)
        payload["executions"] = project.crypto.get("executions", [])
        recent_executions = project.crypto.get("executions", [])[-8:]
        recent_timeouts = [item for item in recent_executions if item.get("exit_reason") == "timeout"]
        if recent_timeouts:
            note = (
                f"最近 {len(recent_timeouts)} 次执行以 timeout 结束；重试同一计算时必须把更高的 "
                "wall_seconds 写进 crypto.budget（不是 intent 顶层），或使用该次执行的 checkpoint_ref 续算。"
            )
            last_budget = next(
                (item.get("budget") for item in reversed(recent_executions)
                 if isinstance(item.get("budget"), dict)),
                None,
            )
            if last_budget:
                note += " 最近一次执行预算：" + json.dumps(last_budget, ensure_ascii=False) + "。"
            payload["budget_note"] = note
        payload["candidates"] = [fact.to_dict() for fact in project.facts.values() if fact.kind == FactKind.CANDIDATE]
        payload["verifications"] = project.crypto.get("verifications", [])
        if project.crypto.get("planning_feedback"):
            payload["planning_feedback"] = project.crypto["planning_feedback"]
    return "\n".join(
        [
            "项目上下文分为已知证据、证伪结论、已经做过的动作、不要重复的失败路径、终点说明。",
            "target_url 可以同时包含 URL、IP 和容器内附件路径，不要假定它一定是网页地址。",
            "evidence 才是已验证事实。新 intent 的 from 只能取 allowed_from。",
            "evidence 为空时，allowed_from 只包含起点，避免没有证据时无法继续规划。",
            "findings 是证伪或错误事实的正文。不能作为 from，也不能换一种说法重做。",
            "already_done.result 是该动作实际得到的结论。",
            "refuted 和 verified 仅关闭相应 executions.covered_scope；同一输入、方法、参数和范围不能换名重做，其他范围仍可规划。" if project.task_type == "crypto" else "refuted 和 verified 不能更换 action_key 或换一种说法再规划。",
            "timeout、solver_unknown、tool_error 和 empty 不证伪假设；缩小或改变输入、参数、范围可以继续；原范围重试需匹配检查点或提高明确的计算预算。" if project.task_type == "crypto" else "error 和 empty 不能原样重复 description；只有更小的新步骤才可以继续。",
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
        has_full_crypto_reference = bool(fact.crypto.get("typed_value") or fact.crypto.get("artifact_refs"))
        limit = (
            _EVIDENCE_CRYPTO_OLDER_CHARS
            if project.task_type == "crypto" and not has_full_crypto_reference
            else _EVIDENCE_OLDER_CHARS
        )
        if index < keep_from and len(description) > limit:
            description = description[:limit] + "…（较早事实已截断）"
        item: dict[str, object] = {"id": fact.id, "description": description}
        if project.task_type == "crypto":
            item["fact_ref"] = f"/projects/{project.id}/facts/{fact.id}"
            item["crypto"] = fact.crypto
            if fact.crypto.get("artifact_refs"):
                item["artifact_worker_paths"] = [
                    f"/home/kali/workspace/intents/{reference['path'].split('/', 2)[2]}"
                    for reference in fact.crypto["artifact_refs"]
                ]
        items.append(item)
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
