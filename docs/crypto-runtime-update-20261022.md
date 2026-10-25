# 密码适配环境更新记录

时间：2026-10-22 08:22–08:24（Asia/Shanghai）。用户授权构建新镜像并重新加载宿主机服务；本次未修改业务代码、配置或测试，未执行解题、重跑测试套件或同步远端 Git。

## 镜像更新

- 当前配置使用 `echo-agent-worker:crypto-s03`；原镜像 ID 为 `sha256:61d42f9b9745edfcd2dd4b72e5d88bc9f328fc477b0c0e8de29e82f1d8c62674`。
- 先将原镜像另标记为 `echo-agent-worker:pre-replay-20261022-61d42f9b9745`，保留本地回退版本。
- 在仓库根目录执行 `docker build --pull=false --progress=plain -t echo-agent-worker:crypto-s03 -f .\container\Dockerfile .\container`，退出码 0。已有依赖层全部复用缓存，本次重新复制最新 `container/AGENTS.md`。
- 新镜像 ID：`sha256:fbc9745fcadc6aa410f0ca7c3389a6fc0a4e4a9a30e5a7b3e7864a9b241394ac`。
- 使用新镜像启动一个临时、禁网、只读容器核对文件，结束后自动移除。镜像内 `/home/kali/workspace/AGENTS.md` 的 SHA-256 为 `2b4629ae692d773c4cb66b8ba0d799d1e96eb9d01ad84e639d55da21a6912ee4`，与本地文件一致，并包含密码任务跨步骤继承约定。
- 构建有一个既有 Dockerfile 风格提示（固定 `FROM --platform=amd64`），未影响构建；此次未调整 Dockerfile。

## 宿主机服务

- 更新前 `/health` 不可访问，所有项目容器均已停止，没有发现正在执行的 EchoAgent 调度服务，无需中断正在解题的进程。
- 使用既有 Conda Python：`C:\code\miniconda\envs\echoagent\python.exe`。
- 使用原数据库：`C:\Users\20527\Desktop\EchoAgent\.echo_agent\echo_agent.db`。
- 于 08:24:06 在后台启动服务，初始 PID `38196`，监听 `127.0.0.1:8000`；新进程直接加载当前工作区 Python 代码，包括三个密码模式的共享继承规则。
- 本机 Docker 位于 `C:\Users\20527\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`。仅给新服务进程的 PATH 补入该目录和既有 Conda 环境目录，未修改系统/用户级 PATH，也未打印或更改 API 密钥。
- `GET /health` 返回 `{"ok":true}`，携带既有服务认证配置读取 `/projects` 成功，项目数为 41。服务地址：[本地 EchoAgent](http://127.0.0.1:8000)。

后台日志与 PID 文件（均在 `.echo_agent/runtime/`）：

- `server-replay-20261022.stdout.log`
- `server-replay-20261022.stderr.log`
- `server-replay-20261022.pid`
- `replay-deploy-before.json`、`replay-deploy-after.json`：更新前后核对摘要，不含 API 密钥。

## 数据保留与未执行项

服务启动前以 SQLite backup API 创建一致性备份：`.echo_agent/backups/echo_agent-pre-replay-deploy-20261022-082320.db`，备份 `quick_check` 返回 `ok`。

| 对象 | 启动前 | 启动后 |
| --- | --- | --- |
| projects | 41 | 41 |
| facts | 515 | 515 |
| intents | 374 | 374 |
| project_logs | 30645 | 30645 |

库中项目 11、41 原先状态为 `running`，但容器均已停止；服务启动后其后台调度状态均为 `running=false`。本次保留原状态，没有自动续跑题目或修改项目图。

**已有项目容器没有重建。** 它们仍绑定原先的镜像 ID，更新同名镜像和重新启动服务不会替换它们。新建项目容器会使用新镜像；已有项目若需升级，应另行核对容器可写层与挂载、保留必要文件后重建。本次没有删除项目、容器或其数据。

本次证明服务已加载新代码、新镜像包含新 worker 指引、原数据数量保持一致；不代表 S09 复审问题已修复，也不是 S10 端到端验收通过。未调用模型、连接题目服务或验证实际解题效果。
