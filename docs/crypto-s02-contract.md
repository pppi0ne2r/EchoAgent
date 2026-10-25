# S02：密码学任务的最小数据与执行约定

状态：S02 设计交付（2026-10-21）。本文是后续实现的契约，不表示项目已能运行密码题。S02 不改动数据库、API、调度器或提示词；实现与运行验收分别安排在 S03–S10。

## 1. 现有框架与适配边界

沿用 `first_run → explore → reason` 和现有 Project、Fact、Intent。渗透任务的执行提示词要求模型返回 `answer`、`fact_description`、`outcome`，规划提示词只允许从已确认的 evidence 延伸 intent。现有 `Fact` 保存 `description/kind`，`Intent` 保存 `from/to/description/status/outcome/action_key`；这些字段继续保留。项目没有任务类型字段，网页创建表单要求目标 URL。当前答案门槛主要检查答案字符串是否出现在 evidence 中，不等于密码学等式或字节校验。

密码任务首先复用自然语言描述来说明方向、命令和观察；只把必须由程序精确比较、恢复或校验的信息定义为机器可读记录。不要把 JSON 字符串塞入 `fact_description` 冒充结构化存储。下述字段是**逻辑字段**：S04 再决定 SQLite 列、JSON 元数据及项目文件清单的实际布局。

## 2. 最小逻辑契约

| 对象 | 逻辑字段与含义 | 与现有字段的关系 |
| --- | --- | --- |
| Project | `task_type: general | crypto`；默认 `general`。`schema_version: 1` 仅用于密码元数据。`inputs[]` 为输入清单；`default_budget` 为可选预算模板。 | `name/objective/hint` 保留；`crypto` 可不填 `target_url`，普通任务行为不变。 |
| InputRef | 项目内唯一 `id`、`role`、相对路径 `path`、原始字节的 `sha256`、`size_bytes`、可选 `media_type`。 | 输入内容由文件链路提供；清单不复制整份源码、密文或矩阵。 |
| Fact | `source_input_ids[]` 或 `producer_intent_id`；可选 `depends_on_fact_ids[]`、`typed_value`、`artifact_refs[]`。 | `description/kind` 仍供人和模型阅读；无来源的历史 fact 不伪造来源。一次 intent 的多个 fact 各自保留同一个生产者。 |
| Intent | `input_fact_ids[]`、`input_ids[]`、稳定 `method`、有类型的 `params`、`scope`、可选 `budget` 与 `resume_from`。 | 原有 `from/to` 作为图的主边；`to` 可指一个主要结果，不代表只有一个产出。`action_key` 是可读标签，不作为完整参数指纹。 |
| ExecutionRecord | `exit_reason`、`covered_scope`、`artifact_refs[]`、`tool/version`、可选 `checkpoint_ref`。 | 原有 `status` 表示生命周期，`outcome` 表示已验证/证伪/错误等图语义；不能拿它们代替精确退出原因。 |
| VerificationRecord | 候选值、候选来源 fact、使用的输入哈希、校验方法/版本、实际输出、`pass/fail/inconclusive`。可选平台接受回执另记。 | 候选不是已验证 fact；现有 `answer` 字符串在 evidence 中出现不等于验证通过。S09 实施完成门槛。 |

`InputRef` 与 `artifact_refs` 只存项目内相对路径，实际存储位置由宿主机项目目录管理；不得把 Windows 绝对路径或容器临时路径当成持久标识。文件哈希基于原始字节，计算前后都可复核。输入文件以只读方式交给 worker；输出和检查点另存持久位置。目录创建、路径校验和挂载链路留给 S04/S05。

### 值的编码

- 整数：`{"type":"integer","radix":10,"value":"55"}`；十六进制须显式写 `radix:16`。JSON number 不用于密码大整数，浮点数不得参与传递。
- 原始字节：`{"type":"bytes","encoding":"hex","value":"0007","length":2}`。`length` 与解码后的字节数一致，保留前导零；字节序只在转为整数时另外声明。
- 大文件、矩阵、源码和长日志：只保留 `InputRef` 或产物引用、哈希、大小和必要摘要，不将全文塞入 fact/intent 或提示词。
- `params` 中的整数/字节沿用上述有类型值。S07 再按规范化后的方法、输入哈希、参数和范围计算去重指纹，不能仅靠改写 `action_key` 控制重试。

### 结果与事实语义

`ExecutionRecord.exit_reason` 至少区分 `completed`、`range_exhausted`、`timeout`、`solver_unknown`、`tool_error`、`cancelled`。`covered_scope` 必须说明真实完成的部分；计划范围与完成范围是两个字段。`range_exhausted` 只能说明指定范围已穷尽，不能推出全局无解；`timeout`、`solver_unknown` 和工具故障也不证伪数学假设。

`FactKind.EVIDENCE` 可以承载正、负观察，例如 `gcd(n1,n2)=1`。它否定的是**在本次输入下存在共享因子**的假设，不否定整个 RSA 方向。`REFUTED` 应明确被反驳的命题和适用输入/范围。现有通用提示词把已关闭路径引导为 `outcome=refuted`；密码任务需要在 S06 添加这一区分，不能直接照搬“没有找到”即关闭方向。

