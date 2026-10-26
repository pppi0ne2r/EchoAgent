from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("echo_agent.toml")
DEFAULT_CONFIG_EXAMPLE_PATH = Path("echo_agent.toml.example")
CONFIG_DIR_KEY = "__config_dir__"


@dataclass(frozen=True, slots=True)
class ProjectResource:
    name: str
    path: Path
    container_path: str
    url: str | None = None
    archive: bool = False
    archive_single_file: bool = False
    executable: bool = False
    read_only: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerSettings:
    host: str
    port: int
    database_path: Path
    auth_token: str | None = None

    @classmethod
    def from_env(cls) -> "ServerSettings":
        config = _load_config()
        return cls(
            host=_setting("ECHO_AGENT_HOST", config, "server", "host"),
            port=int(_setting("ECHO_AGENT_PORT", config, "server", "port")),
            database_path=_path_setting(
                config,
                _setting("ECHO_AGENT_DB", config, "server", "database_path")
            ),
            auth_token=_optional_config_setting(
                "ECHO_AGENT_SERVER_AUTH_TOKEN", config, "server", "auth_token"
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatcherSettings:
    server_url: str
    server_auth_token: str | None = None
    interval_seconds: float
    explore_max_workers: int
    codex_max_slots: int = 2
    max_concurrent_projects: int = 2
    codex_model: str | None = None
    codex_first_run_model: str | None = None
    codex_explore_model: str | None = None
    codex_reason_model: str | None = None
    codex_workspace: str
    codex_timeout_seconds: int
    codex_first_run_timeout_seconds: int
    codex_explore_timeout_seconds: int
    codex_reason_timeout_seconds: int
    codex_no_output_timeout_seconds: int
    codex_first_run_no_output_timeout_seconds: int
    codex_explore_no_output_timeout_seconds: int
    codex_reason_no_output_timeout_seconds: int
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    project_docker_image: str
    project_docker_context: Path
    project_dockerfile: Path
    project_docker_build_network: str | None
    clash_enabled: bool = False
    clash_subscription_url: str = ""
    container_memory_mb: int = 0
    container_cpus: float = 0.0
    project_resource_root: Path
    project_resources: tuple[ProjectResource, ...] = ()
    codex_command: tuple[str, ...]
    codex_provider_name: str
    codex_provider_supports_websockets: bool
    codex_request_max_retries: int
    codex_stream_max_retries: int
    codex_stream_idle_timeout_ms: int

    @classmethod
    def from_env(cls) -> "DispatcherSettings":
        config = _load_config()
        return cls(
            server_url=_setting(
                "ECHO_AGENT_SERVER_URL", config, "dispatcher", "server_url"
            ),
            server_auth_token=_optional_config_setting(
                "ECHO_AGENT_SERVER_AUTH_TOKEN", config, "server", "auth_token"
            ),
            interval_seconds=float(
                _setting(
                    "ECHO_AGENT_DISPATCH_INTERVAL",
                    config,
                    "dispatcher",
                    "interval_seconds",
                )
            ),
            explore_max_workers=int(
                _setting(
                    "ECHO_AGENT_EXPLORE_MAX_WORKERS",
                    config,
                    "dispatcher",
                    "explore_max_workers",
                )
            ),
            codex_max_slots=int(
                _setting(
                    "ECHO_AGENT_CODEX_MAX_SLOTS",
                    config,
                    "dispatcher",
                    "codex_max_slots",
                    default=2,
                )
            ),
            max_concurrent_projects=int(
                _setting(
                    "ECHO_AGENT_MAX_CONCURRENT_PROJECTS",
                    config,
                    "dispatcher",
                    "max_concurrent_projects",
                    default=2,
                )
            ),
            codex_model=_optional_config_setting("ECHO_AGENT_CODEX_MODEL", config, "codex", "model"),
            codex_first_run_model=_optional_config_setting(
                "ECHO_AGENT_CODEX_FIRST_RUN_MODEL", config, "codex", "first_run_model"
            ),
            codex_explore_model=_optional_config_setting(
                "ECHO_AGENT_CODEX_EXPLORE_MODEL", config, "codex", "explore_model"
            ),
            codex_reason_model=_optional_config_setting(
                "ECHO_AGENT_CODEX_REASON_MODEL", config, "codex", "reason_model"
            ),
            codex_workspace=_setting(
                "ECHO_AGENT_CODEX_WORKSPACE", config, "codex", "workspace"
            ),
            codex_timeout_seconds=int(
                _setting(
                    "ECHO_AGENT_CODEX_TIMEOUT",
                    config,
                    "codex",
                    "timeout_seconds",
                )
            ),
            codex_first_run_timeout_seconds=int(
                _optional_config_setting(
                    "ECHO_AGENT_CODEX_FIRST_RUN_TIMEOUT",
                    config,
                    "codex",
                    "first_run_timeout_seconds",
                )
                or _setting("ECHO_AGENT_CODEX_TIMEOUT", config, "codex", "timeout_seconds")
            ),
            codex_explore_timeout_seconds=int(
                _optional_config_setting(
                    "ECHO_AGENT_CODEX_EXPLORE_TIMEOUT",
                    config,
                    "codex",
                    "explore_timeout_seconds",
                )
                or _setting("ECHO_AGENT_CODEX_TIMEOUT", config, "codex", "timeout_seconds")
            ),
            codex_reason_timeout_seconds=int(
                _optional_config_setting(
                    "ECHO_AGENT_CODEX_REASON_TIMEOUT",
                    config,
                    "codex",
                    "reason_timeout_seconds",
                )
                or _setting("ECHO_AGENT_CODEX_TIMEOUT", config, "codex", "timeout_seconds")
            ),
            codex_no_output_timeout_seconds=int(
                _setting(
                    "ECHO_AGENT_CODEX_NO_OUTPUT_TIMEOUT",
                    config,
                    "codex",
                    "no_output_timeout_seconds",
                )
            ),
            codex_first_run_no_output_timeout_seconds=int(
                _setting(
                    "ECHO_AGENT_CODEX_FIRST_RUN_NO_OUTPUT_TIMEOUT",
                    config,
                    "codex",
                    "first_run_no_output_timeout_seconds",
                )
            ),
            codex_explore_no_output_timeout_seconds=int(
                _setting(
                    "ECHO_AGENT_CODEX_EXPLORE_NO_OUTPUT_TIMEOUT",
                    config,
                    "codex",
                    "explore_no_output_timeout_seconds",
                )
            ),
            codex_reason_no_output_timeout_seconds=int(
                _setting(
                    "ECHO_AGENT_CODEX_REASON_NO_OUTPUT_TIMEOUT",
                    config,
                    "codex",
                    "reason_no_output_timeout_seconds",
                )
            ),
            openai_api_key=_first_optional_setting(
                "OPENAI_API_KEY",
                "ECHO_AGENT_OPENAI_API_KEY",
                default=_config_value(config, "codex", "api_key"),
            ),
            openai_base_url=_first_optional_setting(
                "OPENAI_BASE_URL",
                "ECHO_AGENT_OPENAI_BASE_URL",
                default=_config_value(config, "codex", "base_url"),
            ),
            project_docker_image=_setting(
                "ECHO_AGENT_PROJECT_DOCKER_IMAGE",
                config,
                "docker",
                "image",
            ),
            project_docker_context=_path_setting(
                config,
                _setting(
                    "ECHO_AGENT_PROJECT_DOCKER_CONTEXT",
                    config,
                    "docker",
                    "context",
                )
            ),
            project_dockerfile=_path_setting(
                config,
                _setting(
                    "ECHO_AGENT_PROJECT_DOCKERFILE",
                    config,
                    "docker",
                    "dockerfile",
                )
            ),
            project_docker_build_network=_optional_setting(
                "ECHO_AGENT_PROJECT_DOCKER_BUILD_NETWORK",
                _config_value(config, "docker", "build_network"),
            ),
            clash_enabled=_optional_bool(
                "ECHO_AGENT_CLASH_ENABLED",
                _config_value(config, "clash", "enabled", default=False),
            ),
            clash_subscription_url=_optional_setting(
                "ECHO_AGENT_CLASH_SUBSCRIPTION_URL",
                _config_value(config, "clash", "subscription_url", default=""),
            )
            or "",
            container_memory_mb=_bounded_int(
                "ECHO_AGENT_CONTAINER_MEMORY_MB",
                _config_value(config, "docker", "memory_mb", default=0),
                maximum=131072,
                label="docker.memory_mb",
            ),
            container_cpus=_bounded_float(
                "ECHO_AGENT_CONTAINER_CPUS",
                _config_value(config, "docker", "cpus", default=0),
                maximum=64,
                label="docker.cpus",
            ),
            project_resource_root=_path_setting(
                config,
                _setting(
                    "ECHO_AGENT_PROJECT_RESOURCE_ROOT",
                    config,
                    "resources",
                    "root",
                )
            ),
            project_resources=_resource_settings(config),
            codex_command=_command_setting(config),
            codex_provider_name=str(
                _required_config_value(config, "codex", "provider_name")
            ),
            codex_provider_supports_websockets=_bool_config_value(
                config, "codex", "supports_websockets"
            ),
            codex_request_max_retries=int(
                _required_config_value(config, "codex", "request_max_retries")
            ),
            codex_stream_max_retries=int(
                _required_config_value(config, "codex", "stream_max_retries")
            ),
            codex_stream_idle_timeout_ms=int(
                _required_config_value(config, "codex", "stream_idle_timeout_ms")
            ),
        )

    @property
    def codex_mode_models(self) -> dict[str, str]:
        models: dict[str, str] = {}
        if self.codex_first_run_model:
            models["first_run"] = self.codex_first_run_model
        if self.codex_explore_model:
            models["explore"] = self.codex_explore_model
        if self.codex_reason_model:
            models["reason"] = self.codex_reason_model
        return models

    @property
    def codex_mode_no_output_timeout_seconds(self) -> dict[str, int]:
        return {
            "first_run": self.codex_first_run_no_output_timeout_seconds,
            "explore": self.codex_explore_no_output_timeout_seconds,
            "reason": self.codex_reason_no_output_timeout_seconds,
        }

    @property
    def codex_mode_timeout_seconds(self) -> dict[str, int]:
        return {
            "first_run": self.codex_first_run_timeout_seconds,
            "explore": self.codex_explore_timeout_seconds,
            "reason": self.codex_reason_timeout_seconds,
        }


def _optional_setting(env_name: str, default: Any = None) -> str | None:
    value = os.environ.get(env_name, default)
    if value is None or not str(value).strip():
        return None
    return str(value)


def _first_optional_setting(*env_names: str, default: Any = None) -> str | None:
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            return value
    if default is None or not str(default).strip():
        return None
    return str(default)


def _load_config() -> dict[str, Any]:
    config = _read_config_file(DEFAULT_CONFIG_EXAMPLE_PATH)
    path = Path(os.environ.get("ECHO_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    if path.exists():
        config = _merge_config(config, _read_config_file(path))
        config_dir = path.resolve().parent
    else:
        config_dir = Path.cwd()
    console_path = console_settings_path()
    if console_path.exists():
        config = _merge_config(config, _read_config_file(console_path))
    if "ECHO_AGENT_DATA_DIR" in os.environ:
        config = _merge_config(
            config,
            {
                "server": {"database_path": str(Path(os.environ["ECHO_AGENT_DATA_DIR"]) / "echo_agent.db")},
                "resources": {"root": str(Path(os.environ["ECHO_AGENT_DATA_DIR"]) / "resources")},
            },
        )
    config[CONFIG_DIR_KEY] = config_dir
    return config



def console_settings_path() -> Path:
    if data_dir := os.environ.get("ECHO_AGENT_DATA_DIR"):
        return Path(data_dir) / "console_settings.toml"
    path = Path(os.environ.get("ECHO_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    config_dir = path.resolve().parent if path.exists() else Path.cwd()
    return config_dir / ".echo_agent" / "console_settings.toml"


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)
    if not isinstance(data, dict):
        raise ValueError(f"EchoAgent config must be a TOML table: {path}")
    return data


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def _config_value(
    config: dict[str, Any], section: str, key: str, *, default: Any = None
) -> Any:
    values = config.get(section, {})
    if not isinstance(values, dict):
        raise ValueError(f"EchoAgent config section [{section}] must be a TOML table")
    return values.get(key, default)


def _required_config_value(config: dict[str, Any], section: str, key: str) -> Any:
    value = _config_value(config, section, key)
    if value is None:
        raise ValueError(f"EchoAgent config must define {section}.{key}")
    return value


def _setting(
    env_name: str,
    config: dict[str, Any],
    section: str,
    key: str,
    *,
    default: Any = None,
) -> Any:
    value = os.environ.get(env_name, _config_value(config, section, key, default=default))
    if value is None:
        raise ValueError(f"EchoAgent config must define {section}.{key}")
    return value


def _optional_config_setting(
    env_name: str,
    config: dict[str, Any],
    section: str,
    key: str,
    *,
    default: Any = None,
) -> str | None:
    return _optional_setting(env_name, _config_value(config, section, key, default=default))


def _path_setting(config: dict[str, Any], value: Any) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    config_dir = config.get(CONFIG_DIR_KEY)
    if isinstance(config_dir, Path):
        return config_dir / path
    return path


def _bounded_int(env_name: str, config_value: Any, *, maximum: int, label: str) -> int:
    value = _bounded_number(env_name, config_value, maximum=maximum, label=label)
    if value != int(value):
        raise ValueError(f"EchoAgent config {label} must be an integer")
    return int(value)


def _bounded_float(env_name: str, config_value: Any, *, maximum: float, label: str) -> float:
    return _bounded_number(env_name, config_value, maximum=maximum, label=label)


def _bounded_number(env_name: str, config_value: Any, *, maximum: float, label: str) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        raw = config_value
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        raise ValueError(f"EchoAgent config {label} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"EchoAgent config {label} must be a number") from exc
    if value != value or value < 0 or value > maximum:
        raise ValueError(f"EchoAgent config {label} must be between 0 and {maximum:g}")
    return value


def _optional_bool(env_name: str, config_value: Any) -> bool:
    raw = os.environ.get(env_name)
    if raw is None:
        raw = config_value
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError("EchoAgent config clash.enabled must be a boolean")


def _bool_config_value(
    config: dict[str, Any], section: str, key: str
) -> bool:
    value = _required_config_value(config, section, key)
    if not isinstance(value, bool):
        raise ValueError(f"EchoAgent config {section}.{key} must be a boolean")
    return value


def _command_setting(config: dict[str, Any]) -> tuple[str, ...]:
    env_value = os.environ.get("ECHO_AGENT_CODEX_COMMAND")
    if env_value:
        command = tuple(shlex.split(env_value))
    else:
        raw = _config_value(
            config,
            "codex",
            "command",
        )
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("EchoAgent config codex.command must be an array of strings")
        command = tuple(raw)
    if not command:
        raise ValueError("EchoAgent Codex command cannot be empty")
    return command


def _resource_settings(config: dict[str, Any]) -> tuple[ProjectResource, ...]:
    raw_items = _config_value(config, "resources", "items", default=[])
    if raw_items is None:
        return ()
    if not isinstance(raw_items, list):
        raise ValueError("EchoAgent config resources.items must be an array of tables")
    resources: list[ProjectResource] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError("EchoAgent config resources.items entries must be TOML tables")
        name = str(raw.get("name") or f"resource-{index + 1}")
        raw_path = raw.get("path")
        raw_container_path = raw.get("container_path")
        if not raw_path or not isinstance(raw_path, str):
            raise ValueError(f"EchoAgent resource {name!r} must define a string path")
        if not raw_container_path or not isinstance(raw_container_path, str):
            raise ValueError(f"EchoAgent resource {name!r} must define a string container_path")
        raw_url = raw.get("url")
        if raw_url is not None and not isinstance(raw_url, str):
            raise ValueError(f"EchoAgent resource {name!r} url must be a string")
        raw_read_only = raw.get("read_only", True)
        if not isinstance(raw_read_only, bool):
            raise ValueError(f"EchoAgent resource {name!r} read_only must be a boolean")
        raw_archive = raw.get("archive", False)
        if not isinstance(raw_archive, bool):
            raise ValueError(f"EchoAgent resource {name!r} archive must be a boolean")
        raw_archive_single_file = raw.get("archive_single_file", False)
        if not isinstance(raw_archive_single_file, bool):
            raise ValueError(f"EchoAgent resource {name!r} archive_single_file must be a boolean")
        raw_executable = raw.get("executable", False)
        if not isinstance(raw_executable, bool):
            raise ValueError(f"EchoAgent resource {name!r} executable must be a boolean")
        resources.append(
            ProjectResource(
                name=name,
                path=Path(raw_path),
                container_path=raw_container_path,
                url=raw_url or None,
                archive=raw_archive,
                archive_single_file=raw_archive_single_file,
                executable=raw_executable,
                read_only=raw_read_only,
            )
        )
    return tuple(resources)
