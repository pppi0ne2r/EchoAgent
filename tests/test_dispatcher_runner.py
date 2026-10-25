from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from unittest.mock import patch

from echo_agent.dispatcher.types import DispatchMode, DispatchResult
from echo_agent.models import ProjectStatus
from echo_agent.server.dispatcher_runner import DispatcherRunner, DispatcherRunState, _RunHandle
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.storage import SQLiteProjectStore


class ForceStopRuntime(DockerProjectRuntime):
    def __init__(self) -> None:
        super().__init__(
            image="worker:test",
            context=Path("container"),
            dockerfile=Path("container/Dockerfile"),
        )
        object.__setattr__(self, "commands", [])

    def _container_state(self, container: str) -> str | None:
        return "running"

    def _run(self, command, log=None, output_prefix: str = "docker") -> None:
        self.commands.append(list(command))


class DispatcherRunnerStopTests(unittest.TestCase):
    def test_stop_releases_running_project_without_force_killing_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(project_id=7, target_url="http://target", objective="get flag")
            project.activate()
            project.start()
            store.save_project(project)
            runtime = ForceStopRuntime()
            runner = DispatcherRunner(store, runtime=runtime)

            state = runner.stop(project.id)

            saved = store.get_project(project.id)
            self.assertFalse(state.running)
            self.assertEqual(saved.status, ProjectStatus.STOPED)
            self.assertEqual(runtime.commands, [["docker", "stop", "echo-agent-task-7"]])
            self.assertNotIn("-t", runtime.commands[0])


    def test_stop_cancels_running_codex_processes_without_force_stopping_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(project_id=7, target_url="http://target", objective="get flag")
            project.activate()
            project.start()
            store.save_project(project)
            runtime = ForceStopRuntime()
            runner = DispatcherRunner(store, runtime=runtime)
            stop_event = threading.Event()
            started = threading.Event()

            def wait_for_stop() -> None:
                started.set()
                stop_event.wait(2)

            thread = threading.Thread(target=wait_for_stop)
            thread.start()
            started.wait(1)

            class CancellingDispatcher:
                def __init__(self) -> None:
                    self.stopped = False

                def request_stop(self) -> None:
                    self.stopped = True

            dispatcher = CancellingDispatcher()
            runner._runs[project.id] = _RunHandle(
                thread=thread,
                stop_event=stop_event,
                state=DispatcherRunState(project_id=project.id, running=True),
                dispatcher=dispatcher,  # type: ignore[arg-type]
            )

            state = runner.stop(project.id)
            thread.join(timeout=1)

            saved = store.get_project(project.id)
            self.assertTrue(dispatcher.stopped)
            self.assertTrue(state.stop_requested)
            self.assertFalse(state.running)
            self.assertEqual(saved.status, ProjectStatus.STOPED)
            self.assertEqual(runtime.commands, [["docker", "stop", "echo-agent-task-7"]])

    def test_stop_force_stops_container_when_thread_ignores_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(project_id=7, target_url="http://target", objective="get flag")
            project.activate()
            project.start()
            store.save_project(project)
            runtime = ForceStopRuntime()
            runner = DispatcherRunner(store, runtime=runtime)
            started = threading.Event()
            release = threading.Event()

            def ignore_stop() -> None:
                started.set()
                release.wait(5)

            thread = threading.Thread(target=ignore_stop, daemon=True)
            thread.start()
            started.wait(1)
            runner._runs[project.id] = _RunHandle(
                thread=thread,
                stop_event=threading.Event(),
                state=DispatcherRunState(project_id=project.id, running=True),
            )
            try:
                state = runner.stop(project.id, join_timeout=0.05)
                saved = store.get_project(project.id)
                self.assertEqual(saved.status, ProjectStatus.STOPED)
                self.assertIn(["docker", "stop", "-t", "0", "echo-agent-task-7"], runtime.commands)
                self.assertTrue(state.running)
            finally:
                release.set()
                thread.join(timeout=1)


