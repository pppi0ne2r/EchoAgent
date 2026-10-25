# 六项审查问题的修复与核实

日期：2026-10-22（Asia/Shanghai）。范围是本轮复现的六项问题；只修改本地，不提交或推送 Git，不调用真实模型，不连接或重跑题目，不自动续跑现有项目。

**状态：六项均已修复并完成定向核实，宿主机服务及新 worker 镜像已加载。** 这不等于所有密码题型或 S10 真实模型验收已经通过。

## 1. 改动与结果

| 问题 | 修复文件 | 处理及核实结果 |
| --- | --- | --- |
| P1：动态源码按旧参数验证通过 | `crypto_inputs.py`、`crypto_verify.py`、密码提示词及 `container/AGENTS.md` | Python 格式仅作为参数数据：只接受 n/e/c 的唯一顶层整数字面量赋值、注释、文档字符串和 int 注解；导入、调用、函数、控制流、删除或其他赋值均返回 inconclusive。`globals()['c']=14` 等不再按旧 c=13 放行；验证版本升级 `crypto-verify-v3`，旧 v2 pass 不能用于新的完成判定。不会执行上传源码来“验证”它。 |
| P1：非法项目名提交坏记录 | `storage.py` | 自动 ID 与显式 ID 都在插入前做完整 Project 校验；空串、空格、换行名称返回 400，项目列表仍正常且没有残留行；事务中初始图写入失败也整体回滚。未提供 name 时仍自动命名。 |
| P1：旧快照覆盖新数据 | `models.py`、`storage.py`、两个客户端、服务路由及 schema | 增加 revision 比较后保存、事务写锁和每项目互斥；旧快照返回 HTTP 409，不覆盖事实，也不先执行 Docker 生命周期。后台与 CLI 保存运行状态时记录内部 run_owner，其他调度器不能释放并重跑它的 running intent；普通外部编辑须先停止。显式停止可解除所有权。 |
| P2：畸形字段抛 TypeError | `crypto_contract.py`、`dispatcher/core.py` | 枚举字段先检查类型，列表、对象、null 等转为 ValueError/400；非法 exit_reason 也进入 tool_error。模型错误结果留下诊断、结束相应 running intent，不让它因 TypeError 留在运行中。 |
| P2：修正前提后被错误去重 | 新增 `crypto_identity.py`，修改模型、契约、去重及执行前检查 | intent 保存创建时的 premise_versions，包含事实内容、类型、参数、候选、产物引用及递归依赖摘要；历史比较读取旧快照。参数、上游事实或产物引用改变后允许重算；完全相同前提仍去重。排队 intent 的前提改变后先拒绝执行并要求重新规划。 |
| P2：完成项目仍可上传附件 | `server/app.py`、`storage.py`、网页按钮 | 界面禁用完成后的上传；入口与输入登记事务均只允许 inactive/active/stoped。完成后上传返回 400，原输入清单、文件目录和 completed 状态保持不变。没有隐式重开项目。 |

## 2. 保存协议与运行所有权

- 新列：`projects.revision INTEGER NOT NULL DEFAULT 0`、`projects.run_owner TEXT`。历史项目迁移后 revision=0，run_owner=NULL；不改写原 facts/intents/crypto/executions/verifications。
- GET 项目会返回 revision；完整 PUT 必须携带读到的 revision，成功后递增。两个同时读取同一版本的写入只能有一个成功，另一方收到 409 并需重新读取。
- `ProjectClient.save_project()` 将成功响应中的新 revision 写回传入对象；`StoreDispatcherClient` 及直接存储保存也更新原对象，保持连续调度可用。
- 运行所有权由客户端生成稳定随机标识，HTTP 通过 `X-Echo-Agent-Dispatcher` 传递；run_owner 不出现在项目 JSON 和提示词中。数据库在保存事务中检查所有权，服务在 Docker 生命周期之前也做检查；同一服务进程的项目读改写及生命周期通过每项目互斥串行化。
- 不能只靠本地线程注册表防并发：另一个真实客户端即使读到了最新 running 项目，也不能将它的 intent 当成残留而恢复。专项检查确认第二个调度器在执行 worker 前被拒绝。
- 进程异常退出后若所有权仍存在，用户需要显式停止项目后重新运行，不自动抢占可能仍在执行的任务。
- 旧版外部脚本若忽略 revision 或每次保存都换一个客户端标识，需要更新。缺版本/旧版本会明确拒绝，不能通过从服务端补一个“最新版本”再盲写来绕过冲突。
- 输入登记也递增 revision；项目已删除后，旧快照不能通过 save 再次创建它。读取项目的基本信息、facts 和 intents 使用同一 SQLite 读取事务。

