# 密码候选验证与完成闭环修复

后续已完成六项审查修复并加载，验证版本升至 v3；当前服务与镜像信息、收紧后的源码范围及版本保护见 [最新修复记录](review-six-fixes-20261022.md)。本文保留前次 v2 修复及部署过程。

日期：2026-10-22（Asia/Shanghai）。本轮根据两份桌面运行/故障文档及静态复审进行本地修复；不重跑原题、不调用真实模型、不连接题目地址、不提交或推送 Git。

**交付状态：代码、新镜像及宿主机服务均已加载。** 部署前查到项目 #43 正在运行；用户明确选择“停止 #43，立即重启服务加载修复”后，先停止并备份，再重启服务。#43 保持 stoped、dispatcher 未运行，未自动续跑；它原来的运行不算新版本真实模型验收。新镜像构建不会替换已创建的项目容器。

## 1. 修复范围

| 对应发现 | 本轮处理 | 尚存边界 |
| --- | --- | --- |
| R01：网页后台缺验证方法 | `StoreDispatcherClient` 补齐 `verify_crypto_candidate`，与 HTTP 共用 `CryptoProjectService`；两入口均检查输入清单、输入文件和既有元数据保留 | 未将低层 `SQLiteProjectStore.save_project` 改造成所有脚本必须经过的权限边界 |
| R02：数学成立与目标脱节 | 项目增加可选 `crypto.goal`；区分明文、因子、私钥指数与任一解/唯一解；绑定目标摘要 | 完整解集暂不支持；大整数素性仍为 probable-prime 检查，不宣称通用严格素性证明 |
| R03：输入/表示范围过窄 | RSA 支持原始文本、Python 顶层字面量赋值与 RSA JSON；多个输入合并并记录来源；支持受目标约束的整数到字节转换；GCM 可独立验原始认证标签 | 不执行上传源码，不解析任意表达式或验证任意求解脚本；NTRU、XOR、Z3、任意二进制与交互题的最终验证仍无通用分支 |
| R04：省略字段提升信任、候选保存静默失败 | 所有密码输出走专用入口；任何 answer/candidate 都进入候选；明确 observed 才能进入 evidence；保存失败有诊断 | observed 仍是执行者对实际观察的声明，不是自动获得数学证明；最终答案仍须独立校验 |
| R09：状态 API 绕过完成门槛 | HTTP PUT、状态 PATCH、网页后台保存采用同一完成门槛，重新核验候选和目标 | 平台回执仍未实现，`require_platform_acceptance=true` 继续阻止仅凭本地校验完成 |

## 2. 目标约定与兼容

不用迁移数据库列，继续使用现有 JSON 元数据。旧密码项目缺少 goal 时，默认明文目标及 unique 策略；默认 RSA 答案为十进制、AES 为 UTF-8。不会根据自然语言 objective 猜测更宽松的目标。

示例：

```json
{
  "schema_version": 1,
  "inputs": [],
  "goal": {
    "answer_kind": "plaintext",
    "solution_policy": "unique",
    "answer_encoding": "hex",
    "byte_length": 32,
    "byte_order": "big"
  }
}
```

- `answer_kind`：`plaintext`、`factors`、`private_exponent`。RSA 因子按十进制 `p,q` 表示，私钥指数按十进制字符串表示。
- 非明文候选须在 `crypto.candidate.answer_kind` 明确填写对应类型，并给出 p/q 或 p/q/d；省略时仍按 plaintext 处理，不从候选答案文字猜测类型。三个密码提示词已给出该精确约定。
- `solution_policy`：`unique` 或 `any`。RSA 明文目标只有在要求 any 时，才允许一个有效根在未证明唯一性的情况下通过；unique 下多根或唯一性不明返回 inconclusive。因子/私钥目标按对应算术关系验收，不要求唯一的文本排序或唯一私钥代表元。
- `answer_encoding`：可省略或指定 `decimal`、`hex`、`utf8`；字节长度/顺序仅用于明确的字节明文目标，不能由候选自行改变。
- AES 按原始认证/明文约束验收，并不穷举全部可能密钥来证明唯一性。GCM 必须通过原始 tag，若还提供明文哈希则两者均须通过；CBC 必须有原始输入中的已知明文哈希。
- 网页创建密码项目时可选择上述目标；CLI 增加 `--answer-kind`、`--solution-policy`、`--answer-encoding`、`--answer-byte-length`、`--answer-byte-order`。输入文件的 `--byte-length/--byte-order` 仍是输入声明，与最终答案表示分开。
- 目标由任务创建者设定，worker 只读取 completion_goal。现有项目可以停止后通过 API 更新 `crypto.goal`；更新后必须重新核验。

