# S05：离线密码任务与输入链路

状态：2026-10-21 已核实通过。仅修改本地 EchoAgent；未同步远端。

## 使用入口

- 网页创建项目时选择“密码学（离线）”，无需目标 URL；可同时选择多个文件。创建后逐个上传，全部成功后再手动激活。创建表单的一批文件共用角色；选中项目后的“补充输入文件”可逐个指定源码、参数或密文角色，也可用于失败后的补传。原“Web / 渗透”选项仍要求 URL，原创建、激活、运行按钮保留。
- API 先 `POST /projects`，传 `{"task_type":"crypto","objective":"..."}`，再对每个文件 `POST /projects/{id}/inputs?filename=...&role=...`。请求体必须是文件原始字节，`Content-Type: application/octet-stream`。响应是带 `id/role/filename/path/sha256/size_bytes` 的输入引用。
- CLI 可用 `create-project NAME --task-type crypto --objective "..." --input parameters=C:\path\rsa.txt --activate`。`--input ROLE=PATH` 可重复；也可用 `upload-input PROJECT_ID PATH --role ciphertext` 向已有密码项目补充文件。CLI 在本机读取文件字节并发送给 API，不把本机路径当作服务端文件引用。上传失败时项目保持未激活，已有成功上传可在项目输入清单查看。

可选的 `number_radix=10|16`、`byte_order=big|little`、`byte_length` 可在 API 查询参数、网页表单或 CLI 参数中登记。它们是用户提供的解释声明，原始文件保持逐字节不变；未填写时不猜测十进制/十六进制、字节顺序或长度。对参数的实际解析、与声明的一致性检查及 typed fact 形成属于 S06。特别是多参数文本文件不能仅靠一个文件级声明替代逐个参数的 typed value。

## 约束与故障行为

- 单文件最多 32 MiB。上传时在未挂载的项目目录中暂存、流式计数和计算 SHA-256，写完后移入 `inputs/`；超限返回 413，临时文件删除。
- 文件名保留原名用于显示，但拒绝路径分隔符、Windows 保留名称、控制字符及不安全尾缀；实际存储名加入随机前缀，限定在该项目 `inputs/`。输入 ID 不可重复。数据库只在完整文件落盘后登记引用，登记失败时删除对应文件。
- API 创建或 JSON 保存项目时不能通过手写 `crypto.inputs` 冒充上传。加载、激活时仍由 S04 校验路径、文件大小和哈希；worker 中的只读路径为 `/home/kali/workspace/inputs/...`。worker 与规划上下文能看到输入 ID、角色、哈希和容器内路径。
- 原有 general 项目的 JSON 输出结构不变，文件上传端点拒绝 general 项目；普通 Web 项目的 URL 和运行入口不变。

## 核实

- `python -m unittest tests.test_crypto_s05 -v`：3 项通过，覆盖无 URL 创建、真实 HTTP/CLI 字节上传、输入清单与哈希、非法文件名/重复 ID/超限、网页同用的 API、原 Web URL 项目创建/激活/运行状态入口。
- `python -m unittest tests.crypto_s05_docker_smoke -v`：1 项通过。用临时数据库经 API 上传含 `\x00`、`\xff` 和 CRLF 的文件，API 激活真实 `echo-agent-worker:crypto-s03` 容器后，容器读出字节与本地输入完全相同；测试后删除容器。
- `node --check echo_agent/server/static/app.js`、Python 编译检查、`git diff --check` 通过。没有重跑旧测试全集，也没有使用现有任务数据库。

S06 仍需完成密码任务专用规划指引和事实语义；目前只是把输入和明确路径交给 worker/规划上下文，不能据此声称模型已会正确分析密码题。