修复没有引入新 Python 包、远程锁服务或常驻基础设施。原渗透任务保留原工具与提示词；保存协议的冲突保护同时作用于 general 和 crypto。

## 3. 前提快照与旧数据

`premise_versions` 是由框架生成的 `fact_id -> SHA-256` 映射，模型提供的同名字段会被丢弃并重新生成。省略 input_fact_ids 时，创建 intent 会默认把 from 事实作为依赖；旧排队 intent 的执行前检查也应用这一规则。摘要会检查递归依赖，遇到环或深度超过 64 明确拒绝。

旧 intent 若没有快照，不能倒推它当时使用的事实版本；因此不拿它证明新前提已经覆盖。旧排队计算缺少必要快照时会要求重新规划，不自动用今天的参数补写历史。历史记录本身保留。

此修复处理具体的前提版本错误，不宣称解决自然语言方法别名、所有同义表达或任意数学范围的语义去重。

## 4. 核实

- 新增 `tests/test_review_six_fixes.py`，**22 项专项检查通过**，涵盖六项问题的正反例、事务回滚、旧库迁移、并行 HTTP 写入、两个真实后台客户端、实际 worker 阻塞期间的第二调度器拒绝、停止解锁、客户端连续保存、隐式 from 依赖及旧数据兼容。
- 与 S04–S09、前次契约/完成修复、原调度器/runner、HTTP 客户端、提示词及解析检查一起共 **214 项：213 通过，1 跳过**。跳过为宿主机未安装可选 AES 库，未为此安装依赖。
- **9 项真实 Docker AES 正反例通过**：新镜像、只读源码挂载、network none、256 MiB 内存；未调用模型或题目。
- 原 general 项目的 first_run/explore/reason 提示词 SHA-256 与本轮前一致；Node 语法检查及 `git diff --check` 通过。
- 未用现有项目进行坏名称、冲突保存或错误候选试验；专项检查全部使用临时数据库/样例。

主要命令：

```powershell
python -X utf8 -B -m unittest tests.test_review_six_fixes tests.test_crypto_s04 tests.test_crypto_s05 tests.test_crypto_s06 tests.test_crypto_s07 tests.test_crypto_s08 tests.test_crypto_s09 tests.test_crypto_contract_recovery tests.test_crypto_completion_closure tests.test_dispatcher tests.test_dispatcher_runner tests.test_client tests.test_prompts tests.test_parsing -q
node --check echo_agent/server/static/app.js
```

## 5. 部署与数据核对

- 部署前确认后台无运行中的调度任务，没有停止或续跑题目。核对原服务身份与 8000 端口归属后，停止旧 PID 14732；首次加载 PID 64668，补齐隐式 from 依赖保护后最终 PID **67292**，再次健康及旧数据摘要核对通过。
- 服务继续使用原 Conda `echoagent`、本地配置和 `.echo_agent/echo_agent.db`，健康检查 `{"ok":true}`，认证读取正常。新 revision 字段已从实际服务返回，run_owner 未对外输出。
- 更新前备份：`.echo_agent/backups/echo_agent-pre-review-six-20261022.db`；停服后冻结备份：`.echo_agent/backups/echo_agent-pre-review-six-frozen-20261022.db`。备份及实际数据库 quick_check 均为 ok。
- 重启前后均为 **43 projects、529 facts、388 intents、32816 project_logs**；不仅比较数量，还对四张业务表的原有列按固定排序计算摘要，迁移前后完全一致。新增两列不纳入旧数据摘要。证据 `.echo_agent/runtime/review-six-pre-load.json` 和 `review-six-deployment.json`。
- 本轮前的主要工作区文件在 `.echo_agent/backups/review-six-before-20261022/`；未重置其他已有本地改动。
- 新镜像仍为 `echo-agent-worker:crypto-20261022`，ID **`ec40281471b7e7f35b8986a5f60820951d879b32760e445aa8202ffcd2795250`**；镜像指引与本地 SHA-256 均为 `3b6599d704fc8de90eb07a1e351f48b5ff5f6f5f184fa833470900ad316cb861`。
- 构建前的镜像保留为 `echo-agent-worker:pre-review-six-20261022-1fbdc8b4e6d9`。未删除、重建现有项目容器；后续新建容器采用新镜像。
- 服务日志：`.echo_agent/runtime/server-review-six-20261022.stdout.log`、`.stderr.log`；PID 文件同名前缀 `.pid`。

本轮没有修改历史已完成项目的结论或补写旧前提快照。更多密码题型、统一验证取消/资源预算及 S10 真实模型验收仍为后续工作。
