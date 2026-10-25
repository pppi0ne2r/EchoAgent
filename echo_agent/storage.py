from __future__ import annotations

import json
import sqlite3
import threading
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from .models import Fact, Intent, Project
from .crypto_contract import input_ref, project_crypto


class ProjectNotFoundError(KeyError):
    pass


class ProjectConflictError(RuntimeError):
    """A stale snapshot or another dispatcher owns this project."""


_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[tuple[str, int], Any] = {}


class SQLiteProjectStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
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
                    objective TEXT NOT NULL DEFAULT '',
                    task_type TEXT NOT NULL DEFAULT 'general',
                    crypto TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS facts (
                    project_id INTEGER NOT NULL,
                    id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'evidence',
                    crypto TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS intents (
                    project_id INTEGER NOT NULL,
                    id INTEGER NOT NULL,
                    from_fact_id INTEGER,
                    to_fact_id INTEGER,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    action_key TEXT NOT NULL DEFAULT '',
                    crypto TEXT NOT NULL DEFAULT '{}',
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
            self._ensure_crypto_columns(conn)
            self._ensure_project_identities(conn)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM projects ORDER BY id").fetchall()
        return [self.get_project(int(row["id"])) for row in rows]

    def get_project(self, project_id: int) -> Project:
        with self._connect() as conn:
            conn.execute("BEGIN")
            project_row = conn.execute(
                "SELECT id, name, status, docker, target_url, hint, objective, task_type, crypto, revision, run_owner, project_uid FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise ProjectNotFoundError(project_id)

            fact_rows = conn.execute(
                """
                SELECT id, description, kind, crypto
                FROM facts
                WHERE project_id = ?
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
            intent_rows = conn.execute(
                """
                SELECT id, from_fact_id, to_fact_id, description, status, outcome, note, action_key, crypto
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
                crypto=json.loads(row["crypto"] or "{}"),
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
                crypto=json.loads(row["crypto"] or "{}"),
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
            task_type=str(project_row["task_type"]),
            crypto=json.loads(project_row["crypto"] or "{}"),
            revision=int(project_row["revision"]),
            project_uid=project_row["project_uid"],
            run_owner=project_row["run_owner"],
        )

    @contextmanager
    def project_lock(self, project_id: int):
        key = (os.path.normcase(str(self.database_path.resolve())), project_id)
        with _LOCKS_GUARD:
            lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
        with lock:
            yield

    @staticmethod
    def _check_write(project: Project, row: Any, dispatcher_id: str | None, force_stop: bool) -> None:
        if not project.project_uid or project.project_uid != row["project_uid"]:
            raise ProjectConflictError("project identity conflict; reload the project before saving")
        if project.revision != row["revision"]:
            raise ProjectConflictError("project revision conflict; reload the project before saving")
        if row["status"] == "running" and row["run_owner"] and row["run_owner"] != dispatcher_id:
            if not (force_stop and project.status.value == "stoped"):
                raise ProjectConflictError("project is owned by another dispatcher; stop it before editing or running")

    def check_write(self, project: Project, *, dispatcher_id: str | None = None, force_stop: bool = False) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT revision, status, run_owner, project_uid FROM projects WHERE id = ?", (project.id,)).fetchone()
            if row is None:
                raise ProjectNotFoundError(project.id)
            self._check_write(project, row, dispatcher_id, force_stop)

    def save_project(self, project: Project, *, dispatcher_id: str | None = None, force_stop: bool = False) -> Project:
        Project.from_dict(project.to_dict())
        with self.project_lock(project.id), self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT revision, status, run_owner, project_uid FROM projects WHERE id = ?", (project.id,)).fetchone()
            if old is not None:
                self._check_write(project, old, dispatcher_id, force_stop)
            elif project.revision is not None or project.project_uid is not None:
                raise ProjectConflictError("project was deleted; a stale save cannot recreate it")
            if old is None:
                self._reserve_project_id(conn, project.id)
            project_uid = old["project_uid"] if old is not None else uuid4().hex
            revision = int(old["revision"]) + 1 if old is not None else 0
            run_owner = dispatcher_id if project.status.value == "running" else None
            if project.status.value == "running" and run_owner is None and old is not None:
                run_owner = old["run_owner"]
            conn.execute(
                """
                INSERT INTO projects (id, name, status, docker, target_url, hint, objective, task_type, crypto, revision, run_owner, project_uid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    docker = excluded.docker,
                    target_url = excluded.target_url,
                    hint = excluded.hint,
                    objective = excluded.objective,
                    task_type = excluded.task_type,
                    crypto = excluded.crypto,
                    revision = excluded.revision,
                    run_owner = excluded.run_owner
                """,
                (
                    project.id,
                    project.name,
                    project.status.value,
                    project.docker,
                    project.target_url,
                    project.hint,
                    project.objective,
                    project.task_type,
                    json.dumps(project.crypto, ensure_ascii=False),
                    revision, run_owner, project_uid,
                ),
            )
            conn.execute("DELETE FROM intents WHERE project_id = ?", (project.id,))
            conn.execute("DELETE FROM facts WHERE project_id = ?", (project.id,))
            self._insert_facts(conn, project.id, project.facts.values())
            self._insert_intents(conn, project.id, project.intents.values())
        project.revision = revision
        project.project_uid = project_uid
        project.run_owner = run_owner
        return project

    def create_project(
        self,
        name: str | None = None,
        project_id: int | None = None,
        docker: str | None = None,
        target_url: str | None = None,
        hint: str = "",
        objective: str = "",
        task_type: str = "general",
        crypto: dict[str, Any] | None = None,
    ) -> Project:
        # Validate before opening the insert transaction, including auto-ID calls.
        Project(id=project_id if project_id is not None else 0, name=name if name is not None else "pending",
                docker=docker, target_url=target_url, hint=hint, objective=objective,
                task_type=task_type, crypto=crypto or {})
        if task_type not in {"general", "crypto"}:
            raise ValueError("task_type must be general or crypto")
        crypto_data = project_crypto(crypto or {})
        if task_type == "general" and crypto_data:
            raise ValueError("general projects cannot have crypto metadata")
        if task_type == "crypto" and not crypto_data:
            crypto_data = {"schema_version": 1, "inputs": []}
        crypto_json = json.dumps(crypto_data, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_id = self._reserve_project_id(conn, project_id)
            project_uid = uuid4().hex
            if name is None:
                name = f"task-{project_id}"
            if docker is None:
                docker = f"echo-agent-task-{project_id}"
            conn.execute(
                """
                INSERT INTO projects (id, name, status, docker, target_url, hint, objective, task_type, crypto, project_uid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, name, "inactive", docker, target_url, hint, objective,
                 task_type, crypto_json, project_uid),
            )
            self._insert_initial_facts(conn, project_id, target_url, hint, objective)
        return self.get_project(project_id)

    def add_crypto_input(self, project_id: int, reference: dict[str, Any]) -> dict[str, Any]:
        """Register an already written input without rewriting the fact/intent graph."""
        validated = input_ref(reference)
        with self.project_lock(project_id), self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status, crypto FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(project_id)
            if row["task_type"] != "crypto":
                raise ValueError("inputs can only be uploaded to crypto projects")
            if row["status"] not in {"inactive", "active", "stoped"}:
                raise ValueError("inputs can only be added to an inactive, active or stopped crypto project")
            metadata = project_crypto(json.loads(row["crypto"] or "{}"))
            if any(item["id"] == validated["id"] for item in metadata["inputs"]):
                raise ValueError("crypto input id already exists")
            metadata["inputs"].append(validated)
            metadata = project_crypto(metadata)
            conn.execute(
                "UPDATE projects SET crypto = ?, revision = revision + 1 WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), project_id),
            )
        return validated

    def delete_project(self, project_id: int) -> Project:
        with self.project_lock(project_id):
            project = self.get_project(project_id)
            if project.status.value == "running":
                raise ProjectConflictError("stop the project before deleting it")
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
        return self.get_project_log(project_id, log_id)

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

    def list_project_logs(self, project_id: int, limit: int = 200) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self._connect() as conn:
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
        return {"project_id": project_id, "deleted": deleted}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_project_columns(conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(projects)").fetchall()
        existing = {str(row["name"]) for row in rows}
        columns = {
            "revision": "ALTER TABLE projects ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
            "run_owner": "ALTER TABLE projects ADD COLUMN run_owner TEXT",
            "project_uid": "ALTER TABLE projects ADD COLUMN project_uid TEXT",
            "target_url": "ALTER TABLE projects ADD COLUMN target_url TEXT",
            "hint": "ALTER TABLE projects ADD COLUMN hint TEXT NOT NULL DEFAULT ''",
            "objective": "ALTER TABLE projects ADD COLUMN objective TEXT NOT NULL DEFAULT ''",
        }
        for name, statement in columns.items():
            if name not in existing:
                conn.execute(statement)

    @staticmethod
    def _ensure_project_identities(conn: sqlite3.Connection) -> None:
        # Tombstones survive project deletion, so IDs and file directories are never reused.
        conn.execute("CREATE TABLE IF NOT EXISTS project_ids (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.execute("INSERT OR IGNORE INTO project_ids(id) SELECT id FROM projects")
        for row in conn.execute("SELECT id FROM projects WHERE project_uid IS NULL").fetchall():
            conn.execute("UPDATE projects SET project_uid = ? WHERE id = ?", (uuid4().hex, row["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS projects_uid ON projects(project_uid)")

    @staticmethod
    def _reserve_project_id(conn: sqlite3.Connection, project_id: int | None) -> int:
        if project_id is None:
            return int(conn.execute("INSERT INTO project_ids DEFAULT VALUES").lastrowid)
        if conn.execute("SELECT 1 FROM project_ids WHERE id = ?", (project_id,)).fetchone():
            raise ProjectConflictError("project id already used; choose a new id")
        conn.execute("INSERT INTO project_ids(id) VALUES (?)", (project_id,))
        return project_id

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

    @staticmethod
    def _ensure_crypto_columns(conn: sqlite3.Connection) -> None:
        for table, columns in {
            "projects": {"task_type": "TEXT NOT NULL DEFAULT 'general'", "crypto": "TEXT NOT NULL DEFAULT '{}'"},
            "facts": {"crypto": "TEXT NOT NULL DEFAULT '{}'"},
            "intents": {"crypto": "TEXT NOT NULL DEFAULT '{}'"},
        }.items():
            existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _insert_facts(conn: sqlite3.Connection, project_id: int, facts: Iterable[Fact]) -> None:
        conn.executemany(
            """
            INSERT INTO facts (project_id, id, description, kind, crypto)
            VALUES (?, ?, ?, ?, ?)
            """,
            ((project_id, fact.id, fact.description, fact.kind.value, json.dumps(fact.crypto, ensure_ascii=False)) for fact in facts),
        )

    @staticmethod
    def _insert_initial_facts(
        conn: sqlite3.Connection,
        project_id: int,
        target_url: str | None,
        hint: str,
        objective: str,
    ) -> None:
        goal_parts = [
            "goal",
            f"target_url: {target_url or ''}",
            f"hint: {hint}",
            f"objective: {objective}",
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO facts (project_id, id, description, kind)
            VALUES (?, ?, ?, ?)
            """,
            [
                (project_id, 0, "start", "start"),
                (project_id, 1, "\n".join(goal_parts), "goal"),
            ],
        )

    @staticmethod
    def _insert_intents(conn: sqlite3.Connection, project_id: int, intents: Iterable[Intent]) -> None:
        conn.executemany(
            """
            INSERT INTO intents (
                project_id, id, from_fact_id, to_fact_id, description, status, outcome, note, action_key, crypto
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    project_id,
                    intent.id,
                    intent.from_fact_id,
                    intent.to_fact_id,
                    intent.description,
                    intent.status.value,
                    intent.outcome.value,
                    intent.note,
                    intent.action_key,
                    json.dumps(intent.crypto, ensure_ascii=False),
                )
                for intent in intents
            ),
        )


def _nullable_int(value: object) -> int | None:
    return None if value is None else int(value)


def _log_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "created_at": str(row["created_at"]),
        "level": str(row["level"]),
        "message": str(row["message"]),
        "metadata": json.loads(str(row["metadata"])),
    }
