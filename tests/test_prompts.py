from __future__ import annotations

import unittest

from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.models import Project, ProjectStatus


class FirstRunPromptTests(unittest.TestCase):
    @staticmethod
    def _project_with_intent() -> tuple[Project, object]:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始探测", from_fact_id=0, to_fact_id=1)
        return project, intent

    def test_first_run_prompt_uses_one_json_for_flag(self) -> None:
        project, intent = self._project_with_intent()

        prompt = PromptBuilder().build_first_run_prompt(project, intent)  # type: ignore[arg-type]

        self.assertIn("本次执行只输出一条 JSON", prompt)
        self.assertIn("可以输出多条", prompt)
        self.assertIn("创建项目时填写的目的", prompt)
        self.assertNotIn("得到 flag", prompt)
        self.assertNotIn("同一次执行可以输出多个 JSON", prompt)
        self.assertNotIn("结束时再输出最后一个总结", prompt)
        self.assertIn('"fact_description"', prompt)

    def test_explore_prompt_still_single_result(self) -> None:
        project, intent = self._project_with_intent()

        prompt = PromptBuilder().build_explore_prompt(project, intent)  # type: ignore[arg-type]

        self.assertIn("只执行给定 intent", prompt)
        self.assertIn("当前 intent 已经满足目的", prompt)
        self.assertNotIn("直接得到 flag", prompt)
        self.assertIn('"fact_description"', prompt)


    def test_prompts_accept_ip_or_local_target(self) -> None:
        project, intent = self._project_with_intent()
        project.target_url = "10.0.0.8"
        project.hint = "备用账号 admin"
        project.objective = "拿到 flag"

        first_run = PromptBuilder().build_first_run_prompt(project, intent)  # type: ignore[arg-type]
        reason = PromptBuilder().build_reason_prompt(project)

        self.assertIn("10.0.0.8", first_run)
        self.assertIn("备用账号 admin", first_run)
        self.assertIn("拿到 flag", first_run)
        self.assertIn("不要假定它一定是网页地址", first_run)
        self.assertIn("不要假定它一定是网页地址", reason)
        self.assertIn("备用账号 admin", reason)
        self.assertIn("拿到 flag", reason)


class ReasonContextBudgetTests(unittest.TestCase):
    def test_older_evidence_is_truncated_but_ids_are_kept(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        total = 50
        for index in range(total):
            project.add_fact(f"fact-{index}-" + "x" * 300, fact_id=2 + index)

        prompt = PromptBuilder().build_reason_prompt(project)

        # 所有 evidence id 仍出现在 allowed_from 里
        for index in range(total):
            self.assertIn(str(2 + index), prompt)
        # 较早的事实被截断
        self.assertIn("较早事实已截断", prompt)
        # 最新的一条保持完整
        self.assertIn("fact-49-" + "x" * 300, prompt)

    def test_reason_prompt_explains_source_line(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)

        prompt = PromptBuilder().build_reason_prompt(project)

        self.assertIn("source:", prompt)
        self.assertIn("只能用来核验答案是否真实出现过", prompt)
        self.assertIn("to 必须为 null", prompt)
        self.assertNotIn("临时连接到目标终点", prompt)


if __name__ == "__main__":
    unittest.main()
