from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.config import DispatcherSettings
from echo_agent.console_settings import public_dispatcher_settings, save_console_settings


MINIMAL_CONFIG = """
[server]
host = "127.0.0.1"
port = 8000
database_path = "state.db"

[dispatcher]
server_url = "http://127.0.0.1:8000"
interval_seconds = 3
explore_max_workers = 4
codex_max_slots = 2

[codex]
command = ["codex", "exec", "-"]
model = "old-model"
first_run_model = "old-model"
explore_model = "old-model"
reason_model = "old-model"
workspace = "/workspace"
timeout_seconds = 30
no_output_timeout_seconds = 10
first_run_no_output_timeout_seconds = 10
explore_no_output_timeout_seconds = 10
reason_no_output_timeout_seconds = 10
api_key = "original-key"
base_url = "https://old.example/v1"
provider_name = "test_proxy"
supports_websockets = false
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = 1000

[docker]
image = "worker:test"
context = "container"
dockerfile = "container/Dockerfile"

[resources]
root = "resources"
items = []
"""


class ConsoleSettingsTests(unittest.TestCase):
    def test_saved_settings_override_toml_without_exposing_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "echo_agent.toml"
            config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")
            with patch.dict(os.environ, {"ECHO_AGENT_CONFIG": str(config_path)}, clear=True):
                saved = save_console_settings(
                    max_concurrent_projects=3,
                    codex_max_slots=5,
                    explore_max_workers=6,
                    base_url="https://gateway.example/v1",
                    model="grok-4.7",
                    api_key="sk-secret-value",
                )
                loaded = DispatcherSettings.from_env()
                visible = public_dispatcher_settings()

        self.assertNotIn("sk-secret-value", str(saved))
        self.assertTrue(saved["api_key_set"])
        self.assertEqual(saved["api_key_hint"], "alue")
        self.assertEqual(saved["model"], "grok-4.7")
        self.assertTrue(saved["models_unified"])
        self.assertEqual(loaded.max_concurrent_projects, 3)
        self.assertEqual(loaded.codex_max_slots, 5)
        self.assertEqual(loaded.explore_max_workers, 6)
        self.assertEqual(loaded.codex_model, "grok-4.7")
        self.assertEqual(loaded.codex_mode_models["explore"], "grok-4.7")
        self.assertEqual(loaded.openai_base_url, "https://gateway.example/v1")
        self.assertEqual(loaded.openai_api_key, "sk-secret-value")
        self.assertEqual(visible["api_key_hint"], "alue")
        self.assertNotIn("sk-secret-value", str(visible))

    def test_blank_key_keeps_the_saved_key_and_env_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "echo_agent.toml"
            config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")
            with patch.dict(os.environ, {"ECHO_AGENT_CONFIG": str(config_path)}, clear=True):
                save_console_settings(
                    max_concurrent_projects=2,
                    codex_max_slots=2,
                    explore_max_workers=4,
                    base_url="https://gateway.example/v1",
                    model="grok-4.7",
                    api_key="sk-secret-value",
                )
                kept = save_console_settings(
                    max_concurrent_projects=2,
                    codex_max_slots=2,
                    explore_max_workers=4,
                    base_url="https://gateway.example/v1",
                    model="grok-4.7",
                    api_key="",
                )
                self.assertEqual(DispatcherSettings.from_env().openai_api_key, "sk-secret-value")
                with patch.dict(os.environ, {"ECHO_AGENT_CODEX_MAX_SLOTS": "9"}):
                    overridden = DispatcherSettings.from_env()

        self.assertTrue(kept["api_key_set"])
        self.assertEqual(overridden.codex_max_slots, 9)

if __name__ == "__main__":
    unittest.main()
