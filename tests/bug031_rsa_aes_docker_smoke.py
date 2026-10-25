"""Docker smoke for BUG-031: real RSA+AES verification inside the worker image.

Run from the repository root:
    C:/code/miniconda/envs/echoagent/python.exe -X utf8 tests/bug031_rsa_aes_docker_smoke.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".echo_agent" / "echo_agent.db"
IMAGE = "echo-agent-worker:crypto-20261022"
PROJECT_ID = 48
CANDIDATE_ID = 4


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("select crypto from facts where project_id=? and id=?",
                      (PROJECT_ID, CANDIDATE_ID)).fetchone()
    con.close()
    if row is None:
        print("candidate fact not found", file=sys.stderr)
        return 2
    candidate = json.loads(row[0])["candidate"]
    inputs = []
    for path in sorted((ROOT / ".echo_agent" / "projects" / str(PROJECT_ID) / "inputs").iterdir()):
        if path.is_file():
            inputs.append([path.name, path.read_text(encoding="utf-8")])

    payload = json.dumps({
        "inputs": inputs,
        "candidate": candidate,
        "goal": {"answer_kind": "plaintext", "solution_policy": "unique"},
    })
    code = (
        "import json,sys; "
        "from echo_agent.crypto_verify import _verify_rsa_aes; "
        "p=json.load(sys.stdin); "
        "print(json.dumps(_verify_rsa_aes("
        "[(i,t.encode('utf-8')) for i,t in p['inputs']],p['candidate'],p.get('goal'))))"
    )
    command = [
        "docker", "run", "--rm", "-i", "--pull=never", "--network", "none",
        "--read-only", "--memory", "256m", "--cpus", "1", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,source={ROOT / 'echo_agent'},target=/src/echo_agent,readonly",
        "--workdir", "/src", IMAGE, "python3", "-B", "-c", code,
    ]
    completed = subprocess.run(command, input=payload, text=True, capture_output=True, timeout=120)
    if completed.returncode != 0:
        print("docker exited", completed.returncode, file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        return 1
    status, checks = json.loads(completed.stdout)
    print(json.dumps({"status": status, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
