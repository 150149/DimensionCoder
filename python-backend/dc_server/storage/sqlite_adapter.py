
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from .adapter import StorageAdapter

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS epics (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description_md TEXT,
        status TEXT DEFAULT 'active',
        assignees TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        epic_id TEXT REFERENCES epics(id),
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'active',
        pause_level TEXT,
        assignee TEXT,
        best_effort INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'pending',
        required INTEGER DEFAULT 1,
        parallel_with TEXT,
        human_attention TEXT DEFAULT 'none',
        model_tier TEXT DEFAULT 'light',
        type TEXT DEFAULT 'executor',
        process_template TEXT,
        process_read_rules TEXT,
        sort_order INTEGER NOT NULL,
        token_prompt INTEGER DEFAULT 0,
        token_cached INTEGER DEFAULT 0,
        token_completion INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0,
        requests INTEGER DEFAULT 0,
        ttft_total_ms INTEGER DEFAULT 0,
        ttft_samples INTEGER DEFAULT 0,
        output_duration_ms INTEGER DEFAULT 0,
        run_duration_ms INTEGER DEFAULT 0,
        UNIQUE(task_id, step_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        content TEXT NOT NULL,
        content_format TEXT DEFAULT 'json',
        created_at TEXT NOT NULL,
        UNIQUE(task_id, step_id, artifact_type)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        step_id TEXT,
        actor TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS step_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        tool_call_id TEXT,
        tool_calls TEXT,
        tool_name TEXT,
        tool_input TEXT,
        tool_output TEXT,
        round_num INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS stream_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        chunk_type TEXT NOT NULL,
        content TEXT NOT NULL,
        call_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_epic ON tasks(epic_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type)",
    "CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_task_step ON artifacts(task_id, step_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_sm_task_step_seq ON step_messages(task_id, step_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_sc_task_step_seq ON stream_chunks(task_id, step_id, seq)",
]

class SQLiteAdapter(StorageAdapter):

    def __init__(self, db_path: str = "./dimensioncoding.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    async def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        assert self._conn is not None
        for stmt in DDL_STATEMENTS:
            self._conn.execute(stmt)
        for stmt in INDEX_STATEMENTS:
            self._conn.execute(stmt)
        self._migrate_step_messages()
        self._migrate_task_steps_tokens()
        self._migrate_tasks_best_effort()
        self._migrate_flow_reports()
        self._migrate_monitor_instances()
        self._migrate_monitor_entities()
        self._migrate_monitor_unbind()
        self._conn.commit()

    def _migrate_task_steps_tokens(self) -> None:
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(task_steps)").fetchall()}
        for col in ("token_prompt", "token_cached", "token_completion", "context_tokens",
                    "requests", "ttft_total_ms", "ttft_samples",
                    "output_duration_ms", "run_duration_ms"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE task_steps ADD COLUMN {col} INTEGER DEFAULT 0")
        if "description" not in cols:
            self._conn.execute("ALTER TABLE task_steps ADD COLUMN description TEXT")
        if "type" not in cols:
            self._conn.execute("ALTER TABLE task_steps ADD COLUMN type TEXT DEFAULT 'executor'")

    def _migrate_tasks_best_effort(self) -> None:
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "best_effort" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN best_effort INTEGER DEFAULT 0")

    def _migrate_flow_reports(self) -> None:
        import re
        assert self._conn is not None
        tasks = self._conn.execute(
            "SELECT DISTINCT task_id FROM artifacts WHERE artifact_type = 'step_report'"
        ).fetchall()
        for (task_id,) in tasks:
            has_flow = self._conn.execute(
                "SELECT 1 FROM artifacts WHERE task_id = ? AND step_id = '_flow' "
                "AND artifact_type = 'step_report'", (task_id,)
            ).fetchone()
            if has_flow:
                continue
            steps = self._conn.execute(
                "SELECT step_id FROM task_steps WHERE task_id = ? "
                "AND step_id NOT LIKE '\\_%' ESCAPE '\\' "
                "ORDER BY sort_order", (task_id,)
            ).fetchall()
            parts: list[str] = []
            for (sid,) in steps:
                row = self._conn.execute(
                    "SELECT content FROM artifacts WHERE task_id = ? AND step_id = ? "
                    "AND artifact_type = 'step_report'", (task_id, sid)
                ).fetchone()
                if not row:
                    continue
                text = re.sub(r"<!-- DC-KEY-FINDINGS-START -->.*?<!-- DC-KEY-FINDINGS-END -->",
                              "", row[0], flags=re.S).strip()
                if not text:
                    continue
                parts.append(f"## {sid} 报告节选\n\n{text}")
            if not parts:
                continue
            merged = f"# 流程报告（多步骤共享）\n\n" + "\n\n".join(parts)
            self._conn.execute(
                "INSERT INTO artifacts (task_id, step_id, artifact_type, content, "
                "content_format, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, "_flow", "step_report", merged, "markdown", self._now()),
            )

    def _migrate_monitor_instances(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            "UPDATE step_messages SET step_id = '_monitor:intervene' "
            "WHERE step_id = '_monitor_intervene'")
        self._conn.execute(
            "UPDATE step_messages SET step_id = '_monitor:init' "
            "WHERE step_id = '_monitor'")
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_monitor:intervene' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_intervene'")
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_monitor:init' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_plan'")
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_review' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_final'")
        rows = self._conn.execute(
            "SELECT task_id, step_id FROM artifacts "
            "WHERE artifact_type = 'monitor_conversation' "
            "AND step_id NOT LIKE '\\_%' ESCAPE '\\'"
        ).fetchall()
        for r in rows:
            self._conn.execute(
                "UPDATE OR IGNORE artifacts SET step_id = ? "
                "WHERE task_id = ? AND step_id = ? AND artifact_type = 'monitor_conversation'",
                (f"_monitor:{r['step_id']}", r["task_id"], r["step_id"]),
            )
        self._conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = '_monitor:init' "
            "WHERE step_id = '_monitor'")

    def _migrate_monitor_entities(self) -> None:
        assert self._conn is not None
        conn = self._conn
        renames = [
            ("_monitor:init", "monitor-init"),
            ("_monitor:intervene", "monitor-intervene"),
            ("_monitor", "monitor-init"),
            ("_monitor_intervene", "monitor-intervene"),
            ("_review", "review"),
            ("_report", "report"),
        ]
        for old, new in renames:
            for tbl in ("task_steps", "step_messages", "artifacts"):
                conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET step_id = ? WHERE step_id = ?",
                    (new, old))
        for tbl in ("task_steps", "step_messages", "artifacts"):
            conn.execute(
                f"UPDATE OR IGNORE {tbl} SET step_id = 'monitor-' || substr(step_id, 10) "
                f"WHERE step_id LIKE '\\_monitor:step-%' ESCAPE '\\'")
        conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = 'monitor-init' WHERE step_id = '_plan'")
        conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = 'review' WHERE step_id = '_final'")
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'monitor-init' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_plan'")
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'review' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_final'")
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'monitor-intervene' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_intervene'")

        conn.execute(
            "UPDATE task_steps SET type = 'monitor' WHERE step_id = 'monitor-init' "
            "OR step_id = 'monitor-intervene' OR step_id LIKE 'monitor-step-%'")
        conn.execute("UPDATE task_steps SET type = 'review' WHERE step_id = 'review'")
        conn.execute("UPDATE task_steps SET type = 'report' WHERE step_id = 'report'")

        tasks = conn.execute("SELECT DISTINCT task_id FROM task_steps").fetchall()
        for t in tasks:
            tid = t["task_id"]
            ids = {r["step_id"] for r in conn.execute(
                "SELECT step_id FROM task_steps WHERE task_id = ?", (tid,)).fetchall()}
            if "review" not in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_steps "
                    "(task_id, step_id, title, status, type, sort_order, required) "
                    "VALUES (?, 'review', '最终审查', 'pending', 'review', 999999, 1)",
                    (tid,))
            if "report" not in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_steps "
                    "(task_id, step_id, title, status, type, sort_order, required) "
                    "VALUES (?, 'report', '产出报告', 'pending', 'report', 999999, 1)",
                    (tid,))

        if conn.execute(
                "SELECT 1 FROM task_steps WHERE step_id LIKE 'monitor-step-%'"
        ).fetchone():
            for t in tasks:
                tid = t["task_id"]
                rows = conn.execute(
                    "SELECT step_id FROM task_steps WHERE task_id = ? ORDER BY sort_order",
                    (tid,)).fetchall()
                ids = [r["step_id"] for r in rows]
                is_special = lambda s: s.startswith("monitor-") or s in ("review", "report")  # noqa: E731
                real = [s for s in ids if not is_special(s)]
                monitors = {s for s in ids if s.startswith("monitor-step-")}
                new_order: list[str] = []
                if "monitor-init" in ids:
                    new_order.append("monitor-init")
                for s in real:
                    new_order.append(s)
                    msid = f"monitor-{s}"
                    if msid in monitors:
                        new_order.append(msid)
                        monitors.discard(msid)
                for s in ids:
                    if s in monitors:
                        new_order.append(s)
                if "monitor-intervene" in ids:
                    new_order.append("monitor-intervene")
                if "review" in ids:
                    new_order.append("review")
                if "report" in ids:
                    new_order.append("report")
                for i, sid in enumerate(new_order):
                    conn.execute(
                        "UPDATE task_steps SET sort_order = ? "
                        "WHERE task_id = ? AND step_id = ?",
                    ((i + 1) * 10, tid, sid))

        conn.execute("DELETE FROM task_steps WHERE step_id LIKE '\_%' ESCAPE '\\'")
        conn.execute("DELETE FROM step_messages WHERE step_id LIKE '\_%' ESCAPE '\\'")
        conn.execute(
            "DELETE FROM artifacts WHERE step_id LIKE '\_%' ESCAPE '\\' "
            "AND artifact_type = 'monitor_conversation'")
        conn.commit()

    def _migrate_step_messages(self) -> None:
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(step_messages)").fetchall()}
        if "tool_call_id" not in cols:
            self._conn.execute("ALTER TABLE step_messages ADD COLUMN tool_call_id TEXT")
        if "tool_calls" not in cols:
            self._conn.execute("ALTER TABLE step_messages ADD COLUMN tool_calls TEXT")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _json_dumps(self, obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    def _json_loads(self, text: Optional[str]) -> any:
        if text is None:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return dict(row)

    async def create_epic(self, epic: dict) -> str:
        conn = await self._get_conn()
        epic_id = epic.get("id", "")
        now = self._now()
        conn.execute(
            """INSERT INTO epics (id, title, description_md, status, assignees, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                epic_id,
                epic.get("title", ""),
                epic.get("description_md", ""),
                epic.get("status", "active"),
                self._json_dumps(epic.get("assignees", [])),
                epic.get("created_at", now),
                now,
            ),
        )
        conn.commit()
        return epic_id

    async def get_epic(self, epic_id: str) -> Optional[dict]:
        conn = await self._get_conn()
        row = conn.execute("SELECT * FROM epics WHERE id = ?", (epic_id,)).fetchone()
        if row is None:
            return None
        d = self._row_to_dict(row)
        d["assignees"] = self._json_loads(d.get("assignees", "[]"))
        return d

    async def list_epics(self) -> list[dict]:
        conn = await self._get_conn()
        rows = conn.execute(
            "SELECT * FROM epics ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = self._row_to_dict(r)
            d["assignees"] = self._json_loads(d.get("assignees", "[]"))
            result.append(d)
        return result

    async def _insert_steps(self, conn: sqlite3.Connection, task_id: str, steps: list[dict]) -> None:
        for i, step in enumerate(steps):
            conn.execute(
                """INSERT INTO task_steps
                   (task_id, step_id, title, description, status, required, parallel_with,
                    human_attention, model_tier, type, process_template, process_read_rules, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    step.get("step_id", step.get("id", f"step-{i+1}")),
                    step.get("title", ""),
                    step.get("description", ""),
                    step.get("status", "pending"),
                    1 if step.get("required", True) else 0,
                    self._json_dumps(step.get("parallel_with", [])),
                    step.get("human_attention", "none"),
                    step.get("model_tier", "light"),
                    step.get("type", "executor"),
                    step.get("process_template", ""),
                    self._json_dumps(step.get("process_read_rules", [])),
                    step.get("sort_order", i),
                ),
            )

    async def create_task(self, task: dict) -> str:
        conn = await self._get_conn()
        task_id = task.get("id", "")
        now = self._now()

        conn.execute(
            """INSERT INTO tasks (id, epic_id, type, title, description, status, pause_level, assignee, best_effort, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                task.get("epic_id"),
                task.get("type", ""),
                task.get("title", ""),
                task.get("description", ""),
                task.get("status", "active"),
                task.get("pause_level"),
                task.get("assignee", ""),
                1 if task.get("best_effort") else 0,
                task.get("created_at", now),
                now,
            ),
        )

        steps = task.get("steps", [])
        if steps:
            await self._insert_steps(conn, task_id, steps)

        conn.commit()
        return task_id

    async def delete_task(self, task_id: str) -> None:
        conn = await self._get_conn()
        conn.execute("DELETE FROM step_messages WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM stream_chunks WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

    async def _load_steps(self, conn: sqlite3.Connection, task_id: str,
                          include_hidden: bool = False) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY sort_order", (task_id,)
        ).fetchall()
        steps = []
        for r in rows:
            d = self._row_to_dict(r)
            sid = d.get("step_id", "")
            hidden = (d.get("type") in ("monitor", "review", "report")
                      or sid.startswith("_") or sid.startswith("monitor-")
                      or sid in ("review", "report"))
            if hidden and not include_hidden:
                continue
            d["required"] = bool(d.get("required", 1))
            d["parallel_with"] = self._json_loads(d.get("parallel_with", "[]"))
            d["process_read_rules"] = self._json_loads(d.get("process_read_rules", "[]"))
            steps.append(d)
        return steps

    async def get_task(self, task_id: str,
                       include_hidden: bool = False) -> Optional[dict]:
        conn = await self._get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        task = self._row_to_dict(row)
        task["steps"] = await self._load_steps(conn, task_id, include_hidden)
        return task

    async def update_task(self, task_id: str, updates: dict) -> None:
        conn = await self._get_conn()
        now = self._now()

        task_fields = ["title", "description", "status", "pause_level", "assignee",
                       "epic_id", "type", "best_effort"]
        set_clauses = []
        params = []
        for field in task_fields:
            if field in updates:
                set_clauses.append(f"{field} = ?")
                params.append(updates[field])

        if set_clauses:
            set_clauses.append("updated_at = ?")
            params.append(now)
            params.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?", params
            )

        if "steps" in updates:
            conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
            await self._insert_steps(conn, task_id, updates["steps"])

        conn.commit()

    async def list_tasks(
        self,
        epic_id: Optional[str] = None,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> list[dict]:
        conn = await self._get_conn()
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []

        if epic_id:
            query += " AND epic_id = ?"
            params.append(epic_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if task_type:
            query += " AND type = ?"
            params.append(task_type)

        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()

        tasks = []
        for r in rows:
            task = self._row_to_dict(r)
            task["steps"] = await self._load_steps(conn, task["id"])
            tasks.append(task)
        return tasks

    async def update_step_status(self, task_id: str, step_id: str, status: str) -> None:
        conn = await self._get_conn()
        conn.execute(
            "UPDATE task_steps SET status = ? WHERE task_id = ? AND step_id = ?",
            (status, task_id, step_id),
        )
        conn.commit()

    async def ensure_step(self, task_id: str, step_id: str, title: str = "",
                          step_type: str = "executor") -> None:
        conn = await self._get_conn()
        exists = conn.execute(
            "SELECT 1 FROM task_steps WHERE task_id = ? AND step_id = ?",
            (task_id, step_id)).fetchone()
        if exists:
            return
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM task_steps WHERE task_id = ?",
            (task_id,)).fetchone()
        conn.execute(
            "INSERT INTO task_steps (task_id, step_id, title, status, type, sort_order, required) "
            "VALUES (?, ?, ?, 'pending', ?, ?, 1)",
            (task_id, step_id, title or step_id, step_type, row[0]))
        conn.commit()

    async def add_step_tokens(self, task_id: str, step_id: str,
                              prompt: int = 0, cached: int = 0, completion: int = 0,
                              context_tokens: Optional[int] = None) -> None:
        conn = await self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM task_steps WHERE task_id = ?",
            (task_id,)).fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO task_steps
               (task_id, step_id, title, status, sort_order)
               VALUES (?, ?, ?, 'pending', ?)""",
            (task_id, step_id, step_id, row[0]),
        )
        conn.execute(
            """UPDATE task_steps SET
                 token_prompt = COALESCE(token_prompt, 0) + ?,
                 token_cached = COALESCE(token_cached, 0) + ?,
                 token_completion = COALESCE(token_completion, 0) + ?
               WHERE task_id = ? AND step_id = ?""",
            (prompt, cached, completion, task_id, step_id),
        )
        if context_tokens is not None:
            conn.execute(
                "UPDATE task_steps SET context_tokens = ? WHERE task_id = ? AND step_id = ?",
                (context_tokens, task_id, step_id),
            )
        conn.commit()

    async def update_step_stats(self, task_id: str, step_id: str, requests: int = 0,
                                ttft_ms: Optional[int] = None, output_ms: int = 0,
                                run_ms: Optional[int] = None) -> None:
        conn = await self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM task_steps WHERE task_id = ?",
            (task_id,)).fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO task_steps
               (task_id, step_id, title, status, sort_order)
               VALUES (?, ?, ?, 'pending', ?)""",
            (task_id, step_id, step_id, row[0]),
        )
        conn.execute(
            """UPDATE task_steps SET
                 requests = COALESCE(requests, 0) + ?,
                 output_duration_ms = COALESCE(output_duration_ms, 0) + ?,
                 run_duration_ms = ?,
                 ttft_total_ms = CASE WHEN ? > 0
                   THEN COALESCE(ttft_total_ms, 0) + ? ELSE ttft_total_ms END,
                 ttft_samples = CASE WHEN ? > 0
                   THEN COALESCE(ttft_samples, 0) + 1 ELSE ttft_samples END
               WHERE task_id = ? AND step_id = ?""",
            (requests, output_ms, run_ms or 0,
             ttft_ms or 0, ttft_ms or 0, ttft_ms or 0,
             task_id, step_id),
        )
        conn.commit()

    _VIRTUAL_ID_SQL = ("step_id LIKE 'monitor-%' OR step_id IN ('review','report') "
                       "OR step_id LIKE '\\_%' ESCAPE '\\'")

    async def list_virtual_step_ids(self, task_id: str) -> list[str]:
        conn = await self._get_conn()
        ids: set[str] = set()
        for tbl in ("task_steps", "step_messages", "artifacts"):
            rows = conn.execute(
                f"SELECT DISTINCT step_id FROM {tbl} WHERE task_id = ? "
                f"AND ({self._VIRTUAL_ID_SQL})",
                (task_id,),
            ).fetchall()
            ids.update(r["step_id"] for r in rows)
        return sorted(ids)

    async def list_virtual_steps(self, task_id: str) -> dict[str, str]:
        conn = await self._get_conn()
        rows = conn.execute(
            f"SELECT step_id, status FROM task_steps WHERE task_id = ? "
            f"AND (type IN ('monitor','review','report') OR ({self._VIRTUAL_ID_SQL}))",
            (task_id,),
        ).fetchall()
        return {r["step_id"]: r["status"] for r in rows}

    async def list_virtual_step_orders(self, task_id: str) -> dict[str, int]:
        conn = await self._get_conn()
        rows = conn.execute(
            f"SELECT step_id, sort_order FROM task_steps WHERE task_id = ? "
            f"AND (type IN ('monitor','review','report') OR ({self._VIRTUAL_ID_SQL}))",
            (task_id,),
        ).fetchall()
        return {r["step_id"]: r["sort_order"] for r in rows}

    async def list_virtual_step_tokens(self, task_id: str) -> dict[str, dict]:
        conn = await self._get_conn()
        rows = conn.execute(
            f"SELECT step_id, token_prompt, token_cached, token_completion "
            f"FROM task_steps WHERE task_id = ? "
            f"AND (type IN ('monitor','review','report') OR ({self._VIRTUAL_ID_SQL}))",
            (task_id,),
        ).fetchall()
        return {
            r["step_id"]: {
                "token_prompt": r["token_prompt"] or 0,
                "token_cached": r["token_cached"] or 0,
                "token_completion": r["token_completion"] or 0,
            }
            for r in rows
        }

    async def add_steps(self, task_id: str, steps: list[dict],
                        after_step_id: Optional[str] = None) -> None:
        conn = await self._get_conn()
        if after_step_id:
            row = conn.execute(
                "SELECT sort_order FROM task_steps WHERE task_id = ? AND step_id = ?",
                (task_id, after_step_id),
            ).fetchone()
            if row is not None:
                anchor = row[0]
                conn.execute(
                    "UPDATE task_steps SET sort_order = sort_order + ? "
                    "WHERE task_id = ? AND sort_order > ?",
                    (len(steps), task_id, anchor),
                )
                for i, step in enumerate(steps):
                    step["sort_order"] = anchor + 1 + i
                await self._insert_steps(conn, task_id, steps)
                conn.commit()
                return
        row = conn.execute(
            "SELECT MAX(sort_order) FROM task_steps WHERE task_id = ? "
            "AND step_id NOT IN ('review','report') "
            "AND COALESCE(type, '') NOT IN ('review','report')", (task_id,)
        ).fetchone()
        max_order = (row[0] if row[0] is not None else -1) + 1
        tail = conn.execute(
            "SELECT MIN(sort_order) FROM task_steps WHERE task_id = ? "
            "AND (step_id IN ('review','report') "
            "OR COALESCE(type, '') IN ('review','report'))", (task_id,)
        ).fetchone()
        if tail[0] is not None and max_order >= tail[0]:
            conn.execute(
                "UPDATE task_steps SET sort_order = sort_order + ? "
                "WHERE task_id = ? AND sort_order >= ?",
                (len(steps), task_id, tail[0]))

        for i, step in enumerate(steps):
            step["sort_order"] = max_order + i
        await self._insert_steps(conn, task_id, steps)
        conn.commit()

    def _migrate_monitor_unbind(self) -> None:
        assert self._conn is not None
        conn = self._conn
        for tbl in ("task_steps", "step_messages", "artifacts"):
            conn.execute(
                f"UPDATE OR IGNORE {tbl} SET step_id = 'monitor-intervene-1' "
                f"WHERE step_id = 'monitor-intervene'")
        tasks: set = set()
        for tbl in ("task_steps", "step_messages", "artifacts"):
            rows = conn.execute(
                f"SELECT DISTINCT task_id FROM {tbl} "
                f"WHERE step_id LIKE 'monitor-step-%'").fetchall()
            tasks.update(r["task_id"] for r in rows)
        for tid in sorted(tasks):
            seq = 0
            for r in conn.execute(
                    "SELECT step_id FROM task_steps WHERE task_id = ? "
                    "AND step_id GLOB 'monitor-[0-9]*'", (tid,)).fetchall():
                m = re.match(r"^monitor-(\d+)$", r["step_id"])
                if m:
                    seq = max(seq, int(m.group(1)))
            rows = conn.execute(
                "SELECT step_id, sort_order FROM task_steps WHERE task_id = ? "
                "AND step_id LIKE 'monitor-step-%'", (tid,)).fetchall()
            by_id = {r["step_id"]: r["sort_order"] for r in rows}
            orphan = conn.execute(
                "SELECT DISTINCT step_id FROM step_messages WHERE task_id = ? "
                "AND step_id LIKE 'monitor-step-%' UNION "
                "SELECT DISTINCT step_id FROM artifacts WHERE task_id = ? "
                "AND step_id LIKE 'monitor-step-%'", (tid, tid)).fetchall()
            ids = sorted(set(by_id) | {r["step_id"] for r in orphan},
                         key=lambda s: (by_id.get(s, 1 << 30), s))
            for old in ids:
                seq += 1
                new_id = f"monitor-{seq}"
                for tbl in ("task_steps", "step_messages", "artifacts"):
                    conn.execute(
                        f"UPDATE OR IGNORE {tbl} SET step_id = ? "
                        f"WHERE task_id = ? AND step_id = ?",
                        (new_id, tid, old))
        conn.execute(
            "UPDATE task_steps SET type = 'monitor' WHERE step_id GLOB 'monitor-*' "
            "AND type != 'monitor'")

    async def remove_steps(self, task_id: str, step_ids: list[str]) -> None:
        conn = await self._get_conn()
        placeholders = ",".join(["?"] * len(step_ids))
        conn.execute(
            f"DELETE FROM task_steps WHERE task_id = ? AND step_id IN ({placeholders})",
            [task_id] + step_ids,
        )
        conn.execute(
            f"DELETE FROM step_messages WHERE task_id = ? AND step_id IN ({placeholders})",
            [task_id] + step_ids,
        )
        conn.execute(
            f"DELETE FROM artifacts WHERE task_id = ? AND step_id IN ({placeholders})",
            [task_id] + step_ids,
        )
        conn.commit()

    async def reorder_steps(self, task_id: str, new_order: list[str]) -> None:
        conn = await self._get_conn()
        if not new_order:
            return
        ph = ",".join(["?"] * len(new_order))
        rows = conn.execute(
            f"SELECT step_id, sort_order FROM task_steps "
            f"WHERE task_id = ? AND step_id IN ({ph})", (task_id, *new_order)).fetchall()
        base = min((r["sort_order"] for r in rows), default=0)
        for i, step_id in enumerate(new_order):
            conn.execute(
                "UPDATE task_steps SET sort_order = ? WHERE task_id = ? AND step_id = ?",
                (base + i, task_id, step_id),
            )
        conn.commit()

    async def save_artifact(
        self, task_id: str, step_id: str, artifact_type: str,
        content: str, content_format: str = "json"
    ) -> None:
        conn = await self._get_conn()
        now = self._now()
        conn.execute(
            """INSERT OR REPLACE INTO artifacts
               (task_id, step_id, artifact_type, content, content_format, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, step_id, artifact_type, content, content_format, now),
        )
        conn.commit()

    async def get_artifact(self, task_id: str, step_id: str, artifact_type: str) -> Optional[dict]:
        conn = await self._get_conn()
        row = conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND step_id = ? AND artifact_type = ?",
            (task_id, step_id, artifact_type),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    async def list_artifacts(self, task_id: str, step_id: Optional[str] = None) -> list[dict]:
        conn = await self._get_conn()
        if step_id:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND step_id = ? ORDER BY created_at",
                (task_id, step_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def delete_artifacts(self, task_id: str, step_id: Optional[str] = None,
                               artifact_type: Optional[str] = None) -> None:
        conn = await self._get_conn()
        sql = "DELETE FROM artifacts WHERE task_id = ?"
        params: list = [task_id]
        if step_id:
            sql += " AND step_id = ?"
            params.append(step_id)
        if artifact_type:
            sql += " AND artifact_type = ?"
            params.append(artifact_type)
        conn.execute(sql, params)
        conn.commit()

    async def append_event(self, task_id: str, event: dict) -> None:
        conn = await self._get_conn()
        try:
            conn.execute(
                """INSERT INTO events (task_id, event_type, step_id, actor, content, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    event.get("event_type", "step_complete"),
                    event.get("step_id"),
                    event.get("actor", "ai"),
                    self._json_dumps(event.get("content", {})),
                    event.get("timestamp", self._now()),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            logger.warning(f"append_event: task_id={task_id!r} not found in tasks table, skipping event")
            conn.rollback()

    async def get_events(self, task_id: str, step_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        conn = await self._get_conn()
        if step_id:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND step_id = ? ORDER BY timestamp DESC LIMIT ?",
                (task_id, step_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        result = []
        for r in reversed(rows):
            d = self._row_to_dict(r)
            d["content"] = self._json_loads(d.get("content", "{}"))
            result.append(d)
        return result

    async def save_conversation(self, task_id: str, step_id: str, messages: list[dict]) -> None:
        for message in messages:
            await self.append_message(task_id, step_id, message)

    async def get_conversation(self, task_id: str, step_id: str) -> Optional[list[dict]]:
        msgs = await self.get_step_messages(task_id, step_id)
        return msgs if msgs else None

    async def append_message(self, task_id: str, step_id: str, message: dict, seq: Optional[int] = None) -> int:
        conn = await self._get_conn()
        now = self._now()
        if seq is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM step_messages WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
            next_seq = (row[0] if row[0] is not None else -1) + 1
        else:
            next_seq = seq
        conn.execute(
            """INSERT INTO step_messages (task_id, step_id, seq, role, content, tool_call_id, tool_calls, tool_name, tool_input, tool_output, round_num, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, step_id, next_seq,
                message.get("role", ""),
                message.get("content", "") or "",
                message.get("tool_call_id") or message.get("toolCallId"),
                message.get("tool_calls") or message.get("toolCalls"),
                message.get("toolName") or message.get("tool_name"),
                self._json_dumps(message.get("input")) if message.get("input") else None,
                message.get("output", "")[:10000] if message.get("output") else None,
                message.get("round_num", 0),
                now,
            ),
        )
        conn.commit()
        return next_seq

    async def get_step_messages(self, task_id: str, step_id: str, after_seq: int = -1,
                                limit: Optional[int] = None,
                                before_seq: int = -1) -> list[dict]:
        conn = await self._get_conn()
        if after_seq >= 0:
            rows = conn.execute(
                "SELECT * FROM step_messages WHERE task_id = ? AND step_id = ? AND seq > ? ORDER BY seq",
                (task_id, step_id, after_seq),
            ).fetchall()
        elif limit is not None and limit > 0:
            if before_seq >= 0:
                rows = conn.execute(
                    "SELECT * FROM step_messages WHERE task_id = ? AND step_id = ? "
                    "AND seq < ? ORDER BY seq DESC LIMIT ?",
                    (task_id, step_id, before_seq, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM step_messages WHERE task_id = ? AND step_id = ? "
                    "ORDER BY seq DESC LIMIT ?",
                    (task_id, step_id, limit),
                ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT * FROM step_messages WHERE task_id = ? AND step_id = ? ORDER BY seq",
                (task_id, step_id),
            ).fetchall()
        result = []
        for r in rows:
            d = self._row_to_dict(r)
            if d.get("tool_input"):
                d["input"] = self._json_loads(d["tool_input"])
            d["output"] = d.get("tool_output") or ""
            d["toolName"] = d.get("tool_name") or ""
            result.append(d)
        return result

    async def count_step_messages(self, task_id: str, step_id: str) -> int:
        conn = await self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM step_messages WHERE task_id = ? AND step_id = ?",
            (task_id, step_id),
        ).fetchone()
        return int(row[0]) if row else 0

    async def save_chunk(self, task_id: str, step_id: str, chunk: dict) -> int:
        conn = await self._get_conn()
        now = self._now()
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM stream_chunks WHERE task_id = ? AND step_id = ?",
            (task_id, step_id),
        ).fetchone()
        next_seq = (row[0] if row[0] is not None else -1) + 1
        try:
            conn.execute(
                """INSERT INTO stream_chunks (task_id, step_id, seq, chunk_type, content, call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, step_id, next_seq,
                    chunk.get("chunk_type", "text"),
                    chunk.get("content", ""),
                    chunk.get("call_id"),
                    now,
                ),
            )
            conn.commit()
            return next_seq
        except sqlite3.IntegrityError:
            logger.warning(f"save_chunk: task_id={task_id!r} not found in tasks table, skipping chunk")
            conn.rollback()
            return -1

    async def save_chunks(self, task_id: str, step_id: str, chunks: list[dict]) -> None:
        if not chunks:
            return
        conn = await self._get_conn()
        now = self._now()
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM stream_chunks WHERE task_id = ? AND step_id = ?",
            (task_id, step_id),
        ).fetchone()
        next_seq = (row[0] if row[0] is not None else -1) + 1
        try:
            for chunk in chunks:
                conn.execute(
                    """INSERT INTO stream_chunks (task_id, step_id, seq, chunk_type, content, call_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, step_id, next_seq,
                     chunk.get("chunk_type", "text"),
                     chunk.get("content", ""),
                     chunk.get("call_id"),
                     now),
                )
                next_seq += 1
            conn.commit()
        except sqlite3.IntegrityError:
            logger.warning(f"save_chunks: task_id={task_id!r} not found in tasks table, dropping {len(chunks)} chunks")
            conn.rollback()

    async def get_chunks(self, task_id: str, step_id: str, after_seq: int = -1) -> list[dict]:
        conn = await self._get_conn()
        if after_seq >= 0:
            rows = conn.execute(
                "SELECT * FROM stream_chunks WHERE task_id = ? AND step_id = ? AND seq > ? ORDER BY seq",
                (task_id, step_id, after_seq),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stream_chunks WHERE task_id = ? AND step_id = ? ORDER BY seq",
                (task_id, step_id),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def clear_step_messages(self, task_id: str, step_id: str) -> None:
        conn = await self._get_conn()
        conn.execute("DELETE FROM step_messages WHERE task_id = ? AND step_id = ?", (task_id, step_id))
        conn.execute("DELETE FROM stream_chunks WHERE task_id = ? AND step_id = ?", (task_id, step_id))
        conn.commit()

    async def export_for_ai(self, task_id: str, step_id: str, target_dir: str) -> dict:
        import os

        os.makedirs(target_dir, exist_ok=True)

        task = await self.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        task_context = {
            "task_id": task["id"],
            "type": task["type"],
            "title": task["title"],
            "description": task.get("description", ""),
            "status": task["status"],
            "current_step_id": step_id,
            "steps": task.get("steps", []),
        }
        task_context_path = os.path.join(target_dir, "task_context.json")
        with open(task_context_path, "w", encoding="utf-8") as f:
            json.dump(task_context, f, ensure_ascii=False, indent=2)

        artifacts_dir = os.path.join(target_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        artifacts = await self.list_artifacts(task_id)
        for a in artifacts:
            ext = "md" if a.get("content_format") == "markdown" else "json"
            filename = f"{a['step_id']}-{a['artifact_type']}.{ext}"
            with open(os.path.join(artifacts_dir, filename), "w", encoding="utf-8") as f:
                f.write(a["content"])

        conversations_dir = os.path.join(target_dir, "conversations")
        os.makedirs(conversations_dir, exist_ok=True)
        all_steps = task.get("steps", [])
        for s in all_steps:
            sid = s.get("step_id", "")
            if sid == step_id:
                continue
            msgs = await self.get_step_messages(task_id, sid)
            if msgs:
                filename = f"{sid}-conversation.json"
                with open(os.path.join(conversations_dir, filename), "w", encoding="utf-8") as f:
                    json.dump(msgs, f, ensure_ascii=False, indent=2)

        process_paths = []
        for a in artifacts:
            if a["artifact_type"] == "process" and a["step_id"] != step_id:
                ext = "md" if a.get("content_format") == "markdown" else "json"
                pp = os.path.join(artifacts_dir, f"{a['step_id']}-process.{ext}")
                process_paths.append(pp)

        return {
            "task_context_path": task_context_path,
            "artifacts_dir": artifacts_dir,
            "conversations_dir": conversations_dir,
            "process_paths": process_paths,
        }

    async def query_tasks(
        self, filters: dict, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        conn = await self._get_conn()

        query = "SELECT * FROM tasks WHERE 1=1"
        count_query = "SELECT COUNT(*) FROM tasks WHERE 1=1"
        params: list = []

        for field in ["status", "type"]:
            if field in filters and filters[field]:
                query += f" AND {field} = ?"
                count_query += f" AND {field} = ?"
                params.append(filters[field])

        if "epic_id" in filters and filters["epic_id"]:
            query += " AND epic_id = ?"
            count_query += " AND epic_id = ?"
            params.append(filters["epic_id"])

        if "keyword" in filters and filters["keyword"]:
            kw = f"%{filters['keyword']}%"
            query += " AND (title LIKE ? OR description LIKE ?)"
            count_query += " AND (title LIKE ? OR description LIKE ?)"
            params.extend([kw, kw])

        total = conn.execute(count_query, params).fetchone()[0]

        offset = (page - 1) * page_size
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([page_size, offset])

        rows = conn.execute(query, params).fetchall()
        tasks = []
        for r in rows:
            task = self._row_to_dict(r)
            task["steps"] = await self._load_steps(conn, task["id"])
            tasks.append(task)

        return tasks, total

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