class _ScriptedDispatcher:
    script: list[DispatchResult] = []
    calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run_once(self, project_id: int, release_project: bool = False) -> DispatchResult:
        type(self).calls += 1
        if type(self).script:
            return type(self).script.pop(0)
        return DispatchResult(project_id, DispatchMode.SKIPPED, ProjectStatus.RUNNING, "reason made no progress")

    def request_stop(self) -> None:
        return None

    def _release_project_if_running(self, project_id: int) -> None:
        return None


class DispatcherRunnerBudgetTests(unittest.TestCase):
    def _start(self, script: list[DispatchResult], max_steps: int) -> DispatcherRunState:
        _ScriptedDispatcher.script = list(script)
        _ScriptedDispatcher.calls = 0
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(project_id=9, target_url="http://target", objective="get flag")
            project.activate()
            store.save_project(project)
            runner = DispatcherRunner(store, runtime=ForceStopRuntime())
            with patch("echo_agent.server.dispatcher_runner.Dispatcher", _ScriptedDispatcher):
                state = runner.start(project.id, interval_seconds=0, max_steps=max_steps)
                thread = runner._runs[project.id].thread
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                return state

    def test_empty_reason_does_not_consume_runner_budget(self) -> None:
        state = self._start(
            [
                DispatchResult(9, DispatchMode.REASON, ProjectStatus.RUNNING, "planned 0 intents"),
                DispatchResult(9, DispatchMode.REASON, ProjectStatus.RUNNING, "planned 0 intents"),
            ],
            max_steps=1,
        )
        self.assertEqual(_ScriptedDispatcher.calls, 3)
        self.assertEqual(state.last_message, "reason made no progress")

    def test_successful_plan_runs_one_trailing_explore_at_budget(self) -> None:
        state = self._start(
            [
                DispatchResult(9, DispatchMode.EXPLORE, ProjectStatus.RUNNING, "executed 1 intents, released 0"),
                DispatchResult(9, DispatchMode.REASON, ProjectStatus.RUNNING, "planned 2 intents"),
                DispatchResult(9, DispatchMode.EXPLORE, ProjectStatus.RUNNING, "executed 2 intents, released 0"),
            ],
            max_steps=2,
        )
        self.assertEqual(_ScriptedDispatcher.calls, 3)
        self.assertEqual(state.last_message, "executed 2 intents, released 0")


class DispatcherRunnerWriteupTests(unittest.TestCase):
    def _run(self, script: list[DispatchResult], starter, max_steps: int = 3) -> DispatcherRunState:
        _ScriptedDispatcher.script = list(script)
        _ScriptedDispatcher.calls = 0
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(project_id=9, target_url="http://target", objective="get flag")
            project.activate()
            store.save_project(project)
            runner = DispatcherRunner(store, runtime=ForceStopRuntime(), writeup_starter=starter)
            with patch("echo_agent.server.dispatcher_runner.Dispatcher", _ScriptedDispatcher):
                state = runner.start(project.id, interval_seconds=0, max_steps=max_steps)
                thread = runner._runs[project.id].thread
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                return state

    def test_completed_result_starts_writeup_once(self) -> None:
        started: list[int] = []
        state = self._run(
            [DispatchResult(9, DispatchMode.COMPLETED, ProjectStatus.COMPLETED, "flag found")],
            started.append,
        )
        self.assertEqual(started, [9])
        self.assertEqual(state.last_message, "flag found")

    def test_skipped_result_does_not_start_writeup(self) -> None:
        started: list[int] = []
        self._run(
            [DispatchResult(9, DispatchMode.SKIPPED, ProjectStatus.RUNNING, "reason made no progress")],
            started.append,
        )
        self.assertEqual(started, [])

    def test_trailing_completed_explore_starts_writeup(self) -> None:
        started: list[int] = []
        self._run(
            [
                DispatchResult(9, DispatchMode.REASON, ProjectStatus.RUNNING, "planned 1 intents"),
                DispatchResult(9, DispatchMode.COMPLETED, ProjectStatus.COMPLETED, "flag found"),
            ],
            started.append,
            max_steps=1,
        )
        self.assertEqual(started, [9])

    def test_writeup_starter_failure_does_not_break_the_runner(self) -> None:
        def fail(project_id: int) -> None:
            raise RuntimeError(f"writeup failed for {project_id}")

        state = self._run(
            [DispatchResult(9, DispatchMode.COMPLETED, ProjectStatus.COMPLETED, "flag found")],
            fail,
        )
        self.assertEqual(state.last_message, "flag found")
        self.assertFalse(state.running)


