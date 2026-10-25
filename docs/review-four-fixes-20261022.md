# 后续审查：项目身份、停止、检查点与取消修复

日期：2026-10-22（Asia/Shanghai）。仅修改本地，未提交、未推送远端。没有运行真实模型或现有题目。

## 1. 已修复问题

### 删除项目后的身份复用

原来 `projects.id` 使用 SQLite 隐式分配，删除最大编号后，新项目可以复用旧编号，且 `revision` 又从 0 开始。旧快照因此可能通过版本比较，覆盖新项目。

- `Project` 新增 `project_uid`，由存储层生成永久随机身份；更新时同时比较身份和 revision。缺失或不匹配返回 409，且在 Docker 生命周期操作前拒绝。
- 新增 `project_ids` 编号登记表，删除项目保留登记；自动编号不会复用已经登记的旧编号，显式请求旧编号也拒绝，防止复用原输入和产物目录。
- 旧数据库在初始化时为现有项目补身份并登记编号，保留原编号、revision、状态和图数据。再次初始化不会改变身份。
- 模型序列化、HTTP 保存 schema、HTTP 客户端和后台存储路径同步支持身份。旧客户端或旧页面快照需要重新 GET 后再保存；不自动补身份来放行未知来源的旧快照。
- 编号登记和项目创建在同一事务中；失败的创建不占用编号。旧版本部署前已经删除、且没有登记的编号不能从数据库中还原其历史；身份校验仍拒绝旧快照。

代码：`echo_agent/models.py`、`storage.py`、`server/schemas.py`、`dispatcher/client.py`。

### 状态 API 停止后后台自动恢复

原来 `PATCH /projects/{id}/status` 设置 `stoped`，却没有通知后台线程。默认持续运行时，后台下一轮会再次认领项目并执行 intent。

- 状态 API 停止统一调用 `DispatcherRunner.stop`，与网页停止入口一样设置停止事件、取消运行中的 agent、等待线程和释放项目。
- 等待线程在项目写锁之外进行，避免线程收尾保存时互相等待。
- 保留不合法状态转换检查；重复停止已停止项目可以安全返回。
- 后台释放沿用运行时的统一状态转换方法。HTTP 客户端两种停止请求使用 45 秒超时且不自动重试，给取消和容器收尾留出时间。

代码：`echo_agent/server/app.py`、`server/dispatcher_runner.py`、`dispatcher/client.py`。

本轮针对已复现的服务内后台线程停止路径。独立验证器的子进程取消、跨进程统一调度仍不属于本轮完成范围。

### 检查点未绑定原计算条件

原来只检查文件大小、哈希和生产者是否存在；前提从 n=55 改为 n=77 后，仍能携带原检查点进入 worker。

- 规划和执行前均检查 `resume_from` 必须来自失败生产 intent 的实际执行记录。
- 校验生产任务的输入版本、前提快照、方法、参数、范围及验证上下文与当前任务兼容；旧记录没有完整输入/前提快照时拒绝续跑。
- 文件字节仍在执行前重验。通过直接创建 intent 绕过规划，也不能使用不兼容检查点。
- 兼容范围内的整数/typed 整数范围表示可以互换。条件或范围变化时从头规划，不携带旧检查点；已有脚本仍可以作为普通产物引用供重新执行。
- 保留已有最多三次同范围尝试、预算增加和重复检查点防循环规则。
- 仅在密码 reason 提示中补充约定，不改变普通渗透提示词。

代码：`echo_agent/crypto_dedup.py`、`dispatcher/core.py`、`dispatcher/prompts.py`。

### 自动重试遗漏取消检查

原来首次执行后会处理停止信号，但自动重试直接返回 worker 结果，可能在停止后仍记录 completed / verified。

- 首次执行和重试共用 `_execute_attempt`，每次执行前重新检查停止信号、前提和检查点，执行后统一处理取消。
- 取消时执行记录使用 `cancelled`，intent 保持错误状态，不能生成已穷尽的完成记录。
- 已取消的 `verify.candidate` 不写入新的通过记录，不标记验证成功。

代码：`echo_agent/dispatcher/core.py`。

## 2. 验证

新增 `tests/test_review_four_fixes.py`，16 个测试方法，包含多种状态、任务类型和参数变更的子用例。覆盖删除后重建、同版本不同身份、迁移重复执行、事务回滚、两条停止接口实际驱动后台循环、检查点正反例及真实字节、取消后的重试与验证。

最终综合回归：**230 项，229 通过、1 跳过、无失败**。跳过项为宿主机可选 AES 依赖检查。本轮没有改变 AES 数学检查或容器依赖，没有重跑真实 Docker 解题。

```powershell
python -X utf8 -B -m unittest tests.test_review_four_fixes tests.test_review_six_fixes tests.test_crypto_s04 tests.test_crypto_s05 tests.test_crypto_s06 tests.test_crypto_s07 tests.test_crypto_s08 tests.test_crypto_s09 tests.test_crypto_contract_recovery tests.test_crypto_completion_closure tests.test_dispatcher tests.test_dispatcher_runner tests.test_client tests.test_prompts tests.test_parsing -q
```

普通项目 first_run、explore、reason 提示词与本轮前版本逐字比较一致；原普通渗透调度相关测试通过。`git diff --check` 通过。

## 3. 本地加载和数据保护

- 重启前逐项查询 43 个项目的服务内调度状态，均无活跃调度；核实 8000 端口归属后停止旧服务 PID 67292。没有运行、停止或续跑现有题目。
- 停服冻结备份：`.echo_agent/backups/echo_agent-pre-review-four-frozen-20261022.db`。本轮前代码保存在 `.echo_agent/backups/review-four-before-20261022/`。
- 新服务 PID **69900**，地址 `http://127.0.0.1:8000`。健康、认证读取、无令牌拒绝、43 个不同项目身份检查均通过。
- `projects / facts / intents / project_logs` 分别保留 **43 / 529 / 388 / 32816** 条记录；按原有列、固定顺序计算的摘要逐表一致，数据库 quick_check 为 ok。身份迁移新增 43 个 UID 和 43 条编号登记，不改原状态或历史结论。
- 数据库中历史 status=running 不代表本地后台线程仍在运行；本轮没有顺便清理这些历史状态。
- 此轮只改宿主机 Python 服务及宿主机注入的提示词；Dockerfile、镜像内工具、依赖和 worker 指引没有本轮新增改动，**无需重建镜像**。当前镜像保持 `echo-agent-worker:crypto-20261022`，ID `ec40281471b7e7f35b8986a5f60820951d879b32760e445aa8202ffcd2795250`；现有项目容器未重建。
- 证据：`.echo_agent/runtime/review-four-pre-load.json`、`review-four-idle-check.json`、`review-four-deployment.json`。服务日志和 PID 文件前缀为 `server-review-four-20261022`。

S10 真实模型验收仍未完成。本轮结果不等同于所有密码题型或任意检查点文件内部语义已经得到自动验证。
