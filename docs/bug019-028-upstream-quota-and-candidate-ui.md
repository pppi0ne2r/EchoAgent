# 上游配额错误修复 + Web 候选/验证面板（2026-10-24）

对应台账：BUG-019、BUG-028（合并修复）；Web UI 候选与验证状态展示。

## 一、BUG-019 / BUG-028：上游余额/鉴权错误被误报并空转重试

### 问题

上游返回 402 `Payment Required: 余额不足` 或 403 `{"code":"INSUFFICIENT_BALANCE"}` 时：

1. 错误不匹配 `TRANSIENT_UPSTREAM_MARKERS`，进入通用失败路径；
2. 非零退出时 salvage 扫描 stdout/stderr，从 codex 回显的 prompt 里拿到模板 JSON；
3. reason 侧报 `reason output only contains prompt template intents`，按普通失败重试 3 次；
4. 最终停机，但日志把排查方向引向 prompt/模型，而不是账户余额。

### 修复

`echo_agent/dispatcher/agents.py`

- 新增 `FATAL_UPSTREAM_MARKERS` 与 `fatal_upstream_marker()`，只收录无歧义的账单/凭据短语：
  `insufficient_balance`、`insufficient balance`、`insufficient account balance`、`payment required`、`余额不足`、`invalid api key`、`invalid_api_key`、`incorrect api key`。
  **不收录**裸 `401` / `403` / `status 402`：靶机 HTTP 响应也可能出现这些状态码，不能因此终止整个运行。
- 检测顺序在 **salvage 之前**，命中后抛专用 `UpstreamFatalError`，不进入 salvage、不进入瞬时故障重试。
- 检测优先使用 stderr（codex 自身的错误流），仅当 stderr 为空时才检查 stdout，避免把模型输出里提到的 "insufficient balance" 当成上游错误。
- error 级日志与异常消息保留上游原文（压缩空白后截断 1200 字符）。

`echo_agent/dispatcher/core.py`

- `Dispatcher._fatal_upstream_error` 记录首次致命错误；`_is_retryable_failure()` 对 `UpstreamFatalError` 恒为 `False`。
- explore / first_run：`_execute_with_retry()` 捕获后记录诊断、intent 记 `error`，步骤结束时返回 `DispatchMode.SKIPPED`。
- reason：`_reason_fatal_upstream()` 直接返回 `SKIPPED`，**不增加** `_reason_failures` / `_reason_format_failures`，消息明确写「上游配额/鉴权错误，请充值或切换供应商」。
- 后续步骤（同一 dispatcher 实例）在入口直接返回 `SKIPPED`，`run_loop` 随即停止。
- `_run_project_step` 入口、`_first_run` 与 `_explore` 返回前都检查致命标志。

`echo_agent/dispatcher/parsing.py`

- 模板过滤异常附带原始输出片段（截断 400 字符），避免再次出现「只有模板 intent」而看不到真实原因。

### 验证

新增 `tests/test_bug028_upstream_quota.py`（10 项）：

- 403 `INSUFFICIENT_BALANCE` / 402 `Payment Required` / 401 `invalid api key` / `余额不足` 均识别为致命；
- 普通 403、裸 402、rate limit、service unavailable 不误判；
- stdout 里模型提到 "insufficient balance" 但 stderr 是普通错误时不判致命；
- 进程级：stdout 带模板 JSON、stderr 带 403 余额错误时抛 `UpstreamFatalError`，**不 salvage**；
- explore 路径：`run_once` 返回 `SKIPPED`、日志含 quota/auth、项目退出 `running`；
- reason 路径：`SKIPPED` 且 `_reason_failures` / `_reason_format_failures` 均为 0；
- 普通失败（`RuntimeError`）不触发致命停止；致命后下一步直接 `SKIPPED`。

### 未覆盖

- 超时/无输出分支中的上游错误未做致命判定（只覆盖非零退出路径）。
- 未用真实余额为 0 的 Key 复现；行为由进程级模拟验证。

## 二、Web UI：候选与验证状态面板

### 改动

`echo_agent/server/static/index.html`、`app.js`、`styles.css`

crypto 项目详情新增「候选与验证」区块，展示：

- 完成目标摘要：`answer_kind`、`answer_form`、`answer_hash`、`verification_mode`、`solution_policy`；
- 每个候选：fact 编号、`answer`、`scheme`、`answer_kind`、`m`、验证方法/结果、回执操作者与时间；
- 验证状态徽章：平台已接受 / 验证通过 / 验证失败 / 无法验证 / 未验证；
- 验证 `checks` 中的关键信息：恢复出的明文（UTF-8 可解码时同时显示文本与 hex）、期望哈希与算法、失败原因；
- 验证执行信息（R06 起）：后端（host/container）、耗时、非正常终止原因（timeout/cancelled）；
- 无可解码明文时只显示 hex，不猜测内容。

数据直接来自 `GET /projects/{id}` 的 `facts` 与 `crypto.verifications` / `crypto.acceptances`，不新增 API。general 项目隐藏该区块。

### 验证

- 用临时 HTTP 服务注入一个 crypto 项目（候选 answer 为 md5、验证 `fail`、`checks.preimage_hex` 为明文 hex），Chrome 无头加载真实 `index.html` + `app.js`，确认面板渲染出目标摘要、候选、明文、期望哈希与失败原因。
- `app.js` 语法由 Chrome 无头执行检查（函数定义存在、无 SyntaxError）。

### 未覆盖 / 限制

- 只展示每个候选的**最后一条**验证记录与回执，不提供历史列表。
- 没有在 UI 上新增「接受回执 / 重新验证」操作，仍需调用 API；本次范围是展示。
- 验证 `checks` 中的长字段按 200/300 字符截断显示。
