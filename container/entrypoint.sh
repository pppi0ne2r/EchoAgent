#!/bin/sh
set -eu

project_dir="${PYGHIDRA_PROJECT_PATH:-/home/kali/ghidra-projects}"
config_dst="/home/kali/.codex/config.toml"
mkdir -p "$project_dir" /home/kali/.codex
if [ ! -s "$config_dst" ]; then
  cp /opt/echo-agent/codex-config.toml "$config_dst"
fi

start_clash() {
  subscription="${ECHO_CLASH_SUBSCRIPTION_URL:-}"
  clash_dir="/home/kali/.config/mihomo"
  if [ -z "$subscription" ]; then
    echo "clash enabled but subscription url is empty" >&2
    return 0
  fi
  mkdir -p "$clash_dir"
  tmp="$(mktemp)"
  # 此时 mihomo 还没监听，不能让 curl 走容器里的代理变量。
  if ! env -u http_proxy -u HTTP_PROXY -u https_proxy -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    curl -fsSL --retry 2 -A clash.meta --max-time 30 -o "$tmp" "$subscription"; then
    echo "clash subscription download failed" >&2
    rm -f "$tmp"
    return 0
  fi
  if ! python3 /opt/echo-agent/clash_config.py "$tmp" "$clash_dir/config.yaml"; then
    echo "clash subscription is not a usable mihomo config" >&2
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  chmod 600 "$clash_dir/config.yaml" || true
  echo "starting mihomo on 127.0.0.1:7890" >&2
  nohup mihomo -d "$clash_dir" >>/tmp/mihomo.log 2>&1 &
}

wait_for_clash() {
  i=0
  while [ "$i" -lt 20 ]; do
    if python3 -c 'import socket; socket.create_connection(("127.0.0.1", 7890), 0.3).close()' >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "mihomo did not listen on 127.0.0.1:7890 within 20s" >&2
  return 0
}

if ! python3 -c 'import socket; socket.create_connection(("127.0.0.1", 8000), 0.3).close()' >/dev/null 2>&1; then
  echo "starting pyghidra-mcp on 127.0.0.1:8000" >&2
  env -u http_proxy -u HTTP_PROXY -u https_proxy -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    nohup pyghidra-mcp \
    --transport streamable-http \
    --host 127.0.0.1 \
    --port 8000 \
    --project-path "$project_dir" \
    --project-name echo-agent \
    >>/tmp/pyghidra-mcp.log 2>&1 &
fi

if [ "${ECHO_CLASH_ENABLED:-}" = "1" ]; then
  start_clash
  wait_for_clash
fi

exec "$@"
