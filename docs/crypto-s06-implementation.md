# S06：密码规划与事实语义

状态：2026-10-21 固定输出数据流已有核实记录；2026-10-22 补充跨 intent 思路继承与重连取数提示词。只改本地，未同步远端；本次仅做离线提示词检查，真实模型及 nc 端到端试跑尚未进行。

## 改动

- `PromptBuilder` 只对 `task_type=crypto` 使用密码专用 first_run、explore、reason 指引。first_run 核对输入原始字节、哈希、基数和长度，提取 typed 参数并做低成本检查；explore 限于当前 intent 的输入、参数与范围；reason 从证据和实际覆盖范围规划结构化 intent。普通 Web/渗透任务仍使用原提示词。
- worker JSON 的 `crypto` 对象贯通解析、`WorkerResult`、fact 和项目执行记录。可记录 `exit_reason`、`covered_scope`、已确认 fact 的 `typed_value`、`source_input_ids` 与产物引用。reason 的 `next_intents[].crypto` 不再被解析器丢弃，需包含 `method` 和非空 `scope` 才进入密码项目图；无结构或引用无效的计划被跳过。
- `gcd(n1,n2)=1` 一类负观察可设 `outcome=refuted`、`crypto.fact.observation_kind=evidence`：intent 的共享因子假设被否定，而等式仍作为可复用 `EVIDENCE` fact。反驳结论附实际 `covered_scope`；`range_exhausted` 缺少范围会被视为错误。
- `timeout`、`solver_unknown`、`tool_error`、`cancelled` 均令 intent 保持错误/未证明语义，不生成“全局无解”结论。此前单独输出的已确认观察仍能保存；当前输出若要作为超时前的部分事实，需显式声明 `crypto.fact.confirmed=true`。后续观察元数据无效时也不丢弃先前有效 fact。显式标为 `hypothesis` 或 `candidate` 的内容不写入 verified fact。
- 密码项目的长历史 evidence 摘要可截断，但保留完整 `crypto` typed 值与产物引用；无结构引用的旧密码 fact 不再保留全文，而是按更大的字符上限（600）截断，避免提示词随运行无限增长。上下文给出稳定的 `/projects/{id}/facts/{fact_id}`，API 可取回完整 fact；产物引用同时给出 worker 中的可读路径。执行记录随项目上下文提供给 reasoner。
- 密码项目在 S09 独立验证接入前不会被 reason 的 `completed=true` 自动标为完成；模型给出候选时会规划原始输入上的验证动作。现有通用项目的完成逻辑不变。

## 固定 RSA 数据流核实

用固定 worker 输出核对 `n=55`、`e=3` 后，两个 fact 的 typed 十进制字符串、输入来源和 producer intent 均经调度器保存。固定 reasoner 规划 `factor.trial`，带 `n` typed 参数及 `2..10` 范围；范围穷尽且未找到因子时，实际范围写入执行记录，负观察为 evidence，当前假设 intent 为 refuted。另以 `gcd=1`、`solver_unknown`、`timeout`、`tool_error`、显式 hypothesis 和候选完成请求检查了事实种类与状态。

`python -m unittest tests.test_crypto_s06 -v`：8 项通过。S04–S06、原提示词和解析器合计 47 项定向检查通过；原有渗透调度的 `DispatcherFactInsertionTests` 与 `GraphContractTests` 共 37 项通过。Python 编译与 `git diff --check` 通过。未重跑旧测试全集，未使用现有任务数据库。

## 2026-10-22：跨 intent 继承约定

按用户确认的设计，下一步继承解题方法和脚本，新连接重新读取题目数据；不要求每个密码 intent 共用同一 TCP 连接。

- `prompts.py` 的 `_CRYPTO_REPLAY_RULES` 由 first_run、explore、reason 三个密码提示词共用。区分方法/已核实前提、待验证假设与会话专属观察；只对任务明确给出的服务交互，不给离线任务增加网络操作。
- first_run 只低成本识别协议、参数和适用条件；explore 检查脚本是否硬编码旧值，并以当前参数运行；reason 将依赖同一连接的采样、计算和获取结果规划成一个有界 intent，在 description 中说明请求上限与断线处理。不预填尚未读取的数据，不用换会话标签绕过去重。
- fact_description 说明观察适用范围与重放步骤，脚本/交互记录按已有 artifact_refs 保存。离线或已核实稳定的数据可复用；旧会话样本、配额和检查点不能未经核实用于新连接。候选仍需独立验证，现有验证器不支持时保持待验证。
- `container/AGENTS.md` 增加仅适用于 crypto 的规则，明确通用 tmux 建议不代表密码 intent 之间能保留连接。未改变 general 提示词、数据结构、容器清理、验证器和去重实现。

离线检查使用既有 Conda Python，仅构造内存项目并生成提示词、解析 Python 语法；三个密码模式各含全部 8 条共享规则，固定 general 项目三个提示词的 SHA-256 与修改前逐项相同。未新增测试文件、未重跑测试套件，未访问实际数据库、题目服务或模型，也未操作 Docker。

生效边界：宿主机运行中的服务需加载更新的 Python 代码；`container/AGENTS.md` 是镜像构建输入，当前容器内的副本不会因仓库文件修改自动更新。此次未重建镜像或容器。提示词本身不能实现运行时会话校验或恢复，R05 的实际重放行为仍待受控验收，R01–R04 等其他问题也未因此修复。

同日后续部署：按用户授权已经构建含新指引的镜像并启动加载当前代码的服务，见 [环境更新记录](crypto-runtime-update-20261022.md)。新镜像内指引与本地哈希一致；旧项目容器仍未重建，不将新镜像状态等同于所有已有容器已更新。

同日 BUG-020～022 修复：已补精确 bytes/产物引用契约及规划错误反馈；reason 改用独立的规划版继承规则，并仅在密码模式下配置只读沙箱、禁用 shell。此前“全部模式共用 8 条规则”的描述属于上一次提示词调整记录；当前实现与 138 项定向核实结果见 [本地修复记录](crypto-bug020-022-fix.md)。真实模型解题仍未重跑，原有 S09 待整改项不因此关闭。

## 后续边界（原阶段记录）

这些检查证明固定输出能够正确进入图和项目记录，不证明真实模型在未知题目上总能正确解析或规划。S07 仍需按输入哈希、方法、参数和范围实现严格去重/续算；S08 仍需实际资源限额与子进程清理；S09 仍需独立验证记录和完成门槛。当前阶段的 `claim_status`、`confirmed` 等声明仍由 worker 提供，不能单凭模型声明视为数学证明。
