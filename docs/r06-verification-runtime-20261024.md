# R06 修复：独立验证的期限、取消、并发与记录（2026-10-24）

对应复审项：`docs/crypto-s01-s09-review.md` → **R06**（预算主要约束求解 worker，验证链路仍有独立风险）。

## 问题回顾

- dispatcher 的其他 HTTP 调用 10s、验证请求 45s、AES 容器 30s，服务端计算**无期限**；客户端超时后服务端继续算完并丢弃响应。
- 停止项目只设置 `_stop_event`，`verify_candidate` 内部不检查；最长要等 HTTP 超时才释放计算槽。
- `_crypto_compute_slots` 只是单个 Dispatcher 实例内的信号量；多个项目或直接 API 可并发跑多个 AES 验证容器。
- 验证记录没有后端、耗时、终止原因；完成门槛 `_strict_pass` 每次都会重算验证。

## 改动

### 新模块 `echo_agent/crypto_verification.py`

- `VerificationRegistry`：进程级全局并发上限（默认 2）+ 每项目互斥（1）+ `verification_id → Event` 取消句柄。
- `VerificationLease`：`deadline`、`cancel_event`、`timeout_seconds`。
- `verification_scope()` / `check_verification()`：线程局部上下文；验证器内部循环无需改签名即可响应取消与期限。
- 常量：`DEFAULT_VERIFY_SECONDS = 60`、`MAX_VERIFY_SECONDS = 300`。

### `crypto_verify.py`

- `verify_candidate()` 新增参数：`deadline`、`cancel_event`、`verification_id`、`timeout_seconds`。
- 耗时段检查 `check_verification()`：输入读取、artifact 哈希、RSA 素性判定每轮、ECDSA 点乘每轮、小模数 RSA 根穷举每 256 次、scheme 分派前后。
- 记录新增字段：`verification_id`、`timeout_seconds`、`backend`（host/container）、`elapsed_ms`、`terminated_reason`（completed/timeout/cancelled/error）。
- Web UI 的「候选与验证」面板同步展示后端、耗时与非正常终止原因。
- 超时 → `status=inconclusive` + `terminated_reason=timeout`；取消 → `cancelled`。不虚报 pass/fail。
- AES 容器验证从 `subprocess.run(timeout=30)` 改为 `Popen` + 50ms 轮询：可被取消/期限 kill，stdin 由独立线程写入，保留 30s 本地上限；终止后清理容器。

### `crypto_files.py`

- `verify_ref()` 的分块哈希循环调用 `check_verification()`，大 artifact 可被期限/取消中断。

### `crypto_service.py`

- `verify()` 接收 `verification_id` / `timeout_seconds`，通过 registry 获取租约，`finally` 释放。
- `cancel()` 转发到 registry。
- 保存路径的完成闸门改用 `_verify_for_save()`：保留 60s 期限，但**不占 registry 槽**，避免后台保存与手动验证互相 409。

### HTTP 与客户端

- `POST /projects/{id}/candidates/{cid}/verify?verification_id=...&timeout_seconds=...`
- `POST /projects/{id}/verifications/{verification_id}/cancel` → `{"cancelled": bool}`
- 并发冲突返回 **409**（verify 路由与 PUT/PATCH 保存路由均映射）。
- `ProjectClient.verify_crypto_candidate()` 发送 id/期限，HTTP 超时 = 期限 + 5s（默认 65s，原 45s）；新增 `cancel_crypto_verification()`。
- `StoreDispatcherClient` 同步支持新参数与取消。

### Dispatcher

- `Dispatcher` 记录 `_active_verifications`（project → verification_id）。
- `verify.candidate` 执行前登记、执行后清除；`request_stop()` 取消进行中的验证（best-effort，失败只记 warning）。
- 期限来源：intent `crypto.budget.wall_seconds` > 项目 `crypto.default_budget.wall_seconds` > 默认 60s；上限 300s。
- 对没有新参数的旧客户端/测试替身，`callable_accepts_mode` 检测后回退到旧调用。

## 兼容性

- 客户端验证超时从 45s 改为 65s（`DEFAULT_VERIFY_SECONDS + 5`），既有测试已同步。
- 验证记录新增字段不参与 `verification_matches()` 的严格比较；旧记录仍可读取。
- 当前 `VERSION` 仍是 `crypto-verify-v6`（本次未改变验证语义），旧 v5 记录在 BUG-030 起已失效。
- 保存路径的完成闸门不再占用 registry，不会被手动验证阻塞。

## 验证

新增 `tests/test_verification_runtime.py` 13 项：

- registry：同项目互斥、全局上限、取消事件、超时参数边界、释放后可复用；
- 记录：正常验证的 backend/elapsed/terminated_reason、超期 deadline、已取消事件、service 租约释放；
- client：query 携带 id/期限、默认期限与 HTTP 超时、取消端点路径；
- HTTP：verify 接受 query 并返回 id/期限、cancel 端点、并发冲突 409；
- dispatcher：`request_stop()` 取消进行中的验证（阻塞式 fake client）；
- 预算优先级与 300s 上限。

全量 **354 项**：仅 5 项既有平台性失败（4 项 PATH `python` 商店桩、1 项 NTFS `chmod` 无效），与改动前基线一致。

## 未覆盖 / 限制

- **未做验证结果缓存**：完成门槛 `_strict_pass` 仍会对每条 pass 记录重算；一次完成流程可能验证 2–3 次。可选缓存（按候选/目标/版本/输入哈希）留待后续。
- 未实测真实慢验证的取消（单测用阻塞 fake client 验证取消链路）；AES 容器的 Popen 取消路径需 Docker 环境实跑。
- 并发上限是**进程内**的：HTTP 服务与后台 runner 共享默认 registry，但多个独立进程（如另起 uvicorn、直接调用库）各自上限。
- 验证容器被强杀后的容器清理依赖 `docker rm -f`，未在 Docker 实机验证。

## 使用

- 手动验证可带期限：`POST /projects/7/candidates/8/verify?verification_id=my-check&timeout_seconds=30`。
- 取消：`POST /projects/7/verifications/my-check/cancel`。
- dispatcher 的验证期限由 intent 预算或项目默认预算决定；停止项目会自动取消进行中的验证。