if __name__ == "__main__":
    unittest.main()


class _GateDispatcher:
    seen: list[int] = []
    slots: list[int | None] = []
    ready = threading.Event()

    def __init__(self, *args, **kwargs) -> None:
        self.stop_event = kwargs.get("stop_event") or threading.Event()
        type(self).slots.append(kwargs.get("max_codex_slots"))

    def run_once(self, project_id: int, release_project: bool = False) -> DispatchResult:
        type(self).seen.append(project_id)
        type(self).ready.set()
        while not self.stop_event.is_set():
            self.stop_event.wait(0.05)
        return DispatchResult(project_id, DispatchMode.SKIPPED, ProjectStatus.ACTIVE, "stopped")

    def request_stop(self) -> None:
        self.stop_event.set()

    def _release_project_if_running(self, project_id: int) -> None:
        return None


class DispatcherRunnerQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        _GateDispatcher.seen = []
        _GateDispatcher.slots = []
        _GateDispatcher.ready = threading.Event()

    def test_projects_beyond_the_limit_stay_queued_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            for project_id in (1, 2, 3):
                store.create_project(project_id=project_id, target_url=f"http://target-{project_id}", objective="get flag")
            runner = DispatcherRunner(store, runtime=ForceStopRuntime())
            with (
                patch.object(DispatcherRunner, "_concurrency_limit", return_value=1),
                patch("echo_agent.server.dispatcher_runner.Dispatcher", _GateDispatcher),
            ):
                states = runner.start_many([1, 2, 3], interval_seconds=0.01)
                self.assertTrue(_GateDispatcher.ready.wait(2))
                self.assertEqual(_GateDispatcher.seen, [1])
                self.assertTrue(states[0].running)
                self.assertFalse(states[0].queued)
                self.assertTrue(states[1].queued)
                self.assertTrue(states[2].queued)
                self.assertEqual(store.get_project(1).status, ProjectStatus.ACTIVE)
                self.assertEqual(store.get_project(2).status, ProjectStatus.INACTIVE)
                self.assertEqual(store.get_project(3).status, ProjectStatus.INACTIVE)
                self.assertEqual(len(set(_GateDispatcher.slots)), 1)

                removed = runner.stop(2)
                self.assertFalse(removed.queued)
                self.assertEqual(store.get_project(2).status, ProjectStatus.INACTIVE)

                _GateDispatcher.ready.clear()
                runner.stop(1)
                self.assertTrue(_GateDispatcher.ready.wait(2))
                self.assertEqual(_GateDispatcher.seen, [1, 3])
                self.assertEqual(store.get_project(3).status, ProjectStatus.ACTIVE)
                self.assertTrue(runner.status(3).running)
                runner.stop_all()

    def test_each_running_project_keeps_its_own_codex_slot_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            for project_id in (1, 2):
                store.create_project(project_id=project_id, target_url=f"http://target-{project_id}", objective="get flag")
            runner = DispatcherRunner(store, runtime=ForceStopRuntime())
            with (
                patch.object(DispatcherRunner, "_concurrency_limit", return_value=2),
                patch("echo_agent.server.dispatcher_runner.Dispatcher", _GateDispatcher),
            ):
                runner.start_many([1, 2], interval_seconds=0.01)
                deadline = threading.Event()
                for _ in range(40):
                    if len(_GateDispatcher.seen) == 2:
                        deadline.set()
                        break
                    threading.Event().wait(0.05)
                self.assertTrue(deadline.is_set())
                self.assertEqual(_GateDispatcher.slots, [_GateDispatcher.slots[0], _GateDispatcher.slots[0]])
                self.assertGreaterEqual(_GateDispatcher.slots[0] or 0, 1)
                runner.stop_all()
