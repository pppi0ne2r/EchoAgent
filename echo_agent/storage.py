from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable
from typing import Any, Iterable, Iterator

from .models import Fact, FactKind, Intent, Project




def build_start_description(target_url: str | None, hint: str) -> str:
    items = [line.strip() for line in (target_url or "").splitlines() if line.strip()]
    parts = ["目标:", *(items or ["未设置"])]
    hint_text = hint.strip() if isinstance(hint, str) else ""
    parts.extend(["", "Hint:", hint_text or "无"])
    return "\n".join(parts)


def build_goal_description(objective: str) -> str:
    text = objective.strip() if isinstance(objective, str) else ""
    return text or "未填写目的"


def _sync_boundary_facts(project: Project) -> None:
    start_text = build_start_description(project.target_url, project.hint)
    goal_text = build_goal_description(project.objective)
    start = project.facts.get(0)
    if start is not None and start.kind == FactKind.START:
        start.description = start_text
    elif start is None:
        project.add_fact(start_text, fact_id=0, kind=FactKind.START)
    goal = project.facts.get(1)
    if goal is not None and goal.kind == FactKind.GOAL and not goal.description.startswith("result"):
        goal.description = goal_text
    elif goal is None:
        project.add_fact(goal_text, fact_id=1, kind=FactKind.GOAL)


def _merge_target_lines(existing: str | None, incoming: str) -> str:
    lines: list[str] = []
    for source in (existing, incoming):
        for line in (source or "").splitlines():
            item = line.strip()
            if item and item not in lines:
                lines.append(item)
    return "\n".join(lines)


class ProjectNotFoundError(KeyError):
    pass


class SQLiteProjectStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.after_save: Callable[[Project], None] | None = None
        self.after_log: Callable[[int, dict[str, Any]], None] | None = None
        self.after_clear_logs: Callable[[int], None] | None = None
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    docker TEXT,
                    target_url TEXT,
                    hint TEXT NOT NULL DEFAULT '',
                    objective TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS facts (
                    project_id INTEGER NOT NULL,
                    id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'evidence',
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS intents (
                    project_id INTEGER NOT NULL,
                    id INTEGER NOT NULL,
                    from_fact_id INTEGER,
                    to_fact_id INTEGER,
                    to_fact_ids TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    action_key TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY (project_id, from_fact_id) REFERENCES facts(project_id, id),
                    FOREIGN KEY (project_id, to_fact_id) REFERENCES facts(project_id, id)
                );

                CREATE TABLE IF NOT EXISTS project_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_project_columns(conn)
            self._ensure_graph_columns(conn)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM projects ORDER BY id").fetchall()
        return [self.get_project(int(row["id"])) for row in rows]

    def get_project(self, project_id: int) -> Project:
        with self._connect() as conn:
            project_row = conn.execute(
                "SELECT id, name, status, docker, target_url, hint, objective FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise ProjectNotFoundError(project_id)

            fact_rows = conn.execute(
                """
                SELECT id, description, kind
                FROM facts
                WHERE project_id = ?
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
            intent_rows = conn.execute(
                """
                SELECT id, from_fact_id, to_fact_id, to_fact_ids, description, status, outcome, note, action_key
                FROM intents
                WHERE project_id = ?
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()

        facts = {
            int(row["id"]): Fact(
                id=int(row["id"]),
                description=str(row["description"]),
                kind=str(row["kind"]),
            )
            for row in fact_rows
        }
        intents = {
            int(row["id"]): Intent(
                id=int(row["id"]),
                from_fact_id=_nullable_int(row["from_fact_id"]),
                to_fact_id=_nullable_int(row["to_fact_id"]),
                description=str(row["description"]),
                status=str(row["status"]),
                outcome=str(row["outcome"]),
                note=str(row["note"] or ""),
                action_key=str(row["action_key"] or ""),
                to_fact_ids=_target_ids(row["to_fact_ids"], _nullable_int(row["to_fact_id"])),
            )
            for row in intent_rows
        }
        return Project(
            id=int(project_row["id"]),
            name=str(project_row["name"]),
            status=str(project_row["status"]),
            facts=facts,
            intents=intents,
            docker=project_row["docker"],
            target_url=project_row["target_url"],
            hint=str(project_row["hint"]),
            objective=str(project_row["objective"]),
        )

    def save_project(self, project: Project) -> Project:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, status, docker, target_url, hint, objective)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    docker = excluded.docker,
                    target_url = excluded.target_url,
                    hint = excluded.hint,
                    objective = excluded.objective
                """,
                (
                    project.id,
                    project.name,
                    project.status.value,
                    project.docker,
                    project.target_url,
                    project.hint,
                    project.objective,
                ),
            )
            conn.execute("DELETE FROM intents WHERE project_id = ?", (project.id,))
            conn.execute("DELETE FROM facts WHERE project_id = ?", (project.id,))
            self._insert_facts(conn, project.id, project.facts.values())
            self._insert_intents(conn, project.id, project.intents.values())
        if self.after_save is not None:
            self.after_save(project)
        return project

    def create_project(
        self,
        name: str | None = None,
        project_id: int | None = None,
        docker: str | None = None,
        target_url: str | None = None,
        hint: str = "",
        objective: str = "",
    ) -> Project:
        name = name.strip() if isinstance(name, str) else None
        if not name:
            name = None
        if isinstance(target_url, str):
            target_url = target_url.strip() or None
        with self._connect() as conn:
            if project_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO projects (name, status, docker, target_url, hint, objective)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name or "pending", "inactive", docker, target_url, hint, objective),
                )
                project_id = int(cursor.lastrowid)
                generated_name = name or f"task-{project_id}"
                generated_docker = docker or f"echo-agent-task-{project_id}"
                conn.execute(
                    "UPDATE projects SET name = ?, docker = ? WHERE id = ?",
                    (generated_name, generated_docker, project_id),
                )
            else:
                project = Project(
                    id=project_id,
                    name=name or f"task-{project_id}",
                    docker=docker or f"echo-agent-task-{project_id}",
                    target_url=target_url,
                    hint=hint,
                    objective=objective,
                )
                conn.execute(
                    """
                    INSERT INTO projects (id, name, status, docker, target_url, hint, objective)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project.id,
                        project.name,
                        project.status.value,
                        project.docker,
                        project.target_url,
                        project.hint,
                        project.objective,
                    ),
                )
            self._insert_initial_facts(conn, project_id, target_url, hint, objective)
        return self.get_project(project_id)




    def refresh_boundary_facts(self) -> None:
        for project in self.list_projects():
            start = project.facts.get(0)
            goal = project.facts.get(1)
            boilerplate = (
                "目标可以是 URL、IP，或容器内附件路径，也可以同时存在。",
                "URL 或 IP 是需要访问的目标。附件已放在容器内对应路径，请直接读取该文件。",
                "附件已放在容器内的目标路径，请直接读取该文件。",
            )
            legacy_start = (
                start is not None
                and start.kind == FactKind.START
                and (start.description.strip() == "start" or any(line in start.description for line in boilerplate))
            )
            legacy_goal = (
                goal is not None
                and goal.kind == FactKind.GOAL
                and not goal.description.startswith("result")
                and ("target_url:" in goal.description or goal.description.startswith("goal\n") or "objective:" in goal.description)
            )
            if not legacy_start and not legacy_goal:
                continue
            _sync_boundary_facts(project)
            self.save_project(project)

    def update_project_details(
        self,
        project_id: int,
        *,
        name: str,
        target_url: str | None = None,
        hint: str = "",
        objective: str = "",
    ) -> Project:
        project = self.get_project(project_id)
        cleaned_name = name.strip() if isinstance(name, str) else ""
        if not cleaned_name:
            raise ValueError("项目名称不能为空")
        cleaned_objective = objective.strip() if isinstance(objective, str) else ""
        if not cleaned_objective:
            raise ValueError("目的不能为空")
        cleaned_target = target_url.strip() if isinstance(target_url, str) else ""
        project.name = cleaned_name
        project.target_url = cleaned_target or None
        project.hint = hint.strip() if isinstance(hint, str) else ""
        project.objective = cleaned_objective
        _sync_boundary_facts(project)
        return self.save_project(project)

    def set_project_target(self, project_id: int, target_url: str) -> Project:
        project = self.get_project(project_id)
        merged = _merge_target_lines(project.target_url, target_url)
        project.target_url = merged
        _sync_boundary_facts(project)
        return self.save_project(project)

    def delete_project(self, project_id: int) -> Project:
        project = self.get_project(project_id)
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            if cursor.rowcount == 0:
                raise ProjectNotFoundError(project_id)
        return project

    def add_project_log(
        self,
        project_id: int,
        message: str,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_project(project_id)
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO project_logs (project_id, level, message, metadata)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, level, message, payload),
            )
            log_id = int(cursor.lastrowid)
        record = self.get_project_log(project_id, log_id)
        if self.after_log is not None:
            self.after_log(project_id, record)
        return record

    def get_project_log(self, project_id: int, log_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, project_id, created_at, level, message, metadata
                FROM project_logs
                WHERE project_id = ? AND id = ?
                """,
                (project_id, log_id),
            ).fetchone()
        if row is None:
            raise KeyError(log_id)
        return _log_from_row(row)

    def list_project_logs(
        self,
        project_id: int,
        limit: int = 200,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self._connect() as conn:
            if after_id > 0:
                rows = conn.execute(
                    """
                    SELECT id, project_id, created_at, level, message, metadata
                    FROM project_logs
                    WHERE project_id = ? AND id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (project_id, after_id, limit),
                ).fetchall()
                return [_log_from_row(row) for row in rows]
            rows = conn.execute(
                """
                SELECT id, project_id, created_at, level, message, metadata
                FROM project_logs
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [_log_from_row(row) for row in reversed(rows)]

    def clear_project_logs(self, project_id: int) -> dict[str, Any]:
        self.get_project(project_id)
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM project_logs WHERE project_id = ?", (project_id,))
            deleted = int(cursor.rowcount)
        if self.after_clear_logs is not None:
            self.after_clear_logs(project_id)
        return {"project_id": project_id, "deleted": deleted}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_project_columns(conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(projects)").fetchall()
        existing = {str(row["name"]) for row in rows}
        columns = {
            "target_url": "ALTER TABLE projects ADD COLUMN target_url TEXT",
            "hint": "ALTER TABLE projects ADD COLUMN hint TEXT NOT NULL DEFAULT ''",
            "objective": "ALTER TABLE projects ADD COLUMN objective TEXT NOT NULL DEFAULT ''",
        }
        for name, statement in columns.items():
            if name not in existing:
                conn.execute(statement)

    @staticmethod
    def _ensure_graph_columns(conn: sqlite3.Connection) -> None:
        fact_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "kind" not in fact_columns:
            conn.execute("ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'evidence'")
            conn.execute("UPDATE facts SET kind = 'start' WHERE id = 0")
            conn.execute("UPDATE facts SET kind = 'goal' WHERE id = 1")
        intent_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
        if "outcome" not in intent_columns:
            conn.execute("ALTER TABLE intents ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'")
        if "note" not in intent_columns:
            conn.execute("ALTER TABLE intents ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "action_key" not in intent_columns:
            conn.execute("ALTER TABLE intents ADD COLUMN action_key TEXT NOT NULL DEFAULT ''")
        if "to_fact_ids" not in intent_columns:
            conn.execute("ALTER TABLE intents ADD COLUMN to_fact_ids TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def _insert_facts(conn: sqlite3.Connection, project_id: int, facts: Iterable[Fact]) -> None:
        conn.executemany(
            """
            INSERT INTO facts (project_id, id, description, kind)
            VALUES (?, ?, ?, ?)
            """,
            ((project_id, fact.id, fact.description, fact.kind.value) for fact in facts),
        )

    @staticmethod
    def _insert_initial_facts(
        conn: sqlite3.Connection,
        project_id: int,
        target_url: str | None,
        hint: str,
        objective: str,
    ) -> None:
        conn.executemany(
            """
            INSERT OR IGNORE INTO facts (project_id, id, description, kind)
            VALUES (?, ?, ?, ?)
            """,
            [
                (project_id, 0, build_start_description(target_url, hint), "start"),
                (project_id, 1, build_goal_description(objective), "goal"),
            ],
        )

    @staticmethod
    def _insert_intents(conn: sqlite3.Connection, project_id: int, intents: Iterable[Intent]) -> None:
        conn.executemany(
            """
            INSERT INTO intents (
                project_id, id, from_fact_id, to_fact_id, to_fact_ids, description, status, outcome, note, action_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    project_id,
                    intent.id,
                    intent.from_fact_id,
                    intent.to_fact_id,
                    json.dumps(list(intent.to_fact_ids)),
                    intent.description,
                    intent.status.value,
                    intent.outcome.value,
                    intent.note,
                    intent.action_key,
                )
                for intent in intents
            ),
        )


def _nullable_int(value: object) -> int | None:
    return None if value is None else int(value)


def _target_ids(value: object, primary: int | None) -> tuple[int, ...]:
    parsed: object = []
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
    if isinstance(parsed, list):
        ids: list[int] = []
        for item in parsed:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
                ids.append(int(item))
        if ids:
            return tuple(ids)
    return () if primary is None else (primary,)


def _log_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "created_at": str(row["created_at"]),
        "level": str(row["level"]),
        "message": str(row["message"]),
        "metadata": json.loads(str(row["metadata"])),
    }
