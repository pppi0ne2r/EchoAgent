from __future__ import annotations

import base64
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.clash import clash_docker_arguments, clash_environment_changed


def normalize_subscription(raw: bytes) -> str:
    path = Path("container/clash_config.py")
    spec = importlib.util.spec_from_file_location("clash_config", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.normalize_subscription(raw)


class ClashConfigTests(unittest.TestCase):
    def test_yaml_is_forced_onto_local_mixed_port(self) -> None:
        text = normalize_subscription(
            b"mixed-port: 7892\nallow-lan: true\nport: 7891\nproxies:\n  - name: a\n    port: 1\n"
        )
        self.assertIn("mixed-port: 7890\n", text)
        self.assertIn("allow-lan: false\n", text)
        self.assertIn("bind-address: 127.0.0.1\n", text)
        self.assertNotIn("port: 7891", text)
        self.assertIn("    port: 1", text)

    def test_base64_subscription_is_accepted(self) -> None:
        raw = base64.b64encode(b"proxies:\n  - name: a\n")
        text = normalize_subscription(raw)
        self.assertIn("mixed-port: 7890\n", text)
        self.assertIn("proxies:\n", text)

    def test_rejects_non_config(self) -> None:
        with self.assertRaises(ValueError):
            normalize_subscription(b"not a proxy")

    def test_tun_and_external_controller_stay_inside_the_container(self) -> None:
        text = normalize_subscription(
            b"proxies:\n  - name: a\nexternal-controller: 0.0.0.0:9090\n"
            b"tun:\n  enable: true\n  stack: system\nallow-lan: true\n"
        )
        self.assertNotIn("0.0.0.0:9090", text)
        self.assertNotIn("stack: system", text)
        self.assertIn("tun:\n  enable: false\n", text)
        self.assertIn("allow-lan: false\n", text)


class ClashEnvironmentTests(unittest.TestCase):
    def test_disabled_adds_no_container_arguments(self) -> None:
        settings = type("Settings", (), {"clash_enabled": False, "clash_subscription_url": ""})()
        with patch("echo_agent.config.DispatcherSettings.from_env", return_value=settings):
            self.assertEqual(clash_docker_arguments(), [])

    def test_enabled_points_container_at_local_mihomo(self) -> None:
        settings = type(
            "Settings",
            (),
            {"clash_enabled": True, "clash_subscription_url": "https://example.com/sub"},
        )()
        with patch("echo_agent.config.DispatcherSettings.from_env", return_value=settings):
            arguments = clash_docker_arguments()
        self.assertIn("ECHO_CLASH_ENABLED=1", arguments)
        self.assertIn("ECHO_CLASH_SUBSCRIPTION_URL=https://example.com/sub", arguments)
        self.assertIn("http_proxy=http://127.0.0.1:7890", arguments)
        self.assertTrue(
            clash_environment_changed(
                [],
                {
                    "ECHO_CLASH_ENABLED": "1",
                    "ECHO_CLASH_SUBSCRIPTION_URL": "https://example.com/sub",
                    "http_proxy": "http://127.0.0.1:7890",
                    "HTTP_PROXY": "http://127.0.0.1:7890",
                    "https_proxy": "http://127.0.0.1:7890",
                    "HTTPS_PROXY": "http://127.0.0.1:7890",
                    "all_proxy": "socks5h://127.0.0.1:7890",
                    "ALL_PROXY": "socks5h://127.0.0.1:7890",
                    "no_proxy": "localhost,127.0.0.1,::1",
                    "NO_PROXY": "localhost,127.0.0.1,::1",
                },
            )
        )
        self.assertFalse(
            clash_environment_changed(
                [
                    "ECHO_CLASH_ENABLED=1",
                    "ECHO_CLASH_SUBSCRIPTION_URL=https://example.com/sub",
                    "http_proxy=http://127.0.0.1:7890",
                    "HTTP_PROXY=http://127.0.0.1:7890",
                    "https_proxy=http://127.0.0.1:7890",
                    "HTTPS_PROXY=http://127.0.0.1:7890",
                    "all_proxy=socks5h://127.0.0.1:7890",
                    "ALL_PROXY=socks5h://127.0.0.1:7890",
                    "no_proxy=localhost,127.0.0.1,::1",
                    "NO_PROXY=localhost,127.0.0.1,::1",
                    "PATH=/usr/bin",
                ],
                {
                    "ECHO_CLASH_ENABLED": "1",
                    "ECHO_CLASH_SUBSCRIPTION_URL": "https://example.com/sub",
                    "http_proxy": "http://127.0.0.1:7890",
                    "HTTP_PROXY": "http://127.0.0.1:7890",
                    "https_proxy": "http://127.0.0.1:7890",
                    "HTTPS_PROXY": "http://127.0.0.1:7890",
                    "all_proxy": "socks5h://127.0.0.1:7890",
                    "ALL_PROXY": "socks5h://127.0.0.1:7890",
                    "no_proxy": "localhost,127.0.0.1,::1",
                    "NO_PROXY": "localhost,127.0.0.1,::1",
                },
            )
        )


class ClashEntrypointTests(unittest.TestCase):
    def test_subscription_download_does_not_use_the_local_proxy(self) -> None:
        text = Path("container/entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("ECHO_CLASH_ENABLED", text)
        self.assertIn("python3 /opt/echo-agent/clash_config.py", text)
        self.assertIn("mihomo -d", text)
        self.assertIn("env -u http_proxy", text)
        self.assertIn("127.0.0.1:7890", text)


if __name__ == "__main__":
    unittest.main()
