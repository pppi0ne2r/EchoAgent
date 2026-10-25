from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from echo_agent.server.attachments import save_attachment
from echo_agent.storage import SQLiteProjectStore


class ProjectCreateTargetTests(unittest.TestCase):
    def test_name_and_non_url_target_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(
                name="  pwn-heap  ",
                target_url=" 10.0.0.8 ",
                hint="附件在 /tmp/pwn",
                objective="拿到 shell",
            )

            self.assertEqual(project.name, "pwn-heap")
            self.assertEqual(project.target_url, "10.0.0.8")
            start = project.facts[0].description
            goal = project.facts[1].description
            self.assertIn("10.0.0.8", start)
            self.assertIn("附件在 /tmp/pwn", start)
            self.assertNotIn("10.0.0.8", goal)
            self.assertEqual(goal, "拿到 shell")

            attachment = store.create_project(
                name="rev-easy",
                target_url="/tmp/rev/challenge",
                objective="还原算法",
            )
            self.assertEqual(attachment.target_url, "/tmp/rev/challenge")

    def test_uploaded_attachment_becomes_container_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project = store.create_project(name="rev-easy", objective="还原算法")
            _host_path, container_path = save_attachment(
                root / "uploads",
                project.id,
                "../../题目附件.zip",
                b"PK\x03\x04",
            )
            updated = store.set_project_target(project.id, container_path)

            self.assertEqual(updated.target_url, "/home/kali/attachments/题目附件.zip")
            self.assertIn("/home/kali/attachments/题目附件.zip", updated.facts[0].description)
            self.assertNotIn("请直接读取该文件", updated.facts[0].description)
            self.assertNotIn("目标可以是 URL、IP", updated.facts[0].description)
            self.assertEqual(updated.facts[1].description, "还原算法")
            self.assertTrue((root / "uploads" / str(project.id) / "题目附件.zip").is_file())


    def test_url_and_attachment_are_kept_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project = store.create_project(
                name="web-and-file",
                target_url="https://target.example\n10.0.0.8:9999",
                hint="注意备份",
                objective="获取 flag",
            )
            _host_path, container_path = save_attachment(root / "uploads", project.id, "app.zip", b"zip")
            updated = store.set_project_target(project.id, container_path)

            self.assertEqual(
                updated.target_url,
                "https://target.example\n10.0.0.8:9999\n/home/kali/attachments/app.zip",
            )
            start = updated.facts[0].description
            self.assertIn("https://target.example", start)
            self.assertIn("10.0.0.8:9999", start)
            self.assertIn("/home/kali/attachments/app.zip", start)
            self.assertNotIn("请直接读取该文件", start)
            self.assertNotIn("目标可以是 URL、IP", start)
            self.assertIn("注意备份", start)
            self.assertEqual(updated.facts[1].description, "获取 flag")
            self.assertNotIn("https://target.example", updated.facts[1].description)



    def test_update_details_replaces_target_and_keeps_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(
                name="web-easy",
                target_url="https://old.example\n10.0.0.8",
                hint="旧提示",
                objective="旧目标",
            )
            project.add_fact("found cookie", kind="evidence")
            project.add_intent("try login", from_fact_id=0, to_fact_id=1)
            store.save_project(project)

            updated = store.update_project_details(
                project.id,
                name=" web-fixed ",
                target_url="https://new.example",
                hint="",
                objective="拿到 flag",
            )

            self.assertEqual(updated.name, "web-fixed")
            self.assertEqual(updated.target_url, "https://new.example")
            self.assertEqual(updated.hint, "")
            self.assertEqual(updated.objective, "拿到 flag")
            self.assertEqual(updated.status.value, "inactive")
            self.assertIn("https://new.example", updated.facts[0].description)
            self.assertNotIn("old.example", updated.facts[0].description)
            self.assertNotIn("10.0.0.8", updated.facts[0].description)
            self.assertIn("Hint:\n无", updated.facts[0].description)
            self.assertEqual(updated.facts[1].description, "拿到 flag")
            self.assertTrue(any(fact.description == "found cookie" for fact in updated.facts.values()))
            self.assertEqual(len(updated.intents), 1)



if __name__ == "__main__":
    unittest.main()
