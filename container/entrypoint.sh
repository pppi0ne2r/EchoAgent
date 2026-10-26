#!/bin/sh
set -eu

project_dir="${PYGHIDRA_PROJECT_PATH:-/home/kali/ghidra-projects}"
config_dst="/home/kali/.codex/config.toml"
mkdir -p "$project_dir" /home/kali/.codex
if [ ! -s "$config_dst" ]; then
  cp /opt/echo-agent/codex-config.toml "$config_dst"
fi

if ! python3 -c 'import socket; socket.create_connection(("127.0.0.1", 8000), 0.3).close()' >/dev/null 2>&1; then
  echo "starting pyghidra-mcp on 127.0.0.1:8000" >&2
  # MCP 只监听容器内回环。去掉代理变量，避免进程把自己的监听地址送去代理。
  env -u http_proxy -u HTTP_PROXY -u https_proxy -u HTTPS_PROXY \
    -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
    nohup pyghidra-mcp \
    --transport streamable-http \
    --host 127.0.0.1 \
    --port 8000 \
    --project-path "$project_dir" \
    --project-name echo-agent \
    >>/tmp/pyghidra-mcp.log 2>&1 &
fi

# pyghidra-mcp 在后台启动。等它开始监听后再交给主进程，避免 Codex 立刻连上被拒绝。
if python3 - <<'PY'
import socket
import sys
import time

deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        socket.create_connection(("127.0.0.1", 8000), 0.3).close()
    except OSError:
        time.sleep(0.5)
        continue
    sys.exit(0)
sys.exit(1)
PY
then
  echo "pyghidra-mcp is listening on 127.0.0.1:8000" >&2
else
  echo "pyghidra-mcp did not listen on 127.0.0.1:8000; continuing" >&2
fi

exec "$@"
