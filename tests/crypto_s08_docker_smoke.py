"""Real Docker smoke for S08. Creates and removes one isolated temporary container."""

import subprocess
import threading
import time
import unittest
import tempfile
from pathlib import Path
from uuid import uuid4

from echo_agent.dispatcher.agents import CodexDockerAgent, CryptoExecutionLimit
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.server.docker_runtime import DockerProjectRuntime
from tests.test_crypto_s07 import project, spec


IMAGE = "echo-agent-worker:crypto-s03"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=60, check=check)


class CryptoS08DockerSmoke(unittest.TestCase):
    def test_hard_limits_and_container_child_cleanup(self):
        item = project()
        name = f"echo-agent-s08-smoke-{uuid4().hex[:10]}"
        item.docker = name
        runtime = DockerProjectRuntime(image=IMAGE, context=Path("container"),
                                       dockerfile=Path("container/Dockerfile"))
        temporary = tempfile.TemporaryDirectory(prefix="s08-smoke-", dir=Path(".echo_agent"))
        artifacts = Path(temporary.name).resolve()
        assert artifacts.is_relative_to((Path.cwd() / ".echo_agent").resolve())
        command = runtime._docker_run_command(
            name, [(artifacts, "/home/kali/workspace/intents", False)], crypto=True
        )
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
            item.add_intent("silent bounded task", from_fact_id=2,
                            crypto=spec(budget={"wall_seconds": 4, "memory_mb": 512,
                                                "output_bytes": 4096}))
            script = (
                "import json,subprocess,sys,time\n"
                "from pathlib import Path\n"
                "sys.stdin.read()\n"
                "subprocess.Popen(['python3','-c','import time;time.sleep(120)',"
                "'s08-child-marker'],start_new_session=True)\n"
                "checkpoint=Path('/home/kali/workspace/intents/0/state.bin')\n"
                "checkpoint.parent.mkdir(parents=True,exist_ok=True)\n"
                "checkpoint.write_bytes(b'confirmed checkpoint')\n"
                "print(json.dumps({'answer':None,'fact_description':'confirmed prior observation',"
                "'outcome':'verified','crypto':{'exit_reason':'completed','covered_scope':{}}}),flush=True)\n"
                "time.sleep(120)\n"
            )
            agent = CodexDockerAgent(codex_command=("python3", "-c", script),
                                     timeout_seconds=30, no_output_timeout_seconds=1)
            result = agent.run(item, "run", DispatchMode.EXPLORE, intent_id=0)
            self.assertTrue(result.timed_out)
            self.assertIn("confirmed prior observation", result.output)
            self.assertEqual((artifacts / "0" / "state.bin").read_bytes(), b"confirmed checkpoint")
            self.assertEqual(docker("inspect", "-f", "{{.HostConfig.Memory}}", name).stdout.strip(),
                             str(512 * 1024 * 1024))
            self.assertNotIn("s08-child-marker", docker("top", name).stdout)
            self.assertEqual(docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip(),
                             "running")

            item.intents[0].crypto["budget"] = {"wall_seconds": 5, "memory_mb": 512,
                                                 "output_bytes": 4096}
            noisy = (
                "import subprocess,sys,time\n"
                "sys.stdin.read()\n"
                "subprocess.Popen(['python3','-c','import time;time.sleep(120)',"
                "'s08-noisy-child'],start_new_session=True)\n"
                "print('x'*10000,flush=True)\n"
                "time.sleep(120)\n"
            )
            noisy_agent = CodexDockerAgent(codex_command=("python3", "-c", noisy))
            with self.assertRaises(CryptoExecutionLimit) as raised:
                noisy_agent.run(item, "run", DispatchMode.EXPLORE, intent_id=0)
            self.assertEqual(raised.exception.reason, "budget_exhausted")
            self.assertNotIn("s08-noisy-child", docker("top", name).stdout)

            item.intents[0].crypto["budget"] = {"wall_seconds": 10, "memory_mb": 256,
                                                 "output_bytes": 4096}
            memory_hog = "import sys;sys.stdin.read();x=bytearray(400*1024*1024);print(len(x))"
            memory_agent = CodexDockerAgent(codex_command=("python3", "-c", memory_hog))
            with self.assertRaises(CryptoExecutionLimit) as raised:
                memory_agent.run(item, "run", DispatchMode.EXPLORE, intent_id=0)
            self.assertEqual(raised.exception.reason, "budget_exhausted")
            self.assertEqual(docker("inspect", "-f", "{{.HostConfig.Memory}}", name).stdout.strip(),
                             str(256 * 1024 * 1024))

            item.intents[0].crypto["budget"] = {"wall_seconds": 30, "memory_mb": 512,
                                                 "output_bytes": 4096}
            stopped = (
                "import subprocess,sys,time\n"
                "sys.stdin.read()\n"
                "subprocess.Popen(['python3','-c','import time;time.sleep(120)',"
                "'s08-stop-child'],start_new_session=True)\n"
                "time.sleep(120)\n"
            )
            stop_agent = CodexDockerAgent(codex_command=("python3", "-c", stopped))
            errors = []
            thread = threading.Thread(target=lambda: _run_and_collect(stop_agent, item, errors))
            thread.start()
            for _ in range(40):
                if "s08-stop-child" in docker("top", name).stdout:
                    break
                time.sleep(0.25)
            else:
                self.fail("controlled stop child never started")
            self.assertTrue(stop_agent.cancel_running())
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip(),
                             "exited")
        finally:
            docker("rm", "-f", name, check=False)
            temporary.cleanup()


def _run_and_collect(agent, item, errors):
    try:
        agent.run(item, "run", DispatchMode.EXPLORE, intent_id=0)
    except Exception as exc:
        errors.append(exc)


if __name__ == "__main__":
    unittest.main()
