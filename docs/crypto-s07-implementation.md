# S07：参数化去重与继续计算

状态：2026-10-21 已核实通过（固定输出与定向回归）。仅修改本地，未提交或推送远端；未用真实模型跑题。

## 已实现

- 密码 intent 单独按 `method`、输入 ID 与创建时的 SHA-256/大小快照、实际依赖的 fact 内容、`params` 以及 `scope` 约束判断计算身份。`description`、`action_key` 和图上的 `from` 不再决定密码计算是否重复；普通 `general` 项目仍走原有去重分支。typed 整数按数值规范化，typed bytes 保留字节内容和长度。
- `scope.start/end` 为整数或 typed 整数时，按闭区间处理。已完成或范围穷尽的执行只按实际 `covered_scope` 扣除；扩大范围会拆成尚未覆盖的片段，记录原始请求范围与已有 intent ID。相邻范围可分别执行。其他形状的 scope 只进行精确比较，不推测数学上的包含关系。
- 同一范围的失败/超时不能只换描述或标签重启。允许使用上一执行记录的 `checkpoint_ref` 作为 `resume_from`，或明确提高 `budget.wall_seconds`、`memory_mb`、`output_bytes` 中至少一项。新检查点必须与刚用过的检查点有不同的哈希/大小；无预算增长且重复相同检查点不能无限循环。续算依据保存在新 intent 的 `crypto.continuation`。这些预算的硬上限由 S08 实施。
- 同一范围最多规划 3 次尝试（初次和最多两次有依据的续算）。仅扩大失败搜索的边界、却未提高预算或提供检查点时，新计划只保留旧计划之外的区间；旧区间仍属未证明，不会被写成数学排除结论。
- worker 声明的 `crypto.checkpoint_ref` 经结构、所属 intent 检查后进入 `project.crypto.executions`；reasoner 可以据此规划续算。超时/unknown/工具错误的 `covered_scope` 只作为执行进度记录，不被当成已证明穷尽的区间。新增 fact 若改变实际声明的 `input_fact_ids` 前提，可以重新评估；旧结论和其覆盖范围保留。
- `input_fact_ids` 现在表示计算真正依赖的事实；`from` 只是图的边。reasoner 显式提供该列表时不再强制插入 `from`。旧输出若未提供该字段，仍以 `from` 作为保守默认。

## 核实

`tests.test_crypto_s07` 的 12 个定向用例覆盖跨起点/改写描述去重、参数和输入版本变更、前提变化、相邻与扩展区间、超时与检查点续算、重复检查点阻断、有限重试、执行时有效预算比较、worker 执行记录到下一计划，以及固定 reasoner 输出进入调度图。S04–S06 和原 Web 调度相关类一并回归；本次未运行旧测试全集、未访问现有任务数据库。

## 边界与后续

S08 已补充检查点实际文件字节核验、执行预算和进程清理，详见 [S08 实施记录](crypto-s08-implementation.md)；S09 负责独立答案验证。旧 S07 之前创建且无 `input_versions` 快照的 intent，只能按当前输入清单推断版本；已有上传链路通常为新输入分配独立 ID。真实模型是否稳定输出正确的结构化前提、范围与预算仍需 S10 端到端核实。