验证版本升级到 `crypto-verify-v2`。记录包含 `math_status` 和最终 `status`，以及 goal_sha256、候选摘要、原始输入/产物引用和检查项。目标摘要同时绑定 objective、hint、target_url。旧 v1 记录、目标变化或候选变化不能直接放行；验证 intent 的身份包含版本、目标及候选摘要，允许必要的重新验证。

## 3. 输入与信任处理

- RSA 明文需要原始 n/e/c，因子需要 n，私钥指数需要 n/e；p/q/d 可作为候选的待验算证明材料。
- 最多 16 个原始输入，总计 1 MiB；从同一次有上限的读取中校验长度和 SHA-256，再提取字面量。记录每个参数的 input_id、行号与格式。重复 JSON 键、动态或重复赋值、冲突参数、缺少实例数据明确拒绝为 inconclusive。
- 源码中的字面量仅作为参数来源，提取不是执行或证明整段源码的运行语义。first_run 需核查是否还缺实例密文或明确的服务地址，不将源码阅读冒充恢复 flag。
- RSA hex/UTF-8 输出按项目约定转换，可保留前导零及指定大小端，候选必须提供对应十进制 m。
- 少填 `crypto`、少填 claim_status 的观察以 ERROR fact 加 hypothesis/诊断保存，作为待核实材料，不进入 evidence/allowed_from；原模型输出仍保留在日志中。
- 任何非空 answer、candidate 结构或显式 candidate 标记均先尝试保存为 CANDIDATE。缺答案、两个答案不一致、路径/引用不合法时记录 candidate_error，intent 不再错误声称“候选保存成功”。
- 已核实观察需显式 `crypto.fact.claim_status=observed`；超时末次观察还需 confirmed=true。之前独立输出的合规观察仍保留，不因最后一条失败而全部丢弃。

## 4. 核实证据

- 相关单元/集成检查共 **192 项：191 通过，1 跳过**。跳过的是宿主机可选 AES 库检查；没有为此安装宿主机数学环境。后续收紧验证参数检查后，又对受影响模块的 144 项检查复核通过（其中同一项可选检查跳过），不将两批数量累加。
- 新增 `tests/test_crypto_completion_closure.py` 共 23 项，包括实际 StoreDispatcherClient、HTTP 路由与服务、真实本机 TCP HTTP + ProjectClient 的“候选→验证→完成”；求解输出使用固定样例，没有真实模型。
- 反例包括：明文任务用因子冒充完成、多根与唯一根要求、目标变化、候选变化、伪造 pass、PUT/PATCH/后台绕过、输入篡改与替换、缺字段、候选保存失败、动态/冲突/不完整源码、多输入、前导零与大小端。
- `tests/crypto_completion_docker_smoke.py` 在禁网、只读、256 MiB 临时容器内执行 **9 项 AES 正反例通过**：原始 GCM tag、错误 tag/答案/明文约束、CBC 缺约束及正确/错误约束、错误目标。没有真实 API 调用。
- `tests.crypto_s09_server_docker_smoke` 通过真实临时 Docker 后端验证本地测试 API 正例 pass、错误 tag fail。该脚本改用当前镜像默认标签，可由 `ECHO_AGENT_TEST_IMAGE` 覆盖，并显式声明 hex 目标。
- 原 general 项目的 first_run/explore/reason 提示词 SHA-256 与修复前完全一致；相关 general 调度、客户端和 runner 检查通过；Node 语法检查与 `git diff --check` 通过。没有声称覆盖所有原渗透场景或真实靶机回归。

