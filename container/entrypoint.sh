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
  nohup pyghidra-mcp \
    --transport streamable-http \
    --host 127.0.0.1 \
    --port 8000 \
    --project-path "$project_dir" \
    --project-name echo-agent \
    >>/tmp/pyghidra-mcp.log 2>&1 &
fi

exec "$@"
