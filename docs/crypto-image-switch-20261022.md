# 新密码镜像切换与旧版本清理

日期：2026-10-22（Asia/Shanghai）。用户授权构建新镜像、接入本机 Docker 并删除旧镜像；确认旧镜像被容器引用后，用户进一步明确授权“备份后清理旧容器和镜像”。未同步远端 Git。

## 当前运行配置

- 新镜像：`echo-agent-worker:crypto-20261022`。
- 镜像 ID：`sha256:4972829517094dbce5fac922a00532c0353cb2c8fb6af65eb5dcd1ae40c129ea`。
- 从当前 `container/Dockerfile` 构建并载入本机 Docker Desktop，复用了未变化的构建层。
- 镜像内 `AGENTS.md` SHA-256 为 `a881449550b789af86e6b41b2ce05dd105468f6595735789721ea8fa7e3a9021`，与本地最新版一致。
- `echo_agent.toml` 仅将 `[docker].image` 从 `echo-agent-worker:crypto-s03` 改为新标签，解析比对确认其余设置未变，未打印或修改密钥。
- 服务已重新加载，初始 PID `17780`，地址 `http://127.0.0.1:8000`，健康检查正常。以后新建项目容器采用新镜像；此次没有创建虚构题目或调用模型解题。

## 已删除的旧版本

| 旧镜像/标签 | ID 前缀 | 处理 |
| --- | --- | --- |
| `echo-agent-worker:crypto-s03` | `688048bc2f64` | 确认无容器引用后删除 |
| `echo-agent-worker:pre-replay-20261022-61d42f9b9745` | `61d42f9b9745` | 相关容器归档并删除后移除 |
| `echo-agent-worker:pre-contract-fix-20261022-fbc9745fcadc` | `fbc9745fcadc` | 相关容器归档并删除后移除 |

已删除的旧容器为 `echo-agent-task-40`、`echo-agent-task-41`、`echo-agent-task-42`。删除前逐个确认与备份的容器 ID 相同且状态为 `exited`；使用普通 `docker container rm`，没有使用强制停止、卷删除或全局 prune。

归档期间创建的 3 个临时快照镜像在归档验证成功后也已移除，未继续占用 Docker 镜像列表。原 Web 镜像 `echo-agent-worker:latest`（`ee91f6a515fa`）、Sage 相关镜像及其他项目容器保持原样。

## 备份及恢复

备份目录：`.echo_agent/backups/docker-cleanup-20261022-125024/`。

- `old-project-containers.tar`：三个容器的完整镜像快照，包括各自可写层；大小 **524040192 字节**（约 500 MiB）。
- SHA-256：`9cfa1664ef6101f0803744d0b1ce5d490726da4ee7739e09b8f4e16e3a7ac940`。
- 校验确认 3 个快照标签、所有配置/层引用存在，21 个内容块的 SHA-256 全部匹配。
- `echo-agent-task-*.inspect.json`：原容器配置、挂载、镜像等恢复信息。
- `echo-agent-task-*.diff.txt`：原可写层变更清单。
- `echo_agent-before-cleanup.db`：SQLite 一致性备份，检查结果 `ok`。
- `backup-verification.json`、`cleanup-result.json`、`final-verification.json`：归档、清理及最终核对记录。

若以后需要恢复快照，可在项目根目录执行：

```powershell
docker load --input .\.echo_agent\backups\docker-cleanup-20261022-125024\old-project-containers.tar
```

归档内的标签是 `echo-agent-backup:task-40-20261022-125024`、`task-41-20261022-125024`、`task-42-20261022-125024`（同一仓库前缀）。加载镜像不会自动创建或运行容器，恢复时按对应 inspect 文件恢复挂载和配置。绑定到宿主机的输入、产物和共享资源不在容器快照层中，它们的原目录已保留。

## 最终核对

| 数据 | 清理前 | 清理后 |
| --- | --- | --- |
| projects | 42 | 42 |
| facts | 523 | 523 |
| intents | 375 | 375 |
| project_logs | 30723 | 30723 |

项目 #42 的输入文件已按原清单重新核对哈希，产物目录仍存在；没有删除项目记录、改写旧事实/日志或自动续跑题目。新服务健康正常，新镜像可用，旧容器已从 Docker 移除。

本次只调整运行配置和镜像/容器状态，未修改业务代码或测试脚本。部分历史 Docker 冒烟脚本仍写死旧 `crypto-s03` 标签，后续运行这些历史脚本时应先适配新镜像名称；日常服务已采用新配置。
