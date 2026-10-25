# BUG-031 修复记录：rsa-aes 复合验证（2026-10-24）

对应台账：`EchoAgent-Bug记录.md` → BUG-031
实测题目：check-little（项目 #48），用户已确认候选 `flag{m_m4y_6e_divIS1b1e_by_p?!}` 正确。

## 问题

题目输入是「RSA 加密的 key + AES-CBC 加密的 flag」：

- `output.txt` 给出 `N`、`c`、`iv`、`ciphertext`；
- `task.py` 给出 `e = 3` 与加密流程（程序文件）。

模型在 first_run 与 intent 2/4/5 中完成了 RSA 数学检查、AES 解密与重加密重放，但两次 `verify.candidate` 都 `inconclusive`：

1. RSA 分支：`task.py` 是动态程序，验证器只接受字面量参数 → `RSA parameter files may only assign literal integers…`（已知 BUG-026 边界）。
2. AES 分支：模型构建的 `artifacts/intents/2/aes_manifest.json` 不被验证器读取；验证器只用原始 `output.txt` → `AES input must be a JSON manifest`。

失败点在验证输入契约，不在求解；该缺口已证实阻断正确候选。

## 修复

### 1. 原始输入解析（`crypto_inputs.py`）

新增 `extract_rsa_aes()` / `looks_like_rsa_aes()`：

- 只做**单行白名单键值匹配**，不执行/导入输入文件：`N|c|e|iv|ciphertext = <literal>`；
- 允许参数分散在多个原始输入中：`e = 3` 可从程序文件提取，N/c/iv/ciphertext 从输出文件提取；
- `iv` 支持 Python bytes 字面量或 hex；`ciphertext` 支持 hex 或 bytes 字面量；
- 校验：N ≤ 4096 bit、e ≤ 256 bit、iv 16 字节、ciphertext 非空且 16 字节对齐。

### 2. 新 scheme `rsa-aes`（`crypto_verify.py`，`VERSION` → `crypto-verify-v7`）

验证步骤：

1. 候选 `crypto.candidate.m`（十进制 RSA 明文）必须满足 `pow(m, e, N) == c`；
2. `key = long_to_bytes(m)[:16]`（最小大端表示），必须为 16/24/32 字节；若候选给了 `key_hex`，必须一致；
3. 用原始输入中的 `iv`/`ciphertext` 做 AES-CBC 解密并校验 PKCS#7 去填充；
4. 同 key/iv 重加密，必须与原始 `ciphertext` 逐字节相等；
5. 解密明文与 `candidate.answer` 比较（`literal` 或 `answer_form=hash`）。

安全边界：

- **所有约束来自任务创建者上传的原始输入**；worker 生成的 manifest artifact 不参与验证（否则其 `known_plaintext_sha256` 属于自证）。
- 不执行源码；只读取白名单字面量。
- 兼容旧候选：显式 `scheme=rsa` 但原始输入明显含 `iv`+`ciphertext` 时，自动升级为 `rsa-aes`（该检查严格强于参数型 RSA）。
- 当候选的 `source_input_ids` 只列了输出文件时，可从项目其余**原始输入**补读缺失常量（例如 `e`），仍受 1 MiB 总预算与验证期限/取消约束，并写入 `record.input_refs`。

### 3. Goal 语义

- `rsa-aes` 要求 `answer_kind=plaintext`；
- 不要求 `unique_root`（模数因子不可得时不证明唯一性），由 RSA 关系 + AES 重加密 + 答案匹配共同约束；
- 支持 `answer_encoding=utf8`（默认）、`byte_length` 校验与 `answer_form=hash`。

### 4. 执行环境

- 宿主机有 `pycryptodome`/`cryptography` 时直接计算；
- 否则返回 `inconclusive` 并由 `_verify_rsa_aes_in_container()` 在 worker 镜像内执行（只读、禁网、256 MB、30s 本地超时，支持 R06 的取消/期限）。

### 5. 提示词

- `prompts.py` 的「独立验证支持范围」新增 `rsa-aes`；
- 新增「复合题（rsa-aes）」规则：候选只需给 `m` 与明文 `answer`，不要构建 manifest artifact 作为验证输入；缺少 `e` 时如实报告，不要用 worker 自造参数替代。
- `container/AGENTS.md` 同步（镜像内规则，需重建镜像生效；宿主机提示词已生效）。

## 验证

- 新增 `tests/test_bug031_rsa_aes.py` 11 项：跨文件常量解析、检测条件、缺参报错、正确/错误 m、错误 answer、重加密不一致、AES 库缺失、goal 不要求 unique_root、`scheme=rsa` 自动升级与补读、提示词/版本。
- Docker smoke（`tests/bug031_rsa_aes_docker_smoke.py`）：用项目 #48 的真实 `task.py`/`output.txt` 与 fact 4 的 `m`，在 `echo-agent-worker:crypto-20261022` 内执行：
  - `rsa_ciphertext_matches=true`、`aes_roundtrip=true`、`answer_matches=true`，明文为 `flag{m_m4y_6e_divIS1b1e_by_p?!}`。
- 生产 HTTP 入口重验（服务重启加载 v7 后）：
  - `POST /projects/48/candidates/4/verify` → `status=pass`，`backend=container`，`method=crypto-verify-v7`，`elapsed_ms=1125`；
  - checks：`scheme=rsa-aes`、`n_bits=2048`、`ciphertext_bytes=32`、`rsa_ciphertext_matches=true`、`aes_roundtrip=true`、`answer_matches=true`、`goal_satisfied=true`。
- 全量 365 项：仅 5 项既有平台性失败（4 项 PATH `python` 商店桩、1 项 NTFS `chmod` 无效）。

## 兼容性与未覆盖

- `VERSION` 升为 `crypto-verify-v7`，`v6` 的 pass/inconclusive 记录需重新验证；项目 #48 的两条旧记录（v6）保持原样，未写回新的 pass 记录。
- 显式声明 `scheme=rsa` 的旧候选会自动升级；显式声明其他 scheme 不受影响。
- 目前只支持 **AES-CBC + 单段密文**；RSA+AES-GCM、多密文、非 CBC 模式仍需扩展。
- `e` 的提取依赖 `e = <整数>` 单行字面量；其他写法（如从表达式推导）会返回 `missing e`。
- 未重跑真实模型；修复后的模型行为（是否会按提示给 `m`/`answer` 而不是构建 manifest）仍需一次真实运行验证。
