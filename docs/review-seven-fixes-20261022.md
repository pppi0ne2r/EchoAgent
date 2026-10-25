# 七项审查问题的修复与核实

日期：2026-10-22（Asia/Shanghai）。范围是本次静态审查报告的七项问题；只修改工作区，不提交或推送 Git，不调用真实模型，不连接或重跑题目，不重建镜像、不重启服务、不改 `echo_agent.toml` 与数据库。

**状态：七项均已修复。** 新增 20 项定向检查；全套 `unittest discover` 共 285 项，279 通过、1 跳过、5 项为已知 Windows 环境问题（见第 3 节）。这不等于 S10 真实模型验收或 Docker 端到端回归已完成。

## 1. 改动与结果

| 问题 | 修复文件 | 处理 |
| --- | --- | --- |
| P1：`crypto.fact.confirmed` 契约缺失，超时末次观察分支不可达 | `dispatcher/prompts.py`、`container/AGENTS.md` | 保留原代码门槛（设计文档已规定），在共享密码契约规则、first_run/explore 提示词与 worker 指引中要求：实际执行并核对过的观察写 `claim_status=observed` + `confirmed=true`；超时/预算耗尽/错误/取消时只保留这类末次观察。first_run 示例补充 `confirmed:true`。 |
| P1：fail/inconclusive 独立校验记录被丢弃，note 写成 completed | `dispatcher/core.py` | 提前返回只拦 `exit_reason != "completed"`（取消的校验仍不落记录）；结构合法的 pass/fail/inconclusive 记录一律写入 `project.crypto["verifications"]`，再按实际 status 落 intent 状态；fail/inconclusive 的 note 写出真实 status 与 checks（截断 600 字符）。 |
| P1：`run()` 的 finally 里容器重置失败顶掉成功结果/超时分类 | `dispatcher/agents.py` | 重置失败改为 warning 日志，不再替换已算出的 `AgentRunResult` 或 `CryptoExecutionLimit`；嵌套 `finally` 保证 `crypto_budget` 复位；`_reset_crypto_container` 用 `finally` 清空 preflight 缓存。 |
| P1：`cancel_running` 的 docker stop 失败打断停止 API，所有权卡死 | `dispatcher/agents.py`、`server/dispatcher_runner.py` | 取消改为逐容器 best-effort，失败时清 preflight 后继续；`DispatcherRunner.stop` 用 `try/finally` 保证 `_release_idle_project` 执行；强制停止失败记录 error 日志但不中断释放。 |
| P2：无结构引用的 crypto 观察绕过上下文预算 | `dispatcher/prompts.py`、`docs/crypto-s06-implementation.md` | 新增 `_EVIDENCE_CRYPTO_OLDER_CHARS = 600`：最近 40 条仍全量；更早的、无 `typed_value`/`artifact_refs` 的密码事实按 600 字符截断，有结构引用仍按 160。设计说明同步更新。 |
| P2：crypto 示例占位串未过滤，回声会变成真实证据 | `dispatcher/parsing.py` | `"示例事实；不要照抄"` 加入 `placeholder_facts` 与 `strip_prompt_template_text` 的字面量清理表；省略可选 `template` 的回声不再产生 fact。 |
| P2：超时异常的 output_tail 参与重试判定 | `dispatcher/core.py` | `_is_retryable_failure` 先按 `output_tail` 标记截断，只对异常消息本身分类；`_execution_failed_fact` 复用同一常量，避免标记硬编码。 |

## 2. 行为边界

- 独立校验的 `fail`/`inconclusive` 现在会进入规划上下文，但完成门槛不变：`answer_is_verified` 只接受当前目标与候选下重新核验通过的 `pass` 记录；新增的失败记录不会让旧 pass 自动失效，也不会放宽完成。
- `confirmed` 只是模型对“实际执行并核对过”的显式声明，不是数学证明；非超时路径仍只要求 `claim_status=observed`，与 S06 既有语义一致。
- crypto 原始观察截断后，完整 fact 仍可通过上下文的 `/projects/{id}/facts/{fact_id}` 取回；本次没有为提示词新增读取工具。
- 停止路径的修复只覆盖“docker stop/restart 失败”：`_release_idle_project_locked` 中 Docker 生命周期失败仍会报错，避免把 daemon 整体不可用伪装成已停止。
- 宿主机提示词改动需重启服务才生效；`container/AGENTS.md` 改动需重建 worker 镜像。本次均未执行。

## 3. 核实

```bash
"C:/code/miniconda/envs/echoagent/python.exe" -X utf8 -B -m unittest tests.test_review_seven_fixes -v
"C:/code/miniconda/envs/echoagent/python.exe" -X utf8 -B -m unittest discover -s tests
"C:/code/miniconda/envs/echoagent/python.exe" -X utf8 -B -m compileall -q echo_agent
git diff --check
```

- 新增 `tests/test_review_seven_fixes.py` 共 20 项：confirmed 契约与超时行为、fail/inconclusive/pass/取消的校验记录、重置失败不替换结果、取消失败不抛错、停止释放、crypto 证据截断、示例回声过滤、output_tail 重试分类。
- 全套 285 项：279 通过、1 跳过、5 项失败均为 Windows 环境原因，与本次改动无关：4 项 `test_agents` 用例调用 `python3` 命中商店桩（退出码 9009），1 项 `chmod +x` 在 NTFS 上无效。
- 未运行 `tests/*_docker_smoke.py`（需要 Docker/网络，不属于 `test_*` 发现范围）。

## 4. 未做

- 未重建镜像、未重启服务、未续跑现有项目、未修改数据库或配置。
- 未修 `docker_runtime` 的 `"No such object"` 大小写匹配问题（CLAUDE.md 已记录的独立改动）。
- 未改 HTTP 客户端超时、状态机、存储结构或网页 UI。
