from __future__ import annotations

from echo_agent.config import DispatcherSettings, console_settings_path


def public_dispatcher_settings() -> dict[str, object]:
    settings = DispatcherSettings.from_env()
    return _public_settings(settings)


def save_console_settings(
    *,
    max_concurrent_projects: int,
    codex_max_slots: int,
    explore_max_workers: int,
    base_url: str,
    model: str,
    api_key: str | None = None,
    clear_api_key: bool = False,
    clash_enabled: bool = False,
    clash_subscription_url: str = "",
    container_memory_mb: int = 0,
    container_cpus: float = 0,
) -> dict[str, object]:
    if max_concurrent_projects < 1:
        raise ValueError("max_concurrent_projects must be at least 1")
    if codex_max_slots < 1:
        raise ValueError("codex_max_slots must be at least 1")
    if explore_max_workers < 1:
        raise ValueError("explore_max_workers must be at least 1")
    cleaned_url = base_url.strip().rstrip("/")
    if not cleaned_url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    cleaned_model = model.strip()
    if not cleaned_model or any(char in cleaned_model for char in "\n\r"):
        raise ValueError("model cannot be empty")
    subscription = clash_subscription_url.strip()
    if subscription and (
        not subscription.startswith(("http://", "https://")) or any(char.isspace() for char in subscription)
    ):
        raise ValueError("Clash 订阅地址必须以 http:// 或 https:// 开头，且不能包含空白")
    if clash_enabled and not subscription:
        raise ValueError("启用 Clash 时需要填写 http 或 https 订阅地址")
    if isinstance(container_memory_mb, bool) or not isinstance(container_memory_mb, int):
        raise ValueError("容器内存上限需要是整数")
    if container_memory_mb < 0 or container_memory_mb > 131072:
        raise ValueError("容器内存上限需要在 0 到 131072 MB 之间，0 表示不限制")
    try:
        cpus = float(container_cpus)
    except (TypeError, ValueError) as exc:
        raise ValueError("容器 CPU 上限需要是数字") from exc
    if isinstance(container_cpus, bool) or cpus != cpus or cpus < 0 or cpus > 64:
        raise ValueError("容器 CPU 上限需要在 0 到 64 之间，0 表示不限制")
    if clear_api_key:
        stored_key = ""
    elif api_key and api_key.strip():
        stored_key = api_key.strip()
    else:
        stored_key = DispatcherSettings.from_env().openai_api_key or ""

    path = console_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(
        "\n".join(
            [
                "# Written by the EchoAgent console. Environment variables still win.",
                "[dispatcher]",
                f"max_concurrent_projects = {max_concurrent_projects}",
                f"explore_max_workers = {explore_max_workers}",
                f"codex_max_slots = {codex_max_slots}",
                "",
                "[codex]",
                f"model = {_toml_string(cleaned_model)}",
                f"first_run_model = {_toml_string(cleaned_model)}",
                f"explore_model = {_toml_string(cleaned_model)}",
                f"reason_model = {_toml_string(cleaned_model)}",
                f"base_url = {_toml_string(cleaned_url)}",
                f"api_key = {_toml_string(stored_key)}",
                "",
                "[clash]",
                f"enabled = {'true' if clash_enabled else 'false'}",
                f"subscription_url = {_toml_string(subscription)}",
                "",
                "[docker]",
                f"memory_mb = {container_memory_mb}",
                f"cpus = {_toml_cpus(cpus)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return public_dispatcher_settings()


def _public_settings(settings: DispatcherSettings) -> dict[str, object]:
    key = settings.openai_api_key or ""
    models = {
        item
        for item in (
            settings.codex_model,
            settings.codex_first_run_model,
            settings.codex_explore_model,
            settings.codex_reason_model,
        )
        if item
    }
    return {
        "max_concurrent_projects": settings.max_concurrent_projects,
        "codex_max_slots": settings.codex_max_slots,
        "explore_max_workers": settings.explore_max_workers,
        "base_url": settings.openai_base_url or "",
        "model": settings.codex_model or settings.codex_explore_model or "",
        "models_unified": len(models) <= 1,
        "api_key_set": bool(key),
        "api_key_hint": key[-4:] if len(key) > 4 else "",
        "clash_enabled": settings.clash_enabled,
        "clash_subscription_url": settings.clash_subscription_url,
        "container_memory_mb": settings.container_memory_mb,
        "container_cpus": settings.container_cpus,
    }


def _toml_cpus(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
