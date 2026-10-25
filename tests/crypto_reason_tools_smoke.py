"""Inspect Codex tool schemas against a loopback stub, without a model or network.

Run inside the pinned worker image with --network none and a writable /tmp.
The stub deliberately rejects requests after recording their advertised tools.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


requests: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        requests.append(json.loads(body))
        response = json.dumps({"error": {"message": "offline schema inspection finished", "type": "invalid_request_error"}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args) -> None:
        pass


def inspect_tools(port: int, *, restricted: bool) -> set[str]:
    requests.clear()
    with tempfile.TemporaryDirectory() as home:
        environment = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
        environment.update(CODEX_HOME=home, OPENAI_API_KEY="offline-placeholder")
        command = ["codex", "exec", "--skip-git-repo-check", "--model", "gpt-5.2-codex"]
        command.extend(["--sandbox", "read-only"] if restricted
                       else ["--dangerously-bypass-approvals-and-sandbox"])
        settings = [
            'model_provider="offline"', 'model_providers.offline.name="Offline schema inspection"',
            f'model_providers.offline.base_url="http://127.0.0.1:{port}/v1"',
            'model_providers.offline.env_key="OPENAI_API_KEY"',
            'model_providers.offline.wire_api="responses"',
            "model_providers.offline.request_max_retries=0",
            "model_providers.offline.stream_max_retries=0",
            "features.apps=false", "features.multi_agent=false", 'web_search="disabled"',
            f"features.shell_tool={str(not restricted).lower()}",
            f"features.unified_exec={str(not restricted).lower()}",
        ]
        for setting in settings:
            command.extend(["-c", setting])
        completed = subprocess.run(command + ["-"], input="Return a JSON plan without executing commands.",
                                   text=True, capture_output=True, env=environment, timeout=25)
        if not requests:
            raise RuntimeError(f"No loopback request captured: {completed.stderr[-1200:]}")
        return {tool.get("name", tool.get("type", "")) for tool in requests[0].get("tools", [])}


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        baseline = inspect_tools(server.server_port, restricted=False)
        restricted = inspect_tools(server.server_port, restricted=True)
        shell_tools = {"exec_command", "write_stdin", "shell", "shell_command"}
        assert baseline & shell_tools, f"Baseline did not expose shell tools: {baseline}"
        assert not restricted & shell_tools, f"Restricted reason exposed shell tools: {restricted}"
        assert "spawn_agent" not in restricted
        print(json.dumps({"status": "ok", "baseline_tools": sorted(baseline),
                          "restricted_reason_tools": sorted(restricted)}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
