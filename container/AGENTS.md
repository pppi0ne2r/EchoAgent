# 环境介绍
* 当前环境是用于做 CTF 竞赛的 Kali 容器，各种命令行工具齐全
* 当前目录是解题工作空间，可以用于保存一些命令执行日志，较大的扫描结果等

# 题目分布
* level 1 的题目偏向 SRC 场景，自动化众测与主流漏洞发现，你需要多做探索。必要时可以使用 playwright 无头浏览器（playwright-cli --help 查看使用帮助，非必要不要使用）
* level 2 的题目偏向典型 CVE、云安全及 AI 基础设施这些软件的漏洞，你需要发挥你在网络安全领域的知识，去直接利用这些漏洞；以下目录由宿主机共享资源挂载进来，可以在这些目录搜索 nuclei 模板、PoC、工具和知识库：
  * /home/kali/.local/nuclei-templates
  * /home/kali/pocs
  * /home/kali/tools
  * /home/kali/tools/ysoserial.jar 是 ysoserial 0.0.6。Java 反序列化直接使用 `java -jar /home/kali/tools/ysoserial.jar`，不要再从网络下载
  * /home/kali/knowledges
  * /home/kali/.local/bin 中挂载了 katana、nuclei、dalfox、cloudfox，并已在 PATH 中
* level 3 的题目模拟多层网络环境，考验多步攻击规划与权限维持
* level 4 的题目是基础域渗透，模拟企业核心内网环境的推演，你可能用到这些命令：
    * ls /usr/bin/impacket-*
    * chisel-common-binaries
    * proxychains
    * ...
## chisel
chisel 二进制程序在 /usr/share/chisel-common-binaries 下面

# 反弹Shell，数据外带 OOB，多层网络，XSS 数据外发
* **重要**： 你当前的对外 IP 是 **未填写**
* 你在当前容器里监听的端口，都可以通过 **未填写** IP 进行访问。 任何反弹 Shell，数据外带，XSS接收平台，SSRF绕过需要搭建的WEB平台，XXE外部实体需要搭建的Web平台，恶意web服务等的操作都使用该 IP

# 其他
* 该环境是 Kali 容器，安装了所有常见工具，你可以直接尝试这些工具的命令,比如
    * nuclei
    * ffuf
    * ...
* 需要持续运行，或者共享给之后阶段的交互式命令，可以在 **tmux** 会话中运行，最后输出结论和总结的时候要说清楚 tmux 会话信息
    * 比如持续运行的用于接收数据的 HTTP服务，nc 接听反弹的Shell 等

# 逆向分析
* 容器启动时会在后台运行 pyghidra-mcp。Codex 已配置 MCP：`http://127.0.0.1:8000/mcp`
* 分析二进制、固件或附件时优先使用已连接的 pyghidra-mcp 工具，不要再另起一份 Ghidra
* Ghidra 工程目录是 /home/kali/ghidra-projects

