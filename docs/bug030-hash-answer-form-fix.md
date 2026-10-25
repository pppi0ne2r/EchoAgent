# BUG-030 修复记录：答案形态（answer_form / answer_hash）

日期：2026-10-24
对应台账：`EchoAgent-Bug记录.md` → BUG-030

## 问题

题面规定「flag 是明文的 md5」时，模型输出：

```json
"candidate": {"answer_kind": "plaintext",
              "answer": "wowyoucanreallydance",
              "md5": "8ef6b8c6ba93be7a66aa9846237d864b"}
```

`answer` 是明文、哈希放自定义字段。完成门槛（`completion_source`）、evidence 校验和平台回执都只读 `candidate.answer`，因此按该候选完成会绑定明文；自定义 `md5` 字段没有任何代码读取。框架也无法从题面自动推断答案表示。

## 修复

### 1. 契约：goal 新增显式答案形态

`echo_agent/crypto_contract.py`

- `crypto_goal()` 新增可选字段：
  - `answer_form`：`literal`（默认语义）或 `hash`
  - `answer_hash`：`md5/sha1/sha224/sha256/sha384/sha512/sha3_256/sha3_512/blake2b/blake2s`
- 约束：
  - `answer_form=hash` 必须同时给出 `answer_hash`，且 `answer_kind=plaintext`
  - `answer_form=hash` 时 `answer_encoding` 只能是 `utf8`（答案就是摘要文本）；`byte_length` / `byte_order` 仍然有效，它们描述的是哈希前像（明文）的字节表示
  - 未写 `answer_form` 时不写入默认键——**保证旧项目的 `goal_sha256` 不变**，历史 verification/acceptance 记录不因本次改动失效

### 2. 验证：独立复算明文哈希，拒绝「明文当答案」

`echo_agent/crypto_verify.py`（`VERSION` → `crypto-verify-v6`）

- `_match_plaintext_answer()`（RSA / DSA / ECDSA 共用）：`answer_form=hash` 时不再接受十进制、明文、子串、包裹等关系，只接受
  `hash(恢复出的明文字节) == answer`；`checks` 记录 `answer_hash_algorithm`、`answer_hash_expected`、`answer_hash_matches`、`preimage_hex`。
- `_verify_aes()`：新增 `goal` 参数，同样按 `answer_form=hash` 复算摘要；容器回退路径 `_verify_aes_in_container()` 同步传递 `goal`。
- `_check_goal()`：新增形态一致性校验——`answer_form=hash` 只允许 `answer_kind=plaintext` 且 scheme ∈ {rsa, aes, dsa, ecdsa}；`hash/xor/encoding` 的 answer 本身就是 preimage，声明 hash 形态时在验证入口直接返回 `inconclusive` 并说明原因。
- `_candidate_int()`：`answer_form=hash` 时禁止把 `answer` 当作十进制私钥回退值（摘要可能恰好全是数字）。
- `_verify_rsa()`：`answer_form=hash` 且候选未给 `candidate.m`（十进制明文）时返回 `inconclusive`，明确说明 answer 只能放摘要、明文放 `m`。

关键点：哈希不是模型自报的状态。验证器先从 `n/e/c`（或 DSA/ECDSA 公钥、AES 密文）独立恢复明文字节，再计算摘要与 `candidate.answer` 比较。模型把明文放 `answer`、哈希放 `md5` 字段时，`answer_hash_matches=false`，验证 `fail`。

### 3. 提示词

`echo_agent/dispatcher/prompts.py`（first_run / explore / reason 共用 `_crypto_contract_rules`）

- 新增「答案形态（answer_form）」规则：题面规定提交哈希时，`crypto.candidate.answer` 必须是该哈希的小写十六进制串，明文放 `candidate.m` 或事实描述；不要只放 `md5` 等自定义字段。
- 新增「答案形态与 scheme 的关系」：`answer_form=hash` 仅适用于明文类目标与密钥恢复类题型。
- reason 规则补充：`completed=true` 的 `answer` 必须与验证记录一致；`checks.preimage_hex` 可用于核对明文与哈希。

`container/AGENTS.md` 同步该规则（镜像内 guidance，需重建镜像后生效；宿主机提示词已生效）。

### 4. 配置入口

- CLI：`create-project --answer-form {literal,hash} --answer-hash <algorithm>`
- Web UI：创建项目表单新增「答案形态」「哈希算法」两项；选择哈希时前端强制要求算法并默认 `answer_encoding=utf8`

## 兼容性

- 未声明 `answer_form` 的项目：`crypto_goal()` 输出不变（不新增键），`goal_sha256` 不变，`literal` 路径的 `checks` 字段与判定结果均未改动。
- `VERSION` 升到 `crypto-verify-v6`：strict 模式只接受当前版本的 pass 记录，旧 `crypto-verify-v5` 记录需要重新验证。这是验证语义扩展后的既定做法。
- `hash/xor/encoding` 三类 scheme 上声明 `answer_form=hash` 会被拒绝（`fail` + 原因），因为它们的 answer 本来就是精确 preimage。

## 验证

- 新增 `tests/test_bug030_hash_answer_form.py`（13 项）：契约校验、RSA 正确哈希 pass / 明文答案 fail / 错误算法 fail、DSA 私钥哈希、hash scheme 拒绝、literal 路径不回归、提示词包含规则。
- crypto 相关 136 项检查全部通过（1 项宿主机可选 AES 库检查跳过）。
- 全量 329 项：仅 5 项既有平台性失败（4 项 `tests.test_agents` 因 PATH 上的 `python` 是 Microsoft Store 桩退出 9009；1 项 `test_docker_runtime` 因 NTFS 上 `chmod +x` 无效）。改动前基线同为这 5 项。

## 尚未覆盖

- AES 哈希形态的容器内实跑：本机 conda 环境无 `pycryptodome`/`cryptography`，该路径只有 `skip` 级别的静态覆盖，需在 worker 镜像内验证。
- 真实模型端到端：未用真实题面/真实模型验证「模型能按新规则把哈希写进 answer」；只验证了框架侧拒绝错误形态。