主要测试命令：

```powershell
python -X utf8 -B -m unittest tests.test_crypto_completion_closure tests.test_crypto_s04 tests.test_crypto_s05 tests.test_crypto_s06 tests.test_crypto_s07 tests.test_crypto_s08 tests.test_crypto_s09 tests.test_crypto_contract_recovery tests.test_dispatcher tests.test_dispatcher_runner tests.test_client tests.test_prompts tests.test_parsing -q
python -X utf8 -B -m tests.crypto_s09_server_docker_smoke
node --check echo_agent/server/static/app.js
```

## 5. 镜像、备份与加载

- 新镜像标签仍为 `echo-agent-worker:crypto-20261022`，构建后的 ID 为 `sha256:1fbdc8b4e6d96c7822af20124f87e3d24b8270c7e162caea73cd445095d5802b`。
- worker 指引 SHA-256：`62a92aba7a394cafe06f79f9e24f9f0759ade63097a71eb17ab99553a5cdfb31`，镜像内外一致。
- 本次构建前的镜像保留为 `echo-agent-worker:pre-closure-20261022-497282951709`，没有删除运行中项目容器或旧层。
- 修复前的主要工作区文件备份：`.echo_agent/backups/crypto-closure-before-20261022/`。工作区原有未提交改动保留，未进行 Git 提交/推送。
- 部署前检查时服务健康，实际共有 43 个项目，#43 dispatcher 正在运行；当时未修改原项目数据库，正常运行仍可能自行更新数据。后续仅按用户授权执行正常停止与加载，未改写历史解题事实。
- 用户随后授权停止 #43 并加载：DELETE run 后确认没有活动 dispatcher；通过 SQLite backup 保存 `.echo_agent/backups/echo_agent-pre-closure-load-20261022.db`，quick_check 为 ok。重启前后均为 43 projects、529 facts、388 intents、32816 project_logs；停止行为按正常运行管理落库，没有改写历史求解事实。
- 旧服务 PID 17780 已核对身份后停止，首次加载 PID 16544；最终补齐非明文候选的精确字段说明后，在无活动任务时再次加载，当前 PID **14732**。继续使用原 Conda 环境、原数据库和本地配置；健康检查 `{"ok":true}`，认证读取正常，重启前后数量仍一致。日志 `.echo_agent/runtime/server-closure-20261022.stdout.log` / `.stderr.log`；核对结果 `.echo_agent/runtime/crypto-closure-deployment.json`。
- 没有重建 #43 容器；其镜像内指引仍属于创建时的版本。新宿主机提示词已加载，后续新建项目容器采用新镜像。未删除 #43 文件或额外清理镜像。
- 宿主机核心模块需要服务重启才能加载，新建/重建项目容器才读取新镜像中的指引。不要用重建镜像代替重启服务，也不要认为重启旧容器会自动替换其镜像。

## 6. 后续边界

S10 真实模型端到端仍未完成，#40/#42 历史未解出结果不改写；#43 的原运行也不算新版本验收。R03 的广泛题型覆盖、R05 的不可重放状态、R06 的统一取消/并发与宿主机计算预算、R07 的通用语义去重、R08 的自动版本识别及平台回执仍有后续工作。

本轮将候选验证 POST 和密码 completed PUT 的客户端等待时间设为 45 秒，且不自动重试，以容纳现有 30 秒 Docker 校验；普通请求维持原行为。此项不能替代服务器端取消和幂等机制；直接状态 PATCH 的通用客户端仍使用原默认超时，验证场景建议走标准调度完成路径。