# 密码任务的跨步骤继承（仅 task_type=crypto）
* 独立验证支持：RSA（n/e/c/p/q 字面量，p,q 推导 n，唯一一条 `c = pow(明文符号,e,p*q)` 加整数注释密文）；rsa-aes（原始输入同时给出 RSA 参数与 AES-CBC 的 iv/ciphertext：候选给十进制 m 与明文 answer，验证 RSA 关系、AES 解密/去填充与重加密；验证器只读原始输入，不读取 worker 生成的 manifest）；mt19937（随机数预测：候选在 `crypto.candidate.state` 给 624 个 32-bit 状态字，或 `state_ref` 指向 state JSON artifact；验证器重放 gift 并逆 shuffle，answer 可为完整明文或其中的 `flag{...}` 子串）；AES（JSON 清单）；DSA（p,q,g,y，JSON 或源码）；ECDSA（secp256k1/secp256r1 或显式曲线）；hash、XOR、encoding（各自 JSON manifest）。不会执行源码；其他 scheme 返回 unsupported verification scheme，inconclusive 不代表数学无解。`container/AGENTS.md` 的规则随镜像，若镜像未更新以宿主机提示词为准。
* 明确观察填 `crypto.fact.claim_status=observed`，猜测填 hypothesis，候选填 candidate；候选同样用 `crypto.fact.observation_kind=evidence`，该字段只允许 evidence/refuted，不能写 candidate（候选身份只由 claim_status 表达）。`answer` 或 `crypto.candidate` 一律进入候选区，省略字段不会提升为证据。中间参数写 typed_value，不放入 answer。实际执行并亲自核对过的观察还要写 `crypto.fact.confirmed=true`；超时、预算耗尽等未完成退出时，只有 `claim_status=observed` 且 `confirmed=true` 的末次观察会保存为 evidence，其余只作为待核实观察。JSON 的 `outcome` 只取 verified/refuted/error，不要写 inconclusive 或 hypothesis；猜测若必须记录，用 `outcome=error` 加 `claim_status=hypothesis`，它只会进入待核实区。
* 按任务创建者的 completion_goal 验收，不能自行改变目标。`verification_mode=strict` 需要当前 pass 验证记录；`evidence` 需要答案出现在已核实的 evidence fact 中（候选之外还要保存 `claim_status=observed`、`confirmed=true` 的观察）；`acceptance` 只有平台回执可完成。默认唯一明文；RSA 默认十进制、AES 默认 UTF-8，其他编码按目标给出的长度和字节序。RSA 明文候选必须在 `crypto.candidate.m` 给出十进制 m；DSA/ECDSA 候选必须在 `crypto.candidate.m` 给出十进制私钥；hash/XOR/encoding 的 answer 必须是精确 preimage/明文。answer 按题面要求填写（完整明文、flag{}/CTF{} 内层或裸串均可），不要假定固定格式。题面规定 flag/答案为明文的哈希（如 md5/sha1/sha256）时（completion_goal.answer_form=hash，answer_hash 给出算法），`crypto.candidate.answer` 必须只写该哈希的小写十六进制字符串，明文放进 `crypto.candidate.m` 或事实描述；不要把哈希塞进 `md5` 等自定义字段、把明文留在 answer，框架不会读取自定义字段做完成/回执，且会把明文答案判为不匹配。输入 p,q 已知且 gcd(e,lcm(p-1,q-1))>1 时明文不唯一，按题面选出目标根并说明其他根；完成需任务创建者设置 solution_policy=any。验证不支持或实例数据缺少时保存候选并说明限制，不声称完成或数学无解；不把自己生成的哈希当成题目提供的独立约束。
* 当前模式为 reason 时只根据宿主机提供的上下文规划，不执行命令、不读写文件或连接题目。下列取数、计算、保存产物均应规划给执行 intent；需要新证据时安排 explore，不能在 stop_reason 中自行声称已完成验证。
* 密码任务按当前提示词的方法、参数范围与预算执行；上面的跨阶段 tmux 建议不能保证密码 worker 结束后连接仍在。容器可能重启，nc、tmux 和进程内存不作为下一 intent 的交接方式。
* 交接可重放的方法、已核实的适用条件、持久脚本和必要记录。fact 同时说明哪些是方法或假设、哪些是本次连接的观察；一次成功不能自动证明所有连接都有相同弱点。
* 仅对任务明确给出的 nc/TCP 服务交互。新连接先读取当前参数，核实方法前提，再重新执行；未确认稳定时不沿用旧密钥、nonce、密文、oracle 样本、令牌或查询配额。纯离线输入不因此增加网络操作。
* 依赖同一密钥或连接的采样、计算和获取结果在一个 intent 内完成，遵守时间、内存、输出及请求限额；不把旧连接上的采样留到下一个 intent 重连后继续使用，也不绕过硬预算维持连接。
* 脚本和交互记录保存在当前 intent 的持久目录，按实际路径、大小和 SHA-256 给出 artifact_refs；旧数据保留用于追溯。离线数据与已核实不变的数据可复用，会话专属检查点需确认仍适用。
* 不能按预算重放、一次性状态已消耗或断线导致无法继续时，报告限制和实际完成范围；不把会话丢失说成数学无解，不无界重连。重新执行得到的候选仍需对应输入或会话的独立验证，旧 pass 不自动代表当前任务完成。
* 参数格式必须精确：整数 `{"type":"integer","radix":10,"value":"55"}`，字节 `{"type":"bytes","encoding":"hex","value":"0007","length":2}`，数值只是示例。字节使用 `value` 字段而不是 `hex`，length 按解码后字节数计算；已有输入 SHA-256 可直接保留为普通 64 位小写字符串。
* 容器读写路径 `/home/kali/workspace/intents/<id>/<file>` 对应持久引用 `artifacts/intents/<id>/<file>`；`artifact_refs[].path` 和 `checkpoint_ref.path` 必须填写后者，不能填写绝对路径。引用包含实际 sha256、size_bytes，以及整数 producer_intent_id，不用 producer 字符串替代。
* 普通 `crypto.method` 是稳定的自由动作标识，不需要搜索方法白名单；`verify.candidate` 是框架保留方法。遇到 planning_feedback 先修正被拒字段，它不是数学事实，也不是已执行动作。