## 3. 同一份 RSA 输入的完整样例

样例输入文件 `inputs/rsa.txt` 的 UTF-8 原始字节恰为 `n=55\ne=3\nc=13\n`（末尾一个 LF）。长度 14 字节，SHA-256 为 `4bb309166ae5225d8a9c728841f9604e8aa95f0b95c78d4b108d4cbff070028f`。这是刻意很小的契约样例，不是用于评价求解性能的题目。

| 顺序 | 图或记录 | 必须写清的内容 |
| --- | --- | --- |
| 1 | Project 与 InputRef | `task_type=crypto`；`input-1` 指向 `inputs/rsa.txt`，记录上述字节长度和哈希；目标是恢复明文。原有起点 fact 0、目标 fact 1 保留。 |
| 2 | Fact 2（输入证据） | 从 `input-1` 提取 `n=55, e=3, c=13`；三者分别是十进制整数字符串，`source_input_ids=[input-1]`。 |
| 3 | Intent 0（有边界的假设） | 假设小因子可在 `2…10` 找到；`method=factor.trial`，`input_fact_ids=[2]`，`params.n=55`，`scope=[2,10]`。图上 `from=2`；尚未计算时不能预写因子 fact。 |
| 4 | ExecutionRecord 0 与 Fact 3 | 实际完成 `2…5` 并发现 `5`；检查 `5×11=55`。退出原因为 `completed`；Fact 3 记录因子 `5,11` 和检查结论，`producer_intent_id=0`、`depends_on_fact_ids=[2]`。如同时产生脚本或日志，各产物按引用与哈希记录。 |
| 5 | Intent 1 与 Fact 4 | `input_fact_ids=[2,3]`，计算 `φ(n)=40`、`d=27`、`m=7`；Fact 4 只记录实际算得的结果和来源，不宣称平台已接受。若需要字节表示，必须显式声明长度，例如单字节 `07`。 |
| 6 | 候选与 VerificationRecord 0 | 候选明文整数 `7` 来自 Fact 4。独立使用原始输入检查 `7³ mod 55 = 13`，状态 `pass`，记录校验命令/程序版本和输出。`pass` 只证明此数学条件成立，不表示 CTF 平台已接受提交。 |

多输入、多产出同理：若两个模数分别来自 `input-1/input-2`，共享因子检查 intent 同时引用对应两个 fact；它若产生 `gcd` 和两个新因子三个结论，各 fact 均记录相同的 `producer_intent_id`，并分别记录自己的依赖。图的 `to_fact_id` 只需选择一个主要结论，不承担完整追溯。

### 反例必须能如实表达

- 搜索计划为 `2…100000`，只做到 `2…5000` 就超时：`exit_reason=timeout`、`covered_scope=[2,5000]`；已验证的观察可以保存，不能产生“没有因子”的 fact，也不能把原假设全局证伪。下一次可引用检查点或从 `5001` 继续。
- 指定范围 `2…10` 确实穷尽且未发现因子：`exit_reason=range_exhausted`、`covered_scope=[2,10]`；可记录“此范围无因子”的 evidence，不能写“n 无因子”。
- 解密程序给出候选，但重加密不匹配原始 `c`：VerificationRecord 状态 `fail`；保留候选与失败证据，不完成项目。

## 4. 兼容与实施顺序

1. **旧项目**：缺少 `task_type` 时视为 `general`，缺少密码元数据时使用空值；旧 fact/intent 的文字和类别照原样保留，不从描述中推测输入哈希、生产者或已通过的验证。旧项目的 URL、API 与调度行为保持原样。
2. **新密码项目**：输入清单与必要的机器记录必须经过模型 → JSON/API → SQLite → 模型的往返；未知 schema 版本要明确报错或只读处理，不能静默丢弃。现有 `save_project` 会删后重建 facts/intents，新增元数据必须跟随模型和存储完整保留；API 请求模型也必须接纳它们。
3. **S03** 固定工具环境；**S04** 实现元数据与持久化产物；**S05** 打通无 URL 的离线输入；**S06** 改密码任务提示词与 fact/intent 语义；**S07** 实现参数化去重和续算；**S08** 落实预算与精确退出原因；**S09** 将独立验证结果接入完成门槛。S02 本身只冻结以上约定，不提前声称这些行为已实现。

## 5. S02 审查清单

- 同一 RSA 输入能表达“假设 → 有边界计算 → 因子事实 → 候选 → 独立校验”。
- 多个输入、多个产出有明确编号和来源，`from/to` 仍用于图显示。
- 整数与字节有明确类型、基数、长度；不会丢失前导零或经过浮点数。
- 大材料保留文件引用与哈希，提示词只使用摘要。
- 旧记录缺少新信息时默认兼容且不编造验证状态；实现阶段有明确落点和验收边界。
