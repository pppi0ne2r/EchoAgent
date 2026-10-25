from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from echo_agent.server.dispatcher_runner import _step_finished_message
from echo_agent.storage import SQLiteProjectStore


class ProjectLogQueryTests(unittest.TestCase):
    def test_after_id_returns_only_newer_logs_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="log-project", objective="show progress")
            first = store.add_project_log(project.id, "one")
            second = store.add_project_log(project.id, "two")
            third = store.add_project_log(project.id, "three")

            self.assertEqual(
                [item["message"] for item in store.list_project_logs(project.id)],
                ["one", "two", "three"],
            )
            self.assertEqual(
                [item["message"] for item in store.list_project_logs(project.id, after_id=first["id"])],
                ["two", "three"],
            )
            self.assertEqual(
                [item["id"] for item in store.list_project_logs(project.id, limit=1, after_id=first["id"])],
                [second["id"]],
            )
            self.assertEqual(store.list_project_logs(project.id, after_id=third["id"]), [])

    def test_step_finished_message_is_visible_summary(self) -> None:
        self.assertEqual(_step_finished_message("explore", "executed 2 intents"), "探索完成：executed 2 intents")
        self.assertEqual(_step_finished_message("first_run", ""), "首次执行完成")
        self.assertEqual(_step_finished_message("custom", "done"), "custom完成：done")


if __name__ == "__main__":
    unittest.main()
