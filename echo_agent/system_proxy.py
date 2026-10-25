"""把 macOS 系统代理转成容器里可用的环境变量。

VPN 客户端开启系统代理后，宿主机的 urllib 会直接读取它。
容器里的 127.0.0.1 不是宿主机，所以回环地址要改写到 echo-agent-host。
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CONTAINER_PROXY_HOST = "echo-agent-host"
_NO_PROXY = (
    "localhost,127.0.0.1,::1,host.docker.internal,"
    "gateway.docker.internal,echo-agent-host"
)


def system_proxy_settings() -> dict[str, object]:
    try:
        import _scproxy
    except ImportError:
        return {}
    try:
        settings = _scproxy._get_proxy_settings()
    except Exception:
        return {}
    return settings if isinstance(settings, dict) else {}


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
    return container_proxy_environment()


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
    environment = configured_proxy_environment()
    if not environment:
        return proxy_clear_arguments()
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


def proxy_build_arguments(environment: Mapping[str, str]) -> list[str]:
    arguments: list[str] = []
    for key in _PROXY_KEYS:
        if key in environment:
            arguments.extend(["--build-arg", f"{key}={environment[key]}"])
    if not arguments:
        return []
    return ["--add-host", f"{CONTAINER_PROXY_HOST}:host-gateway", *arguments]


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
