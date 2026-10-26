"""把系统代理转成容器里可用的环境变量。

macOS 从系统代理接口读取。Linux 先读 GNOME 手动代理，没有时再读
http_proxy、https_proxy 和 all_proxy。这两处都没有时，再识别本机回环上
已经在监听的 HTTP 或 SOCKS 代理。容器里的 127.0.0.1 不是宿主机，
所以回环地址要改写到 echo-agent-host。Linux 上只监听回环的代理，
会在 Docker 网桥地址上补一个转发，容器才能连到宿主机。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CONTAINER_PROXY_HOST = "echo-agent-host"
_NO_PROXY = (
    "localhost,127.0.0.1,::1,host.docker.internal,"
    "gateway.docker.internal,echo-agent-host"
)
_GNOME_PROXY_KEYS = (
    ("HTTP", "org.gnome.system.proxy.http"),
    ("HTTPS", "org.gnome.system.proxy.https"),
    ("SOCKS", "org.gnome.system.proxy.socks"),
)
_PROXY_PROCESS_MARKERS = (
    "clash",
    "mihomo",
    "xray",
    "v2ray",
    "sing-box",
    "hysteria",
    "naiveproxy",
    "shadowsocks",
    "ss-local",
    "trojan",
    "gost",
    "privoxy",
    "tinyproxy",
    "polipo",
    "squid",
    "redsocks",
    "nekoray",
    "nekobox",
    "v2raya",
    "hiddify",
)
_HTTP_PROXY_PORTS = (7890, 7892, 7897, 7893, 10809, 20171, 2080, 8118, 8888, 6152, 1087)
_SOCKS_PROXY_PORTS = (7891, 1080, 10808, 20170, 6153)
_LOOPBACK_HEX = {
    "0100007f",
    "00000000000000000000000001000000",
}


def system_proxy_settings() -> dict[str, object]:
    native = _native_proxy_settings()
    if _settings_have_proxy(native):
        return native
    environment = _environment_proxy_settings()
    if _settings_have_proxy(environment):
        return environment
    return _local_proxy_settings()


def _native_proxy_settings() -> dict[str, object]:
    if "_scproxy" not in sys.modules:
        try:
            import _scproxy
        except ImportError:
            return _gnome_proxy_settings()
    module = sys.modules.get("_scproxy")
    if module is None:
        return _gnome_proxy_settings()
    try:
        settings = _scproxy._get_proxy_settings()
    except Exception:
        return {}
    return settings if isinstance(settings, dict) else {}


def _settings_have_proxy(settings: Mapping[str, object]) -> bool:
    return any(_enabled_proxy(settings, name) for name in ("HTTP", "HTTPS", "SOCKS"))


def _environment_proxy_settings() -> dict[str, object]:
    settings: dict[str, object] = {}
    http = _parse_proxy_endpoint(os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "")
    https = _parse_proxy_endpoint(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "")
    socks_raw = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or ""
    socks = _parse_proxy_endpoint(socks_raw)
    if http is not None:
        host, port = http
        settings.update(HTTPEnable=1, HTTPProxy=host, HTTPPort=port)
    if https is not None:
        host, port = https
        settings.update(HTTPSEnable=1, HTTPSProxy=host, HTTPSPort=port)
    if socks is not None and socks_raw.lower().startswith("socks"):
        host, port = socks
        settings.update(SOCKSEnable=1, SOCKSProxy=host, SOCKSPort=port)
    return settings


def _parse_proxy_endpoint(value: str) -> tuple[str, int] | None:
    text = value.strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    host = parts.hostname or ""
    port = parts.port
    if not host or port is None or not 1 <= port <= 65535:
        return None
    return host, port


def _gnome_proxy_settings() -> dict[str, object]:
    """读取 GNOME 手动代理。自动模式和读取失败都按没有可用代理处理。"""
    mode = _gsettings_get("org.gnome.system.proxy", "mode")
    if mode != "manual":
        return {}
    settings: dict[str, object] = {}
    for name, schema in _GNOME_PROXY_KEYS:
        host = _gsettings_get(schema, "host")
        port = _gsettings_port(_gsettings_get(schema, "port"))
        if host and port is not None:
            settings[f"{name}Enable"] = 1
            settings[f"{name}Proxy"] = host
            settings[f"{name}Port"] = port
    return settings


def _gsettings_get(schema: str, key: str) -> str:
    try:
        completed = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return _gsettings_value(completed.stdout)


def _gsettings_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1]
    return value.strip()


def _gsettings_port(raw: str) -> int | None:
    try:
        port = int(raw)
    except ValueError:
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _local_proxy_settings() -> dict[str, object]:
    """GNOME 和进程环境都没有代理时，使用本机回环上实际在监听的代理。"""
    listening = _loopback_listen_ports()
    if not listening:
        return {}
    owned = _proxy_process_listen_ports()
    for port in _ordered_proxy_ports(owned, listening, _HTTP_PROXY_PORTS):
        if _looks_like_http_proxy(port):
            return _endpoint_settings("HTTP", port) | _endpoint_settings("HTTPS", port)
    for port in _ordered_proxy_ports(owned, listening, _SOCKS_PROXY_PORTS):
        if _looks_like_socks_proxy(port):
            return _endpoint_settings("SOCKS", port)
    return {}


def _endpoint_settings(name: str, port: int) -> dict[str, object]:
    return {
        f"{name}Enable": 1,
        f"{name}Proxy": "127.0.0.1",
        f"{name}Port": port,
    }


def _ordered_proxy_ports(
    owned: set[int],
    listening: set[int],
    preferred: tuple[int, ...],
) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()

    def add(port: int) -> None:
        if port in seen or port not in listening:
            return
        seen.add(port)
        ordered.append(port)

    for port in preferred:
        if port in owned:
            add(port)
    for port in sorted(owned):
        add(port)
    for port in preferred:
        add(port)
    return ordered


def _loopback_listen_ports() -> set[int]:
    ports: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ports.update(_loopback_ports_in_proc(text))
    return ports


def _loopback_ports_in_proc(text: str) -> set[int]:
    return set(_loopback_listen_inodes(text).values())


def _loopback_listen_inodes(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        address, separator, port_hex = fields[1].partition(":")
        if separator != ":" or address.lower() not in _LOOPBACK_HEX:
            continue
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            found[fields[9]] = port
    return found


def _proxy_process_listen_ports() -> set[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return set()
    inodes: dict[str, int] = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        inodes.update(_loopback_listen_inodes(text))
    if not inodes:
        return set()
    ports: set[int] = set()
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit() or not _process_looks_like_proxy(pid_dir):
            continue
        try:
            descriptors = list((pid_dir / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if not target.startswith("socket:[") or not target.endswith("]"):
                continue
            port = inodes.get(target[len("socket:["):-1])
            if port is not None:
                ports.add(port)
    return ports


def _process_looks_like_proxy(pid_dir: Path) -> bool:
    try:
        command = (pid_dir / "comm").read_text(encoding="utf-8", errors="replace")
        arguments = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return _identity_is_proxy(command, arguments)


def _identity_is_proxy(command: str, arguments: str) -> bool:
    identity = f"{command} {arguments}".lower()
    return any(marker in identity for marker in _PROXY_PROCESS_MARKERS)


def _looks_like_http_proxy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4) as sock:
            sock.settimeout(0.4)
            sock.sendall(
                b"GET http://127.0.0.1:1/ HTTP/1.1\r\n"
                b"Host: 127.0.0.1:1\r\n"
                b"Connection: close\r\n\r\n"
            )
            data = sock.recv(16)
    except OSError:
        return False
    return data.startswith(b"HTTP/")


def _looks_like_socks_proxy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4) as sock:
            sock.settimeout(0.4)
            sock.sendall(b"\x05\x01\x00")
            data = sock.recv(2)
    except OSError:
        return False
    return data == b"\x05\x00"


def system_proxy_enabled() -> bool:
    """控制台或配置里的开关。读取失败时按关闭处理，避免代理配置拖垮启动。"""
    from echo_agent.config import DispatcherSettings

    try:
        return bool(DispatcherSettings.from_env().use_system_proxy)
    except Exception:
        return False


def configured_proxy_environment() -> dict[str, str]:
    if not system_proxy_enabled():
        return {}
    environment = container_proxy_environment(system_proxy_settings())
    if environment:
        ensure_container_proxy_reachable(environment)
    return environment


def container_proxy_environment(settings: Mapping[str, object] | None = None) -> dict[str, str]:
    source = system_proxy_settings() if settings is None else dict(settings)
    http_url = _enabled_proxy(source, "HTTP")
    https_url = _enabled_proxy(source, "HTTPS") or http_url
    socks_url = _enabled_proxy(source, "SOCKS", scheme="socks5h")
    environment: dict[str, str] = {}
    if http_url:
        environment["http_proxy"] = environment["HTTP_PROXY"] = _for_container(http_url)
    if https_url:
        environment["https_proxy"] = environment["HTTPS_PROXY"] = _for_container(https_url)
    if socks_url and not http_url and not https_url:
        rewritten = _for_container(socks_url)
        environment["all_proxy"] = environment["ALL_PROXY"] = rewritten
    if environment:
        environment["no_proxy"] = environment["NO_PROXY"] = _NO_PROXY
    return environment


_PROXY_KEYS = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def proxy_clear_arguments() -> list[str]:
    arguments: list[str] = []
    for key in _PROXY_KEYS:
        arguments.extend(["-e", f"{key}="])
    return arguments


def proxy_exec_arguments() -> list[str]:
    if not system_proxy_enabled():
        return proxy_clear_arguments()
    environment = configured_proxy_environment()
    if not environment:
        return []
    return proxy_environment_arguments(environment)


def proxy_environment_arguments(environment: Mapping[str, str]) -> list[str]:
    arguments: list[str] = []
    for key in _PROXY_KEYS:
        if key in environment:
            arguments.extend(["-e", f"{key}={environment[key]}"])
    return arguments


def proxy_docker_arguments(environment: Mapping[str, str]) -> list[str]:
    environment_arguments = proxy_environment_arguments(environment)
    if not environment_arguments:
        return []
    return ["--add-host", f"{CONTAINER_PROXY_HOST}:host-gateway", *environment_arguments]


def container_proxy_signature(container: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """返回容器里的代理环境变量和 echo-agent-host 映射；检查失败时返回 None。"""
    import json

    if not container_exists(container):
        return None
    try:
        completed = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Config.Env}}\n{{json .HostConfig.ExtraHosts}}", container],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = completed.stdout.splitlines()
    if len(lines) != 2:
        return None
    try:
        environment = json.loads(lines[0])
        extra_hosts = json.loads(lines[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(environment, list) or not isinstance(extra_hosts, list):
        return None
    proxy_environment = tuple(
        sorted(str(item) for item in environment if _is_proxy_assignment(item))
    )
    proxy_hosts = tuple(
        sorted(str(item) for item in extra_hosts if str(item).startswith(f"{CONTAINER_PROXY_HOST}:"))
    )
    return proxy_environment, proxy_hosts


def proxy_signature(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """用当前配置生成和容器检查结果可比较的代理签名。"""
    source = configured_proxy_environment() if environment is None else dict(environment)
    assignments = tuple(sorted(f"{key}={source[key]}" for key in _PROXY_KEYS if key in source))
    hosts = (f"{CONTAINER_PROXY_HOST}:host-gateway",) if assignments else ()
    return assignments, hosts


def container_exists(container: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "container", "inspect", container],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _is_proxy_assignment(value: object) -> bool:
    if not isinstance(value, str) or "=" not in value:
        return False
    key, _, _ = value.partition("=")
    return key in _PROXY_KEYS


def proxy_build_arguments(environment: Mapping[str, str]) -> list[str]:
    arguments: list[str] = []
    for key in _PROXY_KEYS:
        if key in environment:
            arguments.extend(["--build-arg", f"{key}={environment[key]}"])
    if not arguments:
        return []
    return ["--add-host", f"{CONTAINER_PROXY_HOST}:host-gateway", *arguments]


_forwarded_ports: set[int] = set()
_forwarder_lock = threading.Lock()


def ensure_container_proxy_reachable(environment: Mapping[str, str]) -> None:
    """Linux 上把只监听 127.0.0.1 的代理转到 Docker 网桥地址。"""
    for port in _container_proxy_ports(environment):
        _ensure_forwarder(port)


def _container_proxy_ports(environment: Mapping[str, str]) -> set[int]:
    ports: set[int] = set()
    for key in ("http_proxy", "https_proxy", "all_proxy"):
        parts = urlsplit(environment.get(key) or "")
        if parts.hostname == CONTAINER_PROXY_HOST and parts.port:
            ports.add(parts.port)
    return ports


def _ensure_forwarder(port: int) -> None:
    with _forwarder_lock:
        if port in _forwarded_ports:
            return
        gateway = _docker_bridge_gateway()
        if not gateway or _tcp_open(gateway, port) or not _tcp_open("127.0.0.1", port):
            return
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((gateway, port))
            listener.listen(32)
        except OSError:
            return
        _forwarded_ports.add(port)
        threading.Thread(
            target=_accept_proxy_forward,
            args=(listener, port),
            daemon=True,
            name=f"echo-agent-proxy-{port}",
        ).start()


def _docker_bridge_gateway() -> str:
    try:
        completed = subprocess.run(
            ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    gateway = completed.stdout.strip()
    if not gateway or gateway == "<no value>":
        return ""
    return gateway


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _accept_proxy_forward(listener: socket.socket, port: int) -> None:
    try:
        while True:
            client, _address = listener.accept()
            threading.Thread(target=_bridge_proxy, args=(client, port), daemon=True).start()
    except OSError:
        return


def _bridge_proxy(client: socket.socket, port: int) -> None:
    try:
        remote = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        client.close()
        return
    left = threading.Thread(target=_pipe_proxy, args=(client, remote), daemon=True)
    right = threading.Thread(target=_pipe_proxy, args=(remote, client), daemon=True)
    left.start()
    right.start()


def _pipe_proxy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            destination.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, destination):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _enabled_proxy(settings: Mapping[str, object], name: str, scheme: str = "http") -> str | None:
    if not _truthy(settings.get(f"{name}Enable")):
        return None
    host = str(settings.get(f"{name}Proxy") or "").strip()
    port = settings.get(f"{name}Port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or not host:
        return None
    return f"{scheme}://{host}:{port}"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "yes"}


def _for_container(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host not in _LOOPBACK:
        return url
    port = parts.port
    netloc = CONTAINER_PROXY_HOST if port is None else f"{CONTAINER_PROXY_HOST}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
