# EchoAgent

把一次 CTF 或渗透任务，从一段滚动输出变成一张看得见的图。

EchoAgent 在本地拉起控制台，给每个题目一个独立的 Docker 容器，并在容器里运行 Codex。外层调度器不替模型做题。它只负责三件事：记下已经证实的事实，派出下一步意图，以及在图上标出当前走到哪里。

它是实验性的编排器，适合想看清智能体为什么继续、为什么重复、证据又是怎么留下来的人。它不保证题目一定被解开。

## 一次任务怎么走

创建项目时写下目的，例如「获取 flag，并说明利用路径」。图上先有两个点：起点，和尚未被证明的目标。

随后按三拍推进：

1. **先试一次。** Codex 直接碰目标。打通了就收束；没打通，就把侦察结果落成事实。
2. **再分头验证。** 每条未完成的意图交给容器里的 Codex。多条意图可以同时跑，每个项目能同时启动的 Codex 数量有上限。
3. **然后决定下一步。** 没有待执行的意图时，推理只依据已有事实规划，避免在同一个想法里空转。

新事实是节点，意图是边。控制台同时显示项目状态、事实图、调度日志和 Token 用量。完成后可以导出解题报告。

## 外层留下什么

单次 Codex 会话里，侦察结果容易停在终端滚动区。超时、非零退出或上下文被截断时，过程就没了。EchoAgent 把过程拆开保存。

- **事实**是已经记下的证据，不依赖模型下一次还记得。
- **意图**是待验证的下一步，做完就有状态，不会被当成没做过。
- **一个项目一个容器。** 附件、工具和失败输出留在这道题自己的环境里。
- **多个项目可以排队或同时运行**，容器互不混用。

## 能直接用到的能力

- 本地 Web 控制台：建题、看图、看日志、停止，以及调整调度和 Codex。
- 事实 / 意图图：起点、证据、意图和目标的关系一次展开。
- 三种调度：首次执行、探索、推理。
- 项目隔离：每个项目一个 Docker 容器，镜像中提供 Codex CLI。
- 扩展：在扩展页管理工具、知识库和技能，项目启动时挂进容器。
- 题目入口：URL、IP、附件和 Hint 可以组合使用。
- 密码题：使用单独的 crypto 项目，可上传离线输入。
- 报告：项目完成后导出包含解题步骤和可复现 payload 的 WP。
- 模型可替换：命令、模型、供应商、重试和超时都在配置里。

## 快速开始

环境：

- Python 3.11+
- Docker
- worker 镜像中可运行的 Codex CLI

安装依赖：

```bash
python -m pip install -r requirements.txt
```

或以可编辑模式安装：

```bash
python -m pip install -e .
```

复制配置并启动控制台：

```bash
cp echo_agent.toml.example echo_agent.toml
python -m echo_agent server --host 127.0.0.1 --port 8000 --db .echo_agent/echo_agent.db
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) ，新建项目并填写目的，然后启动。

也可以直接运行 FastAPI：

```bash
uvicorn echo_agent.server.main:app --host 127.0.0.1 --port 8000
```

## 命令行

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

## 配置

EchoAgent 从当前工作目录读取 `echo_agent.toml`。也可以指定其他文件：

```bash
export ECHO_AGENT_CONFIG=/path/to/echo_agent.toml
```

优先级：

```text
环境变量 > .echo_agent/console_settings.toml > echo_agent.toml > echo_agent.toml.example
```

| 配置段 | 作用 |
| --- | --- |
| `[server]` | 控制台地址、端口和数据库路径 |
| `[dispatcher]` | 循环间隔、每个项目的 Codex 槽位、可同时运行的项目数 |
| `[codex]` | 容器内命令、模型、供应商、重试和超时 |
| `[docker]` | worker 镜像的构建和运行 |
| `[resources]` | 扩展文件在宿主机上的根目录 |

把本机配置写进 `echo_agent.toml`。该文件已被 Git 忽略，不要提交真实密钥。需要分享的模板是 `echo_agent.toml.example`。

## 扩展

工具、知识库和技能在控制台的扩展页配置。清单保存在 `extensions.json`，文件下载到 `[resources].root`。

首次启动会准备这些内置位置：

- 加入 `PATH` 的工具：katana、nuclei、dalfox、cloudfox，挂载到 `/home/kali/.local/bin`。
- `ysoserial` 位于 `/home/kali/tools/ysoserial.jar`。
- `nuclei-templates` 位于 `/home/kali/.local/nuclei-templates`。
- 共享目录：`/home/kali/tools`、`/home/kali/knowledges`、`/home/kali/pocs`；技能位于 `/home/kali/.agents/skills`。

服务启动时下载缺失文件，项目运行前再检查一次；下载失败则停止。扩展页的「更新」会按当前 URL 重新下载并替换已有文件。增删条目只在扩展页操作，不要在 `echo_agent.toml` 里再维护一份清单。

`[resources].root` 只是这些文件在宿主机上的目录，`items` 保持为空。

## 容器

每个项目使用一个容器，名称通常是 `echo-agent-task-<project_id>`。

| 项目状态 | 容器行为 |
| --- | --- |
| `active` | 创建或启动容器，并准备挂载 |
| `running` | 调用 Codex 前确保容器正在运行 |
| `stoped` | 停止容器 |
| `completed` | 删除容器 |

在 Web 界面点击停止时，会强制停止项目容器：

```bash
docker stop -t 0 <container>
```

## 测试

```bash
python -m unittest
node --check echo_agent/server/static/app.js
```

## 致谢

本项目受到 [oritera/Cairn](https://github.com/oritera/Cairn) 的启发，并借鉴了它用图表示任务推进过程的思路。EchoAgent 是独立实现，重点是 Docker 隔离的 Codex worker、本地 Web 界面，以及可配置的共享安全资源。
