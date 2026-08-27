

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from .adapter import StorageAdapter


# ═══════════════════════════════════════════════════════════════════
# SQL DDL（与 plan.md 完全对齐）
# ═══════════════════════════════════════════════════════════════════

DDL_STATEMENTS = [
    # ── Epic 表 ──
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

    # ── Task 表 ──
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
        workspace_dir TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,

    # ── 步骤状态表 ──
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

    # ── 产物表 ──
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

    # ── 事件表 ──
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

    # ── 步骤消息表（追加式，每条消息一行）──
    # B1 方案②扩展：tool_call_id（tool 消息关联）、tool_calls（assistant 消息存 OpenAI tool_calls JSON 数组文本）
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

    # ── 流式 chunk 表（实时落盘）──
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


# ═══════════════════════════════════════════════════════════════════
# SQLiteAdapter 实现
# ═══════════════════════════════════════════════════════════════════


class SQLiteAdapter(StorageAdapter):
    """SQLite 存储适配器"""

    def __init__(self, db_path: str = "./dimensioncoding.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    async def _get_conn(self) -> sqlite3.Connection:
        """获取或创建数据库连接"""
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 锁冲突等待 30s（默认仅 5s）：外部进程（DB 查看工具/杀软扫描/文件
            # 同步）短暂持锁时不再立即报 "database is locked"——DB 实证 2026-08-15
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        """初始化数据库 Schema + 启动迁移（C2 幂等）"""
        assert self._conn is not None
        for stmt in DDL_STATEMENTS:
            self._conn.execute(stmt)
        for stmt in INDEX_STATEMENTS:
            self._conn.execute(stmt)
        self._migrate_step_messages()
        self._migrate_task_steps_tokens()
        self._migrate_tasks_best_effort()
        self._migrate_tasks_workspace_dir()
        self._migrate_flow_reports()
        self._migrate_monitor_instances()
        self._migrate_monitor_entities()
        self._migrate_monitor_unbind()
        self._conn.commit()

    def _migrate_task_steps_tokens(self) -> None:
        """
        Token 展示：task_steps 在旧 DDL 基础上新增 token_prompt/token_cached/
        token_completion（累计消耗）与 context_tokens（当前上下文长度，覆盖写，
        C2 幂等迁移）；另加 description 列（Monitor add_steps 可携带步骤说明/
        决策请求，此前被静默丢弃）。PRAGMA 探测列存在性，缺失才 ADD COLUMN
        （新建库的 CREATE TABLE 已含各列，探测后自然跳过）。
        """
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
        """尽力模式（Best-effort Mode，2026-08-16 用户需求）：tasks 表新增
        best_effort 列（开启后 gate 自动放行 + 防放弃提醒）。PRAGMA 探测
        列存在性，缺失才 ALTER TABLE ADD COLUMN（新建库 CREATE TABLE 已含）。"""
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "best_effort" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN best_effort INTEGER DEFAULT 0")

    def _migrate_tasks_workspace_dir(self) -> None:
        """自定义工作目录（2026-08-26 用户需求：创建流程可选工作目录）：tasks 表
        新增 workspace_dir 列（空 = 自动分配 workspace/<tid>/）。PRAGMA 探测列
        存在性，缺失才 ALTER TABLE ADD COLUMN（新建库 CREATE TABLE 已含）。"""
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "workspace_dir" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN workspace_dir TEXT")

    def _migrate_flow_reports(self) -> None:
        """流程级报告迁移（2026-08-16 用户需求）：把各任务步骤级 step_report artifacts
        （{step_id}-步骤报告.md 镜像）合并为任务级一份 (_flow, step_report)——多步骤
        共享同一份流程报告。幂等：已有 (_flow, step_report) 的任务跳过；合并按
        task_steps.sort_order 排序，去除系统注入块，标题分隔（正文整合留给 AI
        首次 nudge 读报告后自行处理，迁移只做无损拼接）；原步骤级 artifact 保留。"""
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
                    # 正文为空但原内容含关键发现注入块（如 gate 步骤纯注入报告）→
                    # 不进节选（2026-08-16 修复：发现列表由 _flow/key_findings 权威
                    # 持有，运行期注入区重建到报告末尾；进节选会让流程报告被碎片占满）
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
        """Monitor 多实例化迁移（2026-08-20）：旧单实例（裸 _monitor 9999、
        _monitor_intervene、_plan/_intervene/_final artifact key）数据迁移到新实例
        id（_monitor:init / _monitor:intervene / _monitor:step-<sid> / _review）。

        幂等：迁移后旧 id 不再存在，重复执行无操作；UPDATE OR IGNORE 防
        UNIQUE(task_id, step_id) 冲突（新任务已有实例行时跳过）。
        不迁移 events（历史审计保留旧 id）。
        """
        assert self._conn is not None
        # step_messages：介入轮与全局轮 → 实例
        self._conn.execute(
            "UPDATE step_messages SET step_id = '_monitor:intervene' "
            "WHERE step_id = '_monitor_intervene'")
        self._conn.execute(
            "UPDATE step_messages SET step_id = '_monitor:init' "
            "WHERE step_id = '_monitor'")
        # artifacts monitor_conversation：保留虚拟 key → 实例（_final→_review 已新格式不动）
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_monitor:intervene' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_intervene'")
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_monitor:init' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_plan'")
        self._conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = '_review' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_final'")
        # artifacts monitor_conversation：真实步骤 key（非 _ 前缀）→ 实例
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
        # task_steps：裸 _monitor 行 → _monitor:init（token 三列随行保留）
        self._conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = '_monitor:init' "
            "WHERE step_id = '_monitor'")

    def _migrate_monitor_entities(self) -> None:
        """Monitor 实体化迁移（2026-08-21）：虚拟步骤全部转正为真实步骤。

        - 三表（task_steps/step_messages/artifacts）改名：`_monitor:init`→`monitor-init`、
          `_monitor:intervene`→`monitor-intervene`、`_monitor:step-X`→`monitor-step-X`、
          `_review`→`review`、`_report`→`report`、`_plan`→`monitor-init`、`_final`→`review`
        - type 置位：monitor 类 → monitor、review → review、report → report
        - 缺 review/report 行的任务补插（幂等）
        - sort_order 重排：monitor-init 最前、monitor-step-X 紧随 step-X、
          monitor-intervene 在审查区前、review/report 末尾；全部 10 步长重编号

        幂等：迁移后旧 id 不存在，重复执行无操作；UPDATE OR IGNORE 防 UNIQUE
        (task_id, step_id) 冲突。不迁移 events（历史审计保留旧 id）。
        """
        assert self._conn is not None
        conn = self._conn
        # 1. 三表改名（固定映射）
        renames = [
            ("_monitor:init", "monitor-init"),
            ("_monitor:intervene", "monitor-intervene"),
            ("_monitor", "monitor-init"),            # 历史裸行（防御）
            ("_monitor_intervene", "monitor-intervene"),  # 更早历史（防御）
            ("_review", "review"),
            ("_report", "report"),
        ]
        for old, new in renames:
            for tbl in ("task_steps", "step_messages", "artifacts"):
                conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET step_id = ? WHERE step_id = ?",
                    (new, old))
        # 动态映射：_monitor:step-X → monitor-step-X（_monitor: 为 9 字符）
        for tbl in ("task_steps", "step_messages", "artifacts"):
            conn.execute(
                f"UPDATE OR IGNORE {tbl} SET step_id = 'monitor-' || substr(step_id, 10) "
                f"WHERE step_id LIKE '\\_monitor:step-%' ESCAPE '\\'")
        # task_steps 的 _plan/_final 行（如有）
        conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = 'monitor-init' WHERE step_id = '_plan'")
        conn.execute(
            "UPDATE OR IGNORE task_steps SET step_id = 'review' WHERE step_id = '_final'")
        # artifacts monitor_conversation：旧虚拟 key
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'monitor-init' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_plan'")
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'review' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_final'")
        conn.execute(
            "UPDATE OR IGNORE artifacts SET step_id = 'monitor-intervene' "
            "WHERE artifact_type = 'monitor_conversation' AND step_id = '_intervene'")

        # 2. type 置位（迁移前虚拟行 type 均为 executor）
        conn.execute(
            "UPDATE task_steps SET type = 'monitor' WHERE step_id = 'monitor-init' "
            "OR step_id = 'monitor-intervene' OR step_id LIKE 'monitor-step-%'")
        conn.execute("UPDATE task_steps SET type = 'review' WHERE step_id = 'review'")
        conn.execute("UPDATE task_steps SET type = 'report' WHERE step_id = 'report'")

        # 3. 缺 review/report 行补插（幂等；仅处理有步骤行的任务）
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

        # 4. sort_order 重排（每任务：真实步骤保序 + monitor-step-X 紧随其步骤）。
        #    仅当存在旧命名行时执行（迁移目标形态）——已去绑定（monitor-N）的
        #    任务跳过：新格式行按自身 sort_order 独立定位，重排会打乱其位置
        #    （DB 实证 2026-08-22：幂等重跑时 monitor-1 被丢出 new_order）
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
                    msid = f"monitor-{s}"  # monitor-step-1 = "monitor-" + "step-1"
                    if msid in monitors:
                        new_order.append(msid)
                        monitors.discard(msid)
                for s in ids:  # 无对应真实步骤的 monitor-step-*（孤儿）按原序
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

        # 5. 残留虚拟行清理（改名冲突防御）：同任务已有目标实体行时
        #    UPDATE OR IGNORE 会跳过改名，旧 `_` 行残留（DB 实证 2026-08-21
        #    c942812e：`_monitor_intervene` 与已有 monitor-intervene 行冲突）——
        #    实体化后 state_machine 不再排除 `_` 前缀，残留 pending 行会被执行
        #    循环误拾取 → 删除步骤行；消息/产物同步清理（`_flow` 为 task 级
        #    artifact 非步骤，保留）
        conn.execute("DELETE FROM task_steps WHERE step_id LIKE '\_%' ESCAPE '\\'")
        conn.execute("DELETE FROM step_messages WHERE step_id LIKE '\_%' ESCAPE '\\'")
        conn.execute(
            "DELETE FROM artifacts WHERE step_id LIKE '\_%' ESCAPE '\\' "
            "AND artifact_type = 'monitor_conversation'")
        conn.commit()

    def _migrate_step_messages(self) -> None:
        """
        B1 方案② + C2 幂等迁移：
        step_messages 在旧 DDL 基础上新增两列 tool_call_id/tool_calls。
        先 PRAGMA table_info(step_messages) 探测列存在性，缺失才 ALTER TABLE ADD COLUMN
        （存在则跳过，防止 duplicate column）；新建库的 CREATE TABLE 已含两列，探测后自然跳过。
        """
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
        """将 Row 转换为普通 dict"""
        return dict(row)

    # ── Epic ──────────────────────────────────────────────────────

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

    # ── Task ──────────────────────────────────────────────────────

    async def _insert_steps(self, conn: sqlite3.Connection, task_id: str, steps: list[dict]) -> None:
        """批量插入步骤"""
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
            """INSERT INTO tasks (id, epic_id, type, title, description, status, pause_level, assignee, best_effort, workspace_dir, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                task.get("workspace_dir"),
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
        """加载 Task 的步骤列表。
        默认过滤审查/收尾步骤（type=monitor/review/report 或 monitor-*/review/
        report id、历史 `_` 前缀）——它们不暴露给 UI/工具（否则 FlowOverview
        会出现伪步骤）；include_hidden=True 时全量返回（执行循环/状态机用）。
        """
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
        """获取任务。include_hidden=True（2026-08-21 实体化）：返回含审查/收尾
        步骤的全量步骤列表（执行循环/状态机拾取 monitor/review/report 用）。"""
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

        # 更新 task 主表字段
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

        # 如果传入了 steps，则替换整个步骤列表
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
        """确保步骤行存在（2026-08-20，Monitor 多实例用；2026-08-21 实体化：
        支持 type 落库）。INSERT OR IGNORE。行 sort_order = 任务当前最大 + 1
        （不固定 9999）、status=pending；幂等——已有行（含 add_step_tokens 占位）
        不覆盖。"""
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
        """累加步骤 token 用量（Token 展示）：SQL 原子加法（读旧值+增量写回）。
        resume 天然幂等——重放历史消息不会产生新的 usage 事件，不重复计费。
        context_tokens：最近一次请求的输入 tokens（当前上下文长度，覆盖写非累加；
        None 则不更新）。
        虚拟步骤不在任务模板中：首次累加时 INSERT OR IGNORE 占位行（title=step_id、
        status=pending、sort_order=尾部递增），仅作 token 容器——_load_steps 过滤
        `_` 开头，FlowOverview 不出现伪步骤。"""
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
        """步骤运行统计落库（2026-08-21 用户需求：统计与 token 明细同表同位置，
        步骤级别）：requests 累加（每轮 LLM 流 = 一次请求）；ttft_ms（本轮首字延迟）
        非 None 时累加进 ttft_total_ms 且 ttft_samples+1（平均在读取端计算，刷新
        后仍准）；output_ms 累加（纯 API 输出时长）；run_ms 覆盖（最近一轮结束的
        定格值，前端 active 时实时补差）。
        虚拟步骤不在任务模板中：首次 INSERT OR IGNORE 占位行（同 add_step_tokens）。"""
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
        """审查/收尾步骤 id 全量（2026-08-21 实体化）：task_steps + step_messages +
        artifacts 三表并集——历史数据可能只有消息/产物无 task_steps 行（如测试与
        旧版本），reset_flow_from_step 清理需覆盖。匹配实体 id（monitor-*/review/
        report）与历史 `_` 前缀（防御）。"""
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
        """审查/收尾步骤状态（2026-08-21 实体化）：返回 {step_id: status}——
        getTask 过滤审查步骤，MonitorDetail 据此判定 running/stop。"""
        conn = await self._get_conn()
        rows = conn.execute(
            f"SELECT step_id, status FROM task_steps WHERE task_id = ? "
            f"AND (type IN ('monitor','review','report') OR ({self._VIRTUAL_ID_SQL}))",
            (task_id,),
        ).fetchall()
        return {r["step_id"]: r["status"] for r in rows}

    async def list_virtual_step_orders(self, task_id: str) -> dict[str, int]:
        """审查/收尾步骤 sort_order（2026-08-21 实体化）：返回 {step_id: sort_order}——
        FlowOverview 多图标按区间归属依赖顺序（monitor_steps 只有状态无位置）。"""
        conn = await self._get_conn()
        rows = conn.execute(
            f"SELECT step_id, sort_order FROM task_steps WHERE task_id = ? "
            f"AND (type IN ('monitor','review','report') OR ({self._VIRTUAL_ID_SQL}))",
            (task_id,),
        ).fetchall()
        return {r["step_id"]: r["sort_order"] for r in rows}

    async def list_virtual_step_tokens(self, task_id: str) -> dict[str, dict]:
        """Token 展示：返回审查/收尾步骤行的 token 三列（MonitorDetail 数据源，
        映射 {step_id: {token_prompt, token_cached, token_completion}}）。"""
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
        """新增步骤。after_step_id（2026-08-20 新增）：插入到指定步骤之后
        （后续步骤 sort_order 后移）；默认追加到末尾（保持原行为）。"""
        conn = await self._get_conn()
        if after_step_id:
            row = conn.execute(
                "SELECT sort_order FROM task_steps WHERE task_id = ? AND step_id = ?",
                (task_id, after_step_id),
            ).fetchone()
            if row is not None:
                anchor = row[0]
                # 后续步骤 sort_order 后移，空出插入位
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
        # 默认：追加到末尾——但收尾链（review/report）已存在时，真实步骤永远
        # 排在收尾链前（DB 实证 1d5def81：步骤排到收尾链后，执行循环先跑
        # review/report → complete_task → 新步骤被丢弃）。插入点 = 最后一个
        # 非收尾步骤之后；若与收尾链并列/越过 → 收尾链整体后移腾位
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
        """Monitor 去绑定迁移（2026-08-21 data patch）：monitor 是独立编号普通步骤。

        - monitor-step-X → monitor-{seq}：按任务分组、按 sort_order 保序重编号，
          序号从任务内现有 monitor-N 最大序号 +1 起（防与新数据冲突）
        - monitor-intervene（无序号单例）→ monitor-intervene-1
        - 三表（task_steps/step_messages/artifacts）同步改名；type 置位防御

        幂等：迁移后旧 id 不存在，重复执行无操作；UPDATE OR IGNORE 防 UNIQUE
        (task_id, step_id) 冲突。不迁移 events（历史审计保留旧 id）。
        """
        assert self._conn is not None
        conn = self._conn
        # 1. monitor-intervene 单例 → monitor-intervene-1（无序号视为 1 号）
        for tbl in ("task_steps", "step_messages", "artifacts"):
            conn.execute(
                f"UPDATE OR IGNORE {tbl} SET step_id = 'monitor-intervene-1' "
                f"WHERE step_id = 'monitor-intervene'")
        # 2. monitor-step-X → monitor-{seq}（按任务、按 sort_order 保序）
        #    任务集合 = 三表并集（历史数据可能只有消息/产物无 task_steps 行）
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
            # 排序：task_steps 行按 sort_order；无行（仅消息/产物）按 step_id 字典序兜底
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
        # 3. type 置位（防御：新格式行 type 缺失/旧值补置）
        conn.execute(
            "UPDATE task_steps SET type = 'monitor' WHERE step_id GLOB 'monitor-*' "
            "AND type != 'monitor'")

    async def remove_steps(self, task_id: str, step_ids: list[str]) -> None:
        conn = await self._get_conn()
        placeholders = ",".join(["?"] * len(step_ids))
        # 2026-08-21 去绑定普通化：monitor 是独立步骤（monitor-N），不再与真实
        # 步骤命名绑定——删除真实步骤不再级联删 monitor 行（后续内容清理由
        # reset_flow_from_step 按 sort_order 统一删除后续 monitor 实例）
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
        # 2026-08-21：保持区间重排——以 order 内最小原 sort_order 为起点（此前从
        # 0 开始会把重排步骤全部顶到流程最前，打乱已完成步骤的相对位置；DB 实证
        # e726f3e6 07:31 reorder [step-15,16,14] 后三者变 0/1/2，step-11(20)
        # 被挤到后面，用户看到"顺序乱了"）
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

    # ── Artifacts ─────────────────────────────────────────────────

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
        """删除产物（重置流程用）：step_id 缺省删全任务；artifact_type 可再过滤。"""
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

    # ── Events ────────────────────────────────────────────────────

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
        for r in reversed(rows):  # 按时间正序返回
            d = self._row_to_dict(r)
            d["content"] = self._json_loads(d.get("content", "{}"))
            result.append(d)
        return result

    # ── Conversation（旧接口，保持兼容，委托给 step_messages） ──

    async def save_conversation(self, task_id: str, step_id: str, messages: list[dict]) -> None:
        """保存对话记录：逐条写入 step_messages（唯一消息源），get_conversation 从 step_messages 读回。"""
        for message in messages:
            await self.append_message(task_id, step_id, message)

    async def get_conversation(self, task_id: str, step_id: str) -> Optional[list[dict]]:
        """获取对话记录，仅从 step_messages 读取。"""
        msgs = await self.get_step_messages(task_id, step_id)
        return msgs if msgs else None

    # ── Step Messages（追加式，逐条写入） ──────────────────────────

    async def append_message(self, task_id: str, step_id: str, message: dict, seq: Optional[int] = None) -> int:
        """
        追加一条消息到 step_messages，返回 seq。

        B1 方案②扩展：
        - 写入新列 tool_call_id（tool 消息关联 id）、tool_calls（assistant 消息 OpenAI tool_calls JSON 数组文本）
        - 可选 seq 参数：缺省仍 MAX(seq)+1；显式传入时按传入值写入（compress seq 保留语义，问题 21 + 第 9 轮 B2）
        """
        conn = await self._get_conn()
        now = self._now()
        if seq is None:
            # 获取当前最大 seq
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
        """查询步骤消息（分页）：after_seq 增量（-1 全量）；limit 取最近 N 条
        （配合 before_seq 往前翻页：seq < before_seq 的最近 N 条，升序返回）。"""
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
            rows = list(reversed(rows))  # 升序返回（渲染顺序）
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
        """步骤消息总数（getTask 瘦身：总览页只需消息量，全量由 getStep 分页拉取）。"""
        conn = await self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM step_messages WHERE task_id = ? AND step_id = ?",
            (task_id, step_id),
        ).fetchone()
        return int(row[0]) if row else 0

    # ── Stream Chunks（实时落盘） ──────────────────────────────────

    async def save_chunk(self, task_id: str, step_id: str, chunk: dict) -> int:
        """保存一个流式 chunk，返回 seq。FK 失败时返回 -1。"""
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
        """批量保存流式 chunk（单事务）：流式输出逐条 INSERT+COMMIT 实测 1.79ms/条，
        1600 条 chunk 拖 2.8s（阻塞流式循环）；单事务批量 0.01ms/条。
        stream_chunks 仅审计用途（API.md 标注写不读、前端未消费）→ 批量无实时性损失。"""
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
        """增量查询流式 chunk。"""
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
        """清除某步骤的消息和 chunk（重新执行前调用）。"""
        conn = await self._get_conn()
        conn.execute("DELETE FROM step_messages WHERE task_id = ? AND step_id = ?", (task_id, step_id))
        conn.execute("DELETE FROM stream_chunks WHERE task_id = ? AND step_id = ?", (task_id, step_id))
        conn.commit()

    # ── 临时文件导出 ──────────────────────────────────────────────

    async def export_for_ai(self, task_id: str, step_id: str, target_dir: str) -> dict:
        """
        从 DB 导出该步骤 AI 所需的全部数据为文件。

        导出结构:
        {target_dir}/
        ├── task_context.json    # Task 基本信息 + 当前步骤定义
        ├── artifacts/            # 已完成步骤的产物
        └── conversations/        # 已完成步骤的对话记录
        """
        import os

        os.makedirs(target_dir, exist_ok=True)

        task = await self.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # 1. Task Context
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

        # 2. Artifacts
        artifacts_dir = os.path.join(target_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        artifacts = await self.list_artifacts(task_id)
        for a in artifacts:
            ext = "md" if a.get("content_format") == "markdown" else "json"
            filename = f"{a['step_id']}-{a['artifact_type']}.{ext}"
            with open(os.path.join(artifacts_dir, filename), "w", encoding="utf-8") as f:
                f.write(a["content"])

        # 3. Conversations（从 step_messages 读取，step_messages 是唯一数据源）
        conversations_dir = os.path.join(target_dir, "conversations")
        os.makedirs(conversations_dir, exist_ok=True)
        # 收集所有已完成步骤的对话
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

        # 4. Process paths (上一步骤的 process.md)
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

    # ── 高级查询 ──────────────────────────────────────────────────

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

        # Count
        total = conn.execute(count_query, params).fetchone()[0]

        # Paginate
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

    # ── 生命周期 ──────────────────────────────────────────────────

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
