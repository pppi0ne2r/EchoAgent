# S04：证据元数据与持久化文件底座

状态：2026-10-21 已在本地实现并完成定向核实。S04 是存储底座；离线文件上传、密码提示词、计算预算和答案放行分别留在 S05、S06、S08、S09。

## 实际数据布局

- 旧项目缺少 `task_type` 时读作 `general`，其 `Project/Fact/Intent.to_dict()` 仍输出原有字段。`crypto` 项目额外输出 `task_type="crypto"` 和项目级 `crypto` 对象；fact/intent 仅在有密码元数据时输出 `crypto` 对象。
- 项目级 `crypto` 使用 `schema_version=1`，可保存 `inputs[]`、`default_budget`、`executions[]`、`verifications[]`；fact 的来源、依赖、类型值与产物引用，以及 intent 的输入、方法、参数、范围、预算、续算引用均保存在各自的 `crypto` 对象中。S04 负责保存和校验结构，不解释求解结果或据此完成项目。
- SQLite 给 `projects` 增加 `task_type/crypto`，给 `facts`、`intents` 增加 `crypto` JSON 列。迁移只增加有默认值的列；旧数据不补造来源、输入哈希或验证记录。项目仍按原流程先删除 intents、再删除 facts，随后先插入 facts、再插入 intents，保留原有外键顺序。密码项目的全量 PUT 必须显式携带 `task_type/crypto`；若已有 fact/intent 元数据而请求遗漏，API 拒绝保存，避免旧客户端静默清空证据。

## 文件位置与引用

项目文件根目录固定为数据库所在目录下的 `projects/<project_id>/`，无需新增用户配置。目录按数字项目 ID 分隔：

```text
projects/<project_id>/
  inputs/                    # worker 中 /home/kali/workspace/inputs，只读
  artifacts/intents/<id>/    # worker 中 /home/kali/workspace/intents/<id>，可写
```

只有 `crypto` 项目增加上述挂载；普通渗透项目仍只使用原有 `[resources]` 挂载。现有 intent 工作目录恰好对应 `artifacts/intents/<id>`，因此同名文件按 intent 分开并随容器删除而留在宿主机。完成项目不会清理这些目录。输入引用形如 `inputs/rsa.txt`，产物引用形如 `artifacts/intents/0/result.txt`；数据库不保存 Windows 绝对路径或容器绝对路径。

`CryptoProjectFiles` 提供目录创建、文件描述与按哈希复核。引用须带大小和 SHA-256；输入清单在创建 worker 前重新核对文件内容。路径必须是项目内相对 POSIX 路径，拒绝绝对路径、反斜杠、盘符、`.`/`..` 和越过项目根目录的符号链接。整数与字节类型值按 S02 的基数、编码、长度检查，不用 `eval`，也不接受浮点数作为密码参数。

## 已核实与边界

- 新增的 `tests.test_crypto_s04` 六项均通过：旧数据库迁移、通用 URL 项目原输出格式、HTTP/后台 Store 往返、多事实来源、项目和 intent 文件分离、越界引用及哈希变化拒绝。真实 `tests.crypto_s04_docker_smoke` 一项通过：DockerProjectRuntime 创建 worker、写入产物、完成项目删除容器、重新打开 SQLite 后输入与产物仍可按哈希读取。另用两个全新容器验证了只读输入和两个 intent 使用同名文件互不覆盖。原有整套测试未按用户要求重跑。
- 实际用户数据库只读核对为 39 个项目、500 条 fact、363 条 intent；先用 SQLite backup API 保存一致性备份到 `.echo_agent/backups/echo_agent-pre-s04-20261021-174431.db`，再在临时副本上运行新存储代码的迁移。迁移后数量不变，39 个项目均可读取，`PRAGMA foreign_key_check` 返回 0 个问题。原数据库本轮没有被迁移。
- 文件目录实现的是按项目及 intent 命名的隔离，同一项目容器中的 worker 用户仍可访问同项目的其他 intent 目录；这里尚未提供恶意代码级别的强隔离。S08 再处理计算资源与子进程清理。
- S04 不提供网页/API 上传或自动导入文件。S05 需把原始文件可靠放进 `inputs/` 并生成清单；S06 才提示 agent 使用这些输入。工作目录内的文件虽已持久化，只有显式生成并保存产物引用后，后续步骤才能按哈希追溯。`executions[]` 和 `verifications[]` 在 S04 可往返保存，S08/S09 才会让其影响退出原因和完成门槛。
- 本地已存在的项目容器不会因代码更新自动改变挂载；新建容器才采用 S04 目录布局。本步骤没有同步远端 Git，也没有修改实际用户项目的数据库记录。
