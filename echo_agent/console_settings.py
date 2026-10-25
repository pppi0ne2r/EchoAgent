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
    use_system_proxy: bool = False,
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
                "[docker]",
                f"use_system_proxy = {'true' if use_system_proxy else 'false'}",
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
        "use_system_proxy": settings.use_system_proxy,
    }


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
