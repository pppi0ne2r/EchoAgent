# BUG-033 修复记录：intent 顶层预算的迁移与提示（2026-10-24）

对应台账：`EchoAgent-Bug记录.md` → BUG-033
实测：ezran（项目 #49），intent 5 声称把预算提高到 1800s，实际仍按 300s 超时。

## 问题

`reason` 把 `budget` 写在 intent 顶层：

```json
{"from": 7, "to": 1, "action_key": "...", "description": "...",
 "crypto": {"method": "...", "input_ids": [...], "params": {}, "scope": {...}},
 "budget": {"wall_seconds": 1800, "memory_mb": 2048, "output_bytes": 4194304}}
```

而：

- `parsing._parse_reason_intents()` 只保留 `description/from/to/action_key/crypto`，顶层 `budget` 被**静默丢弃**；
- `core._crypto_plan_metadata()` 只读取 `item["crypto"]`，因此预算回落到默认 300s；
- 提示词没有说明预算的层级，reason 上下文也没有展示最近超时次数/预算，模型无法判断「提高预算」是否生效。

结果：intent 5 仍 300s 超时，5 次求解全部失败，步数耗尽。

## 修复

1. **解析层不再静默丢弃**（`dispatcher/parsing.py`）
   - `_parse_reason_intents()` 保留所有非标准顶层字段，交给后续校验/迁移。
2. **迁移已知 crypto 字段**（`dispatcher/core.py`）
   - `Dispatcher._MISPLACED_CRYPTO_FIELDS = method, input_ids, input_fact_ids, params, scope, budget, resume_from`；
   - `_crypto_plan_metadata()` 在 `intent_crypto(raw)` 后把这些顶层字段合并进 `crypto`（嵌套字段优先，不覆盖已有值）；
   - **不迁移**框架生成/版本字段（`premise_versions`、`input_versions`、`continuation`、`verification_context`），防止模型伪造前提快照或重试记账。
3. **可见性**
   - 调用处对发生迁移的规划记录 warning 日志：`moved_fields`、`action_key`；
   - reason 上下文新增 `budget_note`：最近 8 条 execution 中 `timeout` 的次数、最近一次实际预算，并明确要求把更高的 `wall_seconds` 写进 `crypto.budget`（不是 intent 顶层）或使用 `checkpoint_ref`。
4. **提示词**（`dispatcher/prompts.py`）
   - reason 规则新增「预算字段位置」：给出 `{"crypto":{"method":"...","budget":{"wall_seconds":1200,"memory_mb":1024,"output_bytes":4194304}}}` 示例，明确不要写在 intent 顶层；顶层会被迁移并记录 warning，但不应依赖。

## 行为

- 写在 `crypto.budget` 的正常规划不变；
- 顶层 `budget`（以及 `method/input_ids/input_fact_ids/params/scope/resume_from`）现在会被迁移并生效，同时记录 warning；
- 顶层与 `crypto` 内同时给出同名预算时，**`crypto` 内的值优先**；
- 其他未知顶层字段仍保留在 item 中，不会静默消失，便于后续拒绝或诊断。

## 验证

新增 `tests/test_bug033_budget_location.py` 6 项：

- `parse_reason_result` 保留顶层 `budget`；
- `_crypto_plan_metadata` 迁移顶层 budget，`resolve_crypto_budget` 与 `plan_crypto_intents` 均得到 1200s；
- 伪造的 `premise_versions`/`input_versions` 不被迁移；
- 嵌套 budget 优先；
- reason 提示词包含「预算字段位置 / crypto.budget / 不要写在 intent 顶层」；
- reason 上下文在存在 timeout execution 时输出 `budget_note` 与最近预算。

全量 **371 项**：仅 5 项既有平台性失败（4 项 PATH `python` 商店桩、1 项 NTFS `chmod` 无效）。

## 未覆盖

- 未重跑真实模型；ezran 的项目 #49 仍保留旧运行结果（5 次 300s 超时）。
- 只迁移已知 crypto 字段；其他顶层拼写错误仍需模型按 planning_feedback/日志修正。
- `budget_note` 只统计最近 8 条 execution，超过该窗口的历史超时不展示。
- 修复已加载到宿主机服务；旧项目如需重试，应重新规划 intent。
