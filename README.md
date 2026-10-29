# EchoAgent

EchoAgent 是一个实验性的 CTF / 渗透测试智能体编排器。它把任务保存为事实（fact）和意图（intent）组成的图，在每个项目独立的 Docker 容器里运行 Codex CLI，并通过 FastAPI Web 界面观察推理和探索过程。

## 致谢

本项目受到 [oritera/Cairn](https://github.com/oritera/Cairn) 的启发，并借鉴了它用图表示任务推进过程的思路。EchoAgent 是独立实现，重点是 Docker 隔离的 Codex worker、本地 Web 界面，以及可配置的共享安全资源。

## 功能

- 用 fact / intent 图保存任务状态。
- 包含首次执行、探索和推理三个阶段。
- 多个活动 intent 可以并行探索。
- 每个项目使用独立的 Docker 容器。
- 可以挂载共享的 nuclei 模板、PoC、工具和知识库。
- 可配置 Codex 命令、模型、供应商、重试和超时。
- Web 界面可查看项目状态、事实图和运行日志。

## 环境要求

- Python 3.11+
- Docker
- worker 镜像中可用的 Codex CLI
- `requirements.txt` 中的 Python 依赖

安装依赖：

```bash
python -m pip install -r requirements.txt
```

或以可编辑模式安装：

```bash
python -m pip install -e .
```

## 配置

复制示例配置：

```bash
cp echo_agent.toml.example echo_agent.toml
```

EchoAgent 从当前工作目录读取 `echo_agent.toml`。也可以指定其他文件：

```bash
export ECHO_AGENT_CONFIG=/path/to/echo_agent.toml
```

配置优先级为：

```text
环境变量 > .echo_agent/console_settings.toml > echo_agent.toml > echo_agent.toml.example
```

主要配置段：

- `[server]`：Web 界面的主机、端口和数据库路径。
- `[dispatcher]`：循环间隔、每个项目的 Codex 并发槽，以及可同时运行的项目数。
- `[codex]`：Codex CLI 命令、模型、供应商、重试和超时。
- `[docker]`：worker 镜像的构建和运行设置。
- `[resources]`：扩展目录在宿主机上的根目录。工具清单本身在扩展页面维护。

本地配置写入 `echo_agent.toml`，不要把真实密钥提交到仓库。`echo_agent.toml` 已被 git 忽略；需要共享的模板是 `echo_agent.toml.example`。

## 共享资源

工具、知识库和技能在 Web 界面的扩展页面配置。EchoAgent 把这份清单保存在 `extensions.json`，并把文件下载到 `[resources].root`。

首次启动时会写入内置条目：

- 加入 `PATH` 的工具：katana、nuclei、dalfox、cloudfox，挂载到 `/home/kali/.local/bin`。
- `ysoserial` 位于 `/home/kali/tools/ysoserial.jar`。
- `nuclei-templates` 位于 `/home/kali/.local/nuclei-templates`。
- 共享目录：`/home/kali/tools`、`/home/kali/knowledges`、`/home/kali/pocs`，技能位于 `/home/kali/.agents/skills`。

服务启动时会下载缺失文件。项目运行前会再检查一次，下载失败则停止。扩展页面的“更新”会按当前 URL 重新下载并替换已有文件。增删条目只在扩展页面操作，不要在 `echo_agent.toml` 里再维护一份清单。

`[resources].root` 只是这些文件在宿主机上的目录。`items` 保持为空。

## 启动 Web 服务

```bash
python -m echo_agent server --host 127.0.0.1 --port 8000 --db .echo_agent/echo_agent.db
```

打开：

```text
http://127.0.0.1:8000/
```

也可以直接运行 FastAPI：

```bash
uvicorn echo_agent.server.main:app --host 127.0.0.1 --port 8000
```

## 调度命令

创建并激活项目：

```bash
python -m echo_agent dispatcher create-project \
  --target-url https://target.example \
  --hint "可选提示" \
  --objective "获取 flag" \
  --activate
```

执行一步：

```bash
python -m echo_agent dispatcher run-once 1 --max-workers 4
```

循环执行：

```bash
python -m echo_agent dispatcher run-loop 1 --interval 3 --max-steps 10 --max-workers 4
```

## Docker 生命周期

每个项目使用一个 Docker 容器，名称通常是：

```text
echo-agent-task-<project_id>
```

状态行为：

- `active`：创建或启动项目容器，并准备挂载。
- `running`：Codex 执行前确保项目容器正在运行。
- `stoped`：停止项目容器。
- `completed`：删除项目容器。

在 Web 界面点击停止时，会强制停止项目容器：

```bash
docker stop -t 0 <container>
```

## 测试

```bash
python -m unittest
node --check echo_agent/server/static/app.js
```
