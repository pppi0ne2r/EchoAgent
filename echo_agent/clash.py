from __future__ import annotations


CLASH_HTTP_PROXY = "http://127.0.0.1:7890"
CLASH_SOCKS_PROXY = "socks5h://127.0.0.1:7890"
CLASH_NO_PROXY = "localhost,127.0.0.1,::1"
_TRACKED_KEYS = (
    "ECHO_CLASH_ENABLED",
    "ECHO_CLASH_SUBSCRIPTION_URL",
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def desired_clash_environment() -> dict[str, str]:
    from echo_agent.config import DispatcherSettings

    try:
        settings = DispatcherSettings.from_env()
    except Exception:
        return {}
    if not settings.clash_enabled:
        return {}
    subscription = settings.clash_subscription_url.strip()
    if not subscription:
        return {}
    return {
        "ECHO_CLASH_ENABLED": "1",
        "ECHO_CLASH_SUBSCRIPTION_URL": subscription,
        "http_proxy": CLASH_HTTP_PROXY,
        "HTTP_PROXY": CLASH_HTTP_PROXY,
        "https_proxy": CLASH_HTTP_PROXY,
        "HTTPS_PROXY": CLASH_HTTP_PROXY,
        "all_proxy": CLASH_SOCKS_PROXY,
        "ALL_PROXY": CLASH_SOCKS_PROXY,
        "no_proxy": CLASH_NO_PROXY,
        "NO_PROXY": CLASH_NO_PROXY,
    }


def clash_docker_arguments() -> list[str]:
    arguments: list[str] = []
    for key, value in desired_clash_environment().items():
        arguments.extend(["-e", f"{key}={value}"])
    return arguments


def clash_environment_changed(actual: list[str], desired: dict[str, str] | None = None) -> bool:
    expected = desired_clash_environment() if desired is None else desired
    actual_map: dict[str, str] = {}
    for item in actual:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        actual_map[key] = value
    return any(actual_map.get(key, "") != expected.get(key, "") for key in _TRACKED_KEYS)
