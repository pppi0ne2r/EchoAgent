# BUG-034 修复记录：MT19937 独立验证（2026-10-24）

对应台账：`EchoAgent-Bug记录.md` → BUG-034
实测：ezran（项目 #50）。用户确认完整解密明文正确，正确 flag 是其中的 `flag{...}` 部分。

## 问题

MT19937 随机数预测题没有独立验证 scheme：

- 候选 fact 7 已由状态恢复 + 逆 shuffle 得到完整明文，fact 8 正向重放确认；
- `verify.candidate` 自动回退 RSA，返回 `RSA independent verification accepts parameter-only files; dynamic Python programs are unsupported`；
- 没有 `pass` 记录，strict 完成门槛拒绝放行，`reason` 连续 no progress 后 `skipped`。

## 实现

### 新增 `echo_agent/crypto_mt19937.py`

- 纯 Python `MT19937`（624 字状态、twist/temper、`getrandbits<=32`）；
- CPython `random.shuffle` 的 `_randbelow`/交换序列；
- `extract_mt19937()`：从**原始输入**解析 `gift`、`c` 与生成参数：
  - 支持 `task.py` 中的 `range(N)`、两次 `getrandbits`、`pow(...,2*i,mod)`、`long_to_bytes(...,k)`、`shuffle` 循环次数；
  - 也支持 `{"algorithm":"mt19937", ...}` JSON manifest（含 gift/cipher/参数）；
  - 不执行提交的程序；程序文件中的 `c = "".join(...)` 不会被误当成密文。
- `replay_mt19937()`：按参数重放 gift；记录每次 shuffle 的交换序列，逆序应用到 `c` 得到明文。

### `crypto_verify.py`（`VERSION` → `crypto-verify-v8`）

- 新 scheme `mt19937`（`_SUPPORTED_SCHEMES`）；`auto` 识别 `getrandbits/shuffle` 程序或 `gift/c` 输出。
- 候选状态来源：
  1. `crypto.candidate.state`：624 个字（list / `{"words":[...],"index":...}` / hex / 逗号分隔）；
  2. `crypto.candidate.state_ref`：指向候选自己的 state JSON artifact（必须在 `artifact_refs` 中，哈希校验）；
  3. 兼容兜底：`artifact_refs` 中名为 `state*.json` 的 artifact（`state_source` 记录实际来源）。
- 候选 `source_input_ids` 只列输出文件时，从项目其余**原始输入**补读 `task.py` 参数（1 MiB/期限/取消约束）。
- answer 匹配：
  - 完整明文 / hex / 明文子串（`flag{...}`）/ 包裹形式；
  - `answer_form=hash` 时比较摘要；
  - 若明文含题面附加噪声（例如 `flag{...}(some_noise...)`），`answer` 可以是 `flag{...}` 子串，记录 `answer_relation=substring`。
- `_check_goal`：mt19937 要求 `answer_kind=plaintext`，不要求 `unique_root`；支持 `byte_length` 与 hash 形态。

### 提示词 / 镜像指引

- `prompts.py` 的「独立验证支持范围」加入 `mt19937`；新增规则：候选给 `state` 或 `state_ref`，`answer` 只写 `flag{...}`（完整明文放 `typed_value`/描述）。
- `container/AGENTS.md` 同步（需重建镜像生效）。

## 安全边界

- 不执行提交的源码；只做单行字面量/正则提取或读取 JSON manifest。
- 状态必须通过 gift 重放（3108 轮）校验；shuffle 次数/参数来自原始输入，候选不能自选。
- 逆 shuffle 的结果由 `c` 与 MT 流唯一确定；answer 子串匹配只接受**明文中的字符串**，不接受任意改写。

## 验证

- 新增 `tests/test_bug034_mt19937.py` 8 项：
  - 参数提取、auto 识别；
  - 小样例重放/逆 shuffle；
  - 完整明文 answer pass、`flag{...}` 子串 pass；
  - 错误 state / 错误 answer fail；
  - mt19937 不要求 unique_root；
  - `VERSION` v8 与提示词包含 mt19937。
- 真实项目 #50 fact 7：
  - `status=pass`、`method=crypto-verify-v8`、`state_source=artifact:artifacts/intents/1/state2.json`、`gift_matches=true`；
  - 明文 96 字符：`flag{7hE_numbEr_0f_biT5_i5_Enou9h_@L5o_ThE_r4nk_must_3n0ugh}(some_noise_to_make_sure_you_get_it)`；
  - 完整明文 answer：`answer_relation=plaintext`、`answer_matches=true`；
  - 截断成 60 字符 flag：`answer_relation=substring`、`answer_matches=true`。
- 全量 **379 项**：仅 5 项既有平台性失败（PATH `python` 商店桩、NTFS `chmod`）。

## 关于提交格式

- 完整明文 96 字符 = 正确 flag 60 字符 + 题面噪声 36 字符 `(some_noise_to_make_sure_you_get_it)`。
- 题目的 `flag.txt` 原样参与 shuffle，因此解密明文包含噪声是正常的。
- 平台最终接受哪种形态（完整明文 / 60 字符 flag / 其他外层包装）由平台规则决定；框架验证器两者都接受，但**提交格式仍需按平台要求**。本记录不替代平台判定。

## 未覆盖

- 只支持 MT19937 + CPython `shuffle`；LCG/xorshift、NumPy `Generator`、不同随机源未支持。
- 只支持候选提供恢复后的 624 字状态；`seed` 反向搜索/部分状态未支持。
- `state*.json` 兜底是兼容路径，正式候选应显式给 `state` 或 `state_ref`。
- 未重跑真实模型；项目 #50 的历史运行结果保持原样。
