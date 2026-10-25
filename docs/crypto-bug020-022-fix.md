# BUG-020～022 本地修复与核实

日期：2026-10-22（Asia/Shanghai）。范围为项目 #42 暴露的产物引用、字节参数契约和 reason 边界问题；没有自动重跑题目、调用真实模型或同步远端 Git。

## 实现

### BUG-020：产物的读写路径与持久引用

`dispatcher/prompts.py` 新增共用的精确契约说明。first_run、explore、reason 均给出 integer、bytes 和 artifact_ref 的字段规范；执行提示还明确当前 intent 的两个目录：

- 容器实际读写：`/home/kali/workspace/intents/<id>/<file>`。
- JSON 持久引用：`artifacts/intents/<id>/<file>`。

明确 `producer_intent_id` 是整数，不能用 `producer="intent-0"` 代替；哈希、大小必须来自真实文件。`crypto_contract.artifact_ref()` 对错误路径增加规范提示，绝对路径、`..` 和生产者不匹配仍被拒绝，没有自动改写或放开路径检查。`container/AGENTS.md` 同步说明。

原报告将 `_crypto_worker_inputs()` 的输入映射视为产物问题的直接根因，需收窄：它只处理 `inputs`，本次没有修改该函数；修复的是模型与持久引用之间缺失的路径交接说明。

### BUG-021：字段格式与可恢复的规划拒绝

统一给出完整示例 `{"type":"bytes","encoding":"hex","value":"0007","length":2}`，明确 `value`、`encoding`、字节长度和前导零要求。输入 SHA-256 已在清单中时，可直接保留为普通 64 位字符串，不要求重复包装 typed bytes。

`_typed_values()` 的错误现在携带字段位置，例如 `crypto.params.input_sha256`，同时给出正确字节格式；仍拒绝错误长度、错误字段和浮点数，不接受 `hex` 别名或静默猜测数据。

密码规划拒绝现在：

1. 记录具体 action_key 和校验原因的 warning。
2. 将最新一轮最多 5 条、每条最长 600 字符的拒绝诊断保存在现有 `Project.crypto.planning_feedback` 中，下一次 reason 可见；不会写成数学 fact。没有新增 SQLite 列。
3. 合法计划照常保留，混合轮次不会整批丢弃。
4. 若整轮没有合法计划且存在密码契约错误，使用独立纠错计数，默认最多 3 轮（沿用 `max_retryable_attempts`）；不增加“无新方向”计数。超过上限以 `crypto planning contract errors persisted` 停止，而不是声称数学上无进展。
5. 格式纠错轮不消耗正常调度步预算，但受独立次数上限约束。成功生成计划后清除旧诊断及纠错计数；未知输入/事实 ID 在去重前给出校验错误，避免 KeyError。

### BUG-022：稳定方法名与规划执行边界

普通 `crypto.method` 明确为自由命名的稳定动作标识，不需要查找内置算法词表；`verify.candidate` 是框架保留方法。reason 采用单独的会话继承表述，规划读取/采样/保存工作，不再复用面向执行者的直接操作指令。

仅在 `task_type=crypto` 且 `mode=reason` 时，Docker Codex 命令：

- 移除原有 bypass、full-auto 和写入沙箱选项，设置 `--sandbox read-only`。
- 禁用 `features.shell_tool`、`features.unified_exec`、`features.multi_agent`、`features.apps` 和 web search。
- 保留原 general、crypto first_run/explore 的执行能力和参数。

现有 Codex 0.118.0 的实际请求工具清单已在禁网临时容器内核对：原有 `exec_command`、`write_stdin` 被移除；仍公布 `apply_patch`、`view_image`、`request_user_input`、`update_plan` 等辅助工具。因此这里只声称 shell 已禁用及只读沙箱已配置，不将其描述为所有工具消失或完整恶意代码隔离。尝试的图片工具开关在该版本没有实际效果，未保留该无效配置。

## 核实结果

- 新增 `tests/test_crypto_contract_recovery.py`，11 项通过，覆盖错误反馈、下一轮修正、同 action_key 接续、有限纠错、真实空规划停止、混合计划、未知输入、临时 SQLite 往返、路径边界和模式隔离。
- 与 S04–S09、原 dispatcher、后台 runner 和现有 agent 命令检查一起运行共 **138 项：137 通过，1 项可选宿主机 AES 依赖检查跳过**。没有安装依赖或重跑旧测试全集。
- 只读读取项目 #42 的日志 `30786/30873/30952`：原始计划仍被严格拒绝，但错误明确定位到 expected_sha256/input_sha256；仅在内存副本修正字节键名后，三条均通过 `intent_crypto()`。没有改写历史计划、事实或日志，也不表示这些方法已经执行成功。
- `tests/crypto_reason_tools_smoke.py` 使用容器内 loopback 假接口抓取 Codex 实际工具清单；接口主动返回 400，未接触真实模型、代理或题目。带 `--network none` 的临时容器检查通过并自动删除。
- 固定 general 项目三个模式的提示词 SHA-256 与修复前完全相同。`git diff --check` 通过。

## 加载与数据保留

- 重建 `echo-agent-worker:crypto-s03`，新镜像 ID 为 `sha256:688048bc2f648e6cdd673b1969fc1dbb0470f4584a67c8f5c0cb01a5931b4286`，旧版本保留为 `echo-agent-worker:pre-contract-fix-20261022-fbc9745fcadc`。
- 新镜像内 worker 指引 SHA-256 为 `a881449550b789af86e6b41b2ce05dd105468f6595735789721ea8fa7e3a9021`，与本地文件一致。
- 确认没有活动 dispatcher 后，备份原数据库至 `.echo_agent/backups/echo_agent-pre-contract-fix-20261022-122903.db`，备份检查为 `ok`。
- 重新加载服务，初始 PID `26836`，`http://127.0.0.1:8000/health` 返回 `{"ok":true}`。日志为 `.echo_agent/runtime/server-contract-fix-20261022.stdout.log` 和同名 `.stderr.log`。
- 加载前后均为 42 项目、523 facts、375 intents、30723 条 project_logs；没有修改旧项目图、自动续跑 #42 或重建旧项目容器。新宿主机提示词和命令策略已生效，新建容器使用新镜像；旧容器内的 AGENTS 副本仍是旧版本。

## 尚未证明或不在本次范围

BUG-020～022 的本地实现和定向检查已完成，真实模型是否稳定遵守新契约、#42 是否能完成题目仍待另行回归。本次没有修复 S01–S09 复审中的后台候选验证方法遗漏、目标/多解约定、验证器题型覆盖等其他问题，不将结果写成 S09/S10 整体验收通过。

同日后续运行环境变更：用户另行授权后，当前服务已切换为 `echo-agent-worker:crypto-20261022`；此前镜像标签及 #40/#41/#42 的旧容器已在完整归档后删除，历史回退标签不再保留在 Docker 中。原项目数据仍在，恢复归档及当前镜像说明见 [切换与清理记录](crypto-image-switch-20261022.md)。
