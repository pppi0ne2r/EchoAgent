# Worker 镜像补库：crypto-20261024（2026-10-24）

## 背景

用户决定**保留容器的联网能力**（原设计未在运行容器上禁网；离线只是提示词/任务语义约束）。ezran 运行中模型曾用 `pip install numpy`（BUG-032 观察），说明镜像缺少常用计算库。为减少运行期联网安装、提高可复现性，在镜像构建期补充固定版本的常用库。

## 新增库

`container/crypto-requirements.txt` 新增（均为 Python 3.14 wheel）：

| 包 | 版本 |
| --- | --- |
| numpy | 2.5.4 |
| scipy | 1.18.1 |
| ecdsa | 0.19.2 |
| cryptography | 50.0.2 |

原有：pycryptodome 3.24.1、sympy 1.14.0、mpmath 1.3.0、gmpy2 2.3.2、z3-solver 4.15.4.0。

**未加入 pwntools**：其依赖中的 capstone 需要 `6.0.0a11`（alpha）才能匹配当前 Python/wheel 组合；保留联网能力，需要时可在运行期安装。如后续要固定，需同时锁定 capstone/cryptography 等间接依赖。

## 构建方式

- 首选：`container/Dockerfile`（从零构建）。
- 本次 kali apt 源在构建网络下不可达（`Unable to connect to http.kali.org:80`），且此前清理过构建缓存，全量构建在 apt 层失败。
- 因此使用增量 Dockerfile `container/Dockerfile.crypto-libs`：
  - `FROM echo-agent-worker:crypto-20261022`（已验证基础镜像）；
  - 只执行 pip 安装、自检、复制最新 `AGENTS.md` 与 `.agents`；
  - 产物标签 `echo-agent-worker:crypto-20261024`。
- 构建命令：

```bash
docker build --network host -f container/Dockerfile.crypto-libs \
  -t echo-agent-worker:crypto-20261024 container
```

## 自检

`crypto_selfcheck.py` 已加入 numpy/scipy/ecdsa/cryptography 检查（位异或、packbits、行列式、ECDSA 密钥、AES-GCM 加解密），构建时输出：

```json
{"status":"ok","python":"3.14.8","interpreter":"/usr/bin/python3","uid":1000,
 "packages":{"cryptography":"50.0.2","ecdsa":"0.19.2","gmpy2":"2.3.2","mpmath":"1.3.0",
             "numpy":"2.5.4","pycryptodome":"3.24.1","scipy":"1.18.1","sympy":"1.14.0",
             "z3-solver":"4.15.4.0"}}
```

独立验证：

```
numpy 2.5.4 / scipy 1.18.1 / ecdsa 0.19.2 / cryptography 50.0.2
gf2 xor [2, 0, 2]
```

镜像信息：`echo-agent-worker:crypto-20261024`，ID `8e5f870d0008`，2.45 GB（旧镜像 2.14 GB，共享基础层）。旧标签 `crypto-20261022` 保留用于回退。

## 配置与部署

- `echo_agent.toml` 的 `[docker] image` 改为 `echo-agent-worker:crypto-20261024`；
- 宿主机服务已重启并加载新配置（健康检查 `{"ok": true}`）；
- 新建项目使用新镜像；**已有项目容器不会自动重建**，仍绑定各自旧镜像（需要时手工停止/删除容器后按新镜像重建）。

## 回退

把 `echo_agent.toml` 的 `image` 改回 `echo-agent-worker:crypto-20261022` 并重启服务；旧镜像仍在。

## 评测注意

- 容器仍允许联网（用户选择）。运行结果若依赖运行期 `pip install` 或外部下载，应在报告中注明，不能与纯离线运行直接横向比较。
- 缺失库优先加入 `crypto-requirements.txt` 并重建镜像；运行期安装只作为兜底。
