
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .utils import (
    as_string_metadata,
    content_hash,
    drop_null_values,
    is_degenerate_text,
    sanitize_text,
)

logger = logging.getLogger(__name__)

MAX_TEMPORAL_LINKS_PER_UNIT = 20
MAX_SEMANTIC_LINKS_PER_UNIT = 20
MAX_CAUSAL_LINKS_PER_UNIT = 10

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _gen_id() -> str:
    return uuid4().hex

def build_text_signals(
    fact_text: str,
    metadata: dict,
    mentioned_at: Optional[str] = None,
) -> str:
    parts: list[str] = []

    for key in ("where", "who", "why", "entities"):
        val = metadata.get(key)
        if val and isinstance(val, str):
            parts.append(val)
        elif val and isinstance(val, list):
            parts.extend(str(v) for v in val)

    if mentioned_at:
        try:
            dt = datetime.fromisoformat(mentioned_at.replace("Z", "+00:00"))
            spelled = dt.strftime("%B %d %Y")
            spelled = spelled.replace(" 0", " ", 1) if spelled[9:11] == " 0" else spelled
            parts.append(spelled)
            parts.append(dt.strftime("%Y-%m-%d"))
        except (ValueError, TypeError):
            pass

    return " ".join(parts)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS memory_banks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        config TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_facts (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        fact_text TEXT NOT NULL,
        fact_type TEXT NOT NULL DEFAULT 'world',
        fact_kind TEXT DEFAULT 'conversation',
        context TEXT DEFAULT '',
        occurred_start TEXT,
        occurred_end TEXT,
        mentioned_at TEXT,
        metadata TEXT DEFAULT '{}',
        chunk_id TEXT,
        document_id TEXT,
        tags TEXT DEFAULT '[]',
        observation_scopes TEXT,
        embedding BLOB,
        content_hash TEXT,
        source_ref TEXT DEFAULT '{}',
        consolidated_at TEXT,
        consolidation_failed_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (bank_id) REFERENCES memory_banks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_entities (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        entity_kind TEXT DEFAULT 'regular',
        metadata TEXT DEFAULT '{}',
        first_seen TEXT,
        last_seen TEXT,
        mention_count INTEGER DEFAULT 0,
        FOREIGN KEY (bank_id) REFERENCES memory_banks(id) ON DELETE CASCADE,
        UNIQUE(bank_id, canonical_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_fact_entities (
        fact_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        FOREIGN KEY (fact_id) REFERENCES memory_facts(id) ON DELETE CASCADE,
        FOREIGN KEY (entity_id) REFERENCES memory_entities(id) ON DELETE CASCADE,
        PRIMARY KEY (fact_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_entity_cooccurrences (
        entity_id_1 TEXT NOT NULL,
        entity_id_2 TEXT NOT NULL,
        cooccurrence_count INTEGER DEFAULT 1,
        last_cooccurred TEXT,
        PRIMARY KEY (entity_id_1, entity_id_2)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_links (
        source_fact_id TEXT NOT NULL,
        target_fact_id TEXT NOT NULL,
        link_type TEXT NOT NULL,
        weight REAL DEFAULT 1.0,
        FOREIGN KEY (source_fact_id) REFERENCES memory_facts(id) ON DELETE CASCADE,
        FOREIGN KEY (target_fact_id) REFERENCES memory_facts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_observations (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        text TEXT NOT NULL,
        proof_count INTEGER DEFAULT 1,
        source_fact_ids TEXT DEFAULT '[]',
        evidence_quotes TEXT DEFAULT '[]',
        scope_tags TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}',
        stale INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        consolidated_at TEXT,
        FOREIGN KEY (bank_id) REFERENCES memory_banks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mental_models (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        name TEXT NOT NULL,
        source_query TEXT NOT NULL,
        content TEXT,
        tags TEXT DEFAULT '[]',
        max_tokens INTEGER DEFAULT 2048,
        trigger_config TEXT DEFAULT '{}',
        content_hash TEXT,
        last_refreshed_at TEXT,
        previous_versions TEXT DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (bank_id) REFERENCES memory_banks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_page_folders (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        parent_id TEXT,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        UNIQUE(bank_id, parent_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_pages (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        folder_id TEXT,
        page_name TEXT NOT NULL,
        mental_model_id TEXT NOT NULL,
        frontmatter TEXT DEFAULT '{}',
        last_refreshed_at TEXT,
        UNIQUE(bank_id, folder_id, page_name)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
        fact_text, context,
        content='memory_facts', content_rowid='rowid',
        tokenize='unicode61'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS directives (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        name TEXT NOT NULL,
        content TEXT NOT NULL,
        priority INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        tags TEXT DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (bank_id) REFERENCES memory_banks(id) ON DELETE CASCADE
    )
    """,
]

FTS_TRIGGER_SQL = [
    """
    CREATE TRIGGER IF NOT EXISTS memory_facts_ai AFTER INSERT ON memory_facts BEGIN
        INSERT INTO memory_facts_fts(rowid, fact_text, context)
        VALUES (new.rowid, new.fact_text, new.context);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_facts_ad AFTER DELETE ON memory_facts BEGIN
        INSERT INTO memory_facts_fts(memory_facts_fts, rowid, fact_text, context)
        VALUES ('delete', old.rowid, old.fact_text, old.context);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_facts_au AFTER UPDATE ON memory_facts BEGIN
        INSERT INTO memory_facts_fts(memory_facts_fts, rowid, fact_text, context)
        VALUES ('delete', old.rowid, old.fact_text, old.context);
        INSERT INTO memory_facts_fts(rowid, fact_text, context)
        VALUES (new.rowid, new.fact_text, new.context);
    END
    """,
]

class MemoryStorage:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        if self.db_path == ":memory:":
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for ddl in DDL_STATEMENTS:
            self._conn.execute(ddl)
        for trig in FTS_TRIGGER_SQL:
            self._conn.execute(trig)
        self._conn.commit()
        logger.info(f"MemoryStorage initialized: {self.db_path}")

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.exception("MemoryStorage close failed")
            self._conn = None
            logger.info("MemoryStorage closed")

    async def aclose(self):
        self.close()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        assert self._conn is not None
        return self._conn

    def get_or_create_bank(self, bank_id: str, name: str = "", config: dict | None = None) -> str:
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id, name, config FROM memory_banks WHERE id = ?", (bank_id,)
        ).fetchone()
        if existing:
            return bank_id
        bank_name = name if name and name != bank_id else bank_id
        conn.execute(
            "INSERT INTO memory_banks (id, name, config) VALUES (?, ?, ?)",
            (bank_id, bank_name, json.dumps(config or {}, ensure_ascii=False)),
        )
        conn.commit()
        return bank_id

    def get_or_create_bank_for_project(self, project_root: str) -> str:
        bank_id = hashlib.sha256(project_root.encode("utf-8")).hexdigest()[:16]
        return self.get_or_create_bank(bank_id, name=project_root)

    def insert_fact(
        self,
        bank_id: str,
        fact_text: str,
        fact_type: str = "world",
        fact_kind: str = "conversation",
        context: str = "",
        occurred_start: Optional[str] = None,
        occurred_end: Optional[str] = None,
        mentioned_at: Optional[str] = None,
        metadata: Optional[dict] = None,
        chunk_id: Optional[str] = None,
        document_id: Optional[str] = None,
        tags: Optional[list] = None,
        source_ref: Optional[dict] = None,
        embedding: Optional[bytes] = None,
        observation_scopes: Optional[list] = None,
        chunk_text_raw: Optional[str] = None,
    ) -> Optional[str]:
        fact_text = sanitize_text(fact_text)
        if is_degenerate_text(fact_text):
            return None

        fact_id = _gen_id()

        ts = build_text_signals(fact_text, metadata or {}, mentioned_at)
        fts_text = fact_text if not ts else f"{fact_text} {ts}"

        meta = drop_null_values(metadata or {})
        meta[f"src:{fact_id}"] = "1"

        ch = content_hash(chunk_text_raw) if chunk_text_raw else None

        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory_facts
                (id, bank_id, fact_text, fact_type, fact_kind, context,
                 occurred_start, occurred_end, mentioned_at, metadata,
                 chunk_id, document_id, tags, observation_scopes, embedding,
                 content_hash, source_ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                bank_id,
                fts_text,
                fact_type,
                fact_kind,
                sanitize_text(context) or "",
                occurred_start,
                occurred_end,
                mentioned_at,
                json.dumps(meta, ensure_ascii=False),
                chunk_id,
                document_id,
                json.dumps(tags or [], ensure_ascii=False),
                json.dumps(observation_scopes or [], ensure_ascii=False),
                embedding,
                ch,
                json.dumps(source_ref or {}, ensure_ascii=False),
                _now_iso(),
            ),
        )
        conn.commit()
        return fact_id

    def get_fact(self, fact_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM memory_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_unconsolidated_facts(
        self, bank_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM memory_facts
            WHERE bank_id = ? AND consolidated_at IS NULL
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
            """,
            (bank_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_fact_consolidated(self, fact_id: str, consolidated_at: Optional[str] = None):
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_facts SET consolidated_at = ? WHERE id = ?",
            (consolidated_at or _now_iso(), fact_id),
        )
        conn.commit()

    def mark_fact_consolidation_failed(self, fact_id: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_facts SET consolidation_failed_at = ? WHERE id = ?",
            (_now_iso(), fact_id),
        )
        conn.commit()

    def count_facts(self, bank_id: Optional[str] = None) -> int:
        conn = self._get_conn()
        if bank_id:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ?", (bank_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as c FROM memory_facts").fetchone()
        return row["c"] if row else 0

    def list_facts(self, bank_id: str, page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
        conn = self._get_conn()
        total = conn.execute(
            "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ?", (bank_id,)
        ).fetchone()["c"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM memory_facts WHERE bank_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (bank_id, page_size, offset),
        ).fetchall()
        return [dict(r) for r in rows], total

    def get_fact_embeddings(self, bank_id: str, limit: int = 1000) -> list[tuple[str, bytes]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, embedding FROM memory_facts WHERE bank_id = ? AND embedding IS NOT NULL LIMIT ?",
            (bank_id, limit),
        ).fetchall()
        return [(r["id"], r["embedding"]) for r in rows]

    def find_or_create_entity(
        self,
        bank_id: str,
        canonical_name: str,
        entity_kind: str = "regular",
    ) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id FROM memory_entities WHERE bank_id = ? AND LOWER(canonical_name) = LOWER(?)",
            (bank_id, canonical_name),
        ).fetchone()
        if row:
            return row["id"]
        entity_id = _gen_id()
        try:
            conn.execute(
                "INSERT INTO memory_entities (id, bank_id, canonical_name, entity_kind, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, bank_id, canonical_name, entity_kind, _now_iso(), _now_iso()),
            )
            conn.commit()
            return entity_id
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM memory_entities WHERE bank_id = ? AND LOWER(canonical_name) = LOWER(?)",
                (bank_id, canonical_name),
            ).fetchone()
            return row["id"] if row else None

    def get_all_entities(self, bank_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memory_entities WHERE bank_id = ?", (bank_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def link_fact_entity(self, fact_id: str, entity_id: str):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO memory_fact_entities (fact_id, entity_id) VALUES (?, ?)",
            (fact_id, entity_id),
        )
        conn.commit()

    def update_entity_stats(self, entity_id: str, mention_count_delta: int = 1):
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_entities SET mention_count = mention_count + ?, last_seen = ? WHERE id = ?",
            (mention_count_delta, _now_iso(), entity_id),
        )
        conn.commit()

    def upsert_cooccurrence(self, entity_id_1: str, entity_id_2: str):
        a, b = sorted([entity_id_1, entity_id_2])
        if a == b:
            return
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory_entity_cooccurrences (entity_id_1, entity_id_2, cooccurrence_count, last_cooccurred)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(entity_id_1, entity_id_2) DO UPDATE SET
                cooccurrence_count = cooccurrence_count + 1,
                last_cooccurred = MAX(last_cooccurred, excluded.last_cooccurred)
            """,
            (a, b, _now_iso()),
        )
        conn.commit()

    def get_cooccurrences(self, bank_id: str, entity_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT
                CASE WHEN entity_id_1 = ? THEN entity_id_2 ELSE entity_id_1 END as other_entity_id,
                cooccurrence_count,
                last_cooccurred
            FROM memory_entity_cooccurrences
            WHERE entity_id_1 = ? OR entity_id_2 = ?
            """,
            (entity_id, entity_id, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_entity_degree(self, bank_id: str, entity_id: str) -> int:
        return len(self.get_cooccurrences(bank_id, entity_id))

    def bulk_insert_links(
        self,
        links: list[tuple[str, str, str, float]],
    ):
        if not links:
            return
        conn = self._get_conn()
        sorted_links = sorted(links, key=lambda x: (x[0], x[1]))
        conn.executemany(
            "INSERT OR IGNORE INTO memory_links (source_fact_id, target_fact_id, link_type, weight) "
            "VALUES (?, ?, ?, ?)",
            sorted_links,
        )
        conn.commit()

    def insert_link(
        self,
        source_fact_id: str,
        target_fact_id: str,
        link_type: str,
        weight: float = 1.0,
    ):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO memory_links (source_fact_id, target_fact_id, link_type, weight) "
            "VALUES (?, ?, ?, ?)",
            (source_fact_id, target_fact_id, link_type, weight),
        )
        conn.commit()

    def count_links(self, fact_id: str, link_type: str, as_source: bool = True) -> int:
        conn = self._get_conn()
        col = "source_fact_id" if as_source else "target_fact_id"
        row = conn.execute(
            f"SELECT COUNT(*) as c FROM memory_links WHERE {col} = ? AND link_type = ?",
            (fact_id, link_type),
        ).fetchone()
        return row["c"] if row else 0

    def get_linked_facts(
        self, fact_id: str, link_type: Optional[str] = None, as_source: bool = True
    ) -> list[dict]:
        conn = self._get_conn()
        col = "source_fact_id" if as_source else "target_fact_id"
        target_col = "target_fact_id" if as_source else "source_fact_id"
        if link_type:
            rows = conn.execute(
                f"""
                SELECT f.*, l.weight, l.link_type
                FROM memory_links l
                JOIN memory_facts f ON f.id = l.{target_col}
                WHERE l.{col} = ? AND l.link_type = ?
                ORDER BY l.weight DESC
                """,
                (fact_id, link_type),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT f.*, l.weight, l.link_type
                FROM memory_links l
                JOIN memory_facts f ON f.id = l.{target_col}
                WHERE l.{col} = ?
                ORDER BY l.weight DESC
                """,
                (fact_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_observation(
        self,
        bank_id: str,
        text: str,
        source_fact_ids: Optional[list] = None,
        evidence_quotes: Optional[list] = None,
        scope_tags: Optional[list] = None,
    ) -> str:
        obs_id = _gen_id()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO memory_observations
                (id, bank_id, text, proof_count, source_fact_ids, evidence_quotes, scope_tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs_id,
                bank_id,
                sanitize_text(text) or "",
                len(source_fact_ids or []),
                json.dumps(source_fact_ids or [], ensure_ascii=False),
                json.dumps(evidence_quotes or [], ensure_ascii=False),
                json.dumps(scope_tags or [], ensure_ascii=False),
                _now_iso(),
                _now_iso(),
            ),
        )
        conn.commit()
        return obs_id

    def update_observation(
        self,
        obs_id: str,
        text: str,
        new_source_fact_ids: Optional[list] = None,
        new_evidence_quotes: Optional[list] = None,
    ):
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT source_fact_ids FROM memory_observations WHERE id = ?", (obs_id,)
        ).fetchone()
        if not existing:
            return
        old_sources = json.loads(existing["source_fact_ids"] or "[]")
        merged = list(set(old_sources + (new_source_fact_ids or [])))
        conn.execute(
            """
            UPDATE memory_observations
            SET text = ?, source_fact_ids = ?, proof_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                sanitize_text(text) or "",
                json.dumps(merged, ensure_ascii=False),
                len(merged),
                _now_iso(),
                obs_id,
            ),
        )
        conn.commit()

    def get_observation(self, obs_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM memory_observations WHERE id = ?", (obs_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_observations(self, bank_id: str, tags: Optional[list] = None) -> list[dict]:
        conn = self._get_conn()
        if tags:
            rows = conn.execute(
                "SELECT * FROM memory_observations WHERE bank_id = ? ORDER BY updated_at DESC",
                (bank_id,),
            ).fetchall()
            results = []
            for r in rows:
                scope = json.loads(r["scope_tags"] or "[]")
                if set(tags).issubset(set(scope)):
                    results.append(dict(r))
            return results
        rows = conn.execute(
            "SELECT * FROM memory_observations WHERE bank_id = ? ORDER BY updated_at DESC",
            (bank_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_observation_stale(self, obs_id: str, stale: bool = True):
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_observations SET stale = ? WHERE id = ?",
            (1 if stale else 0, obs_id),
        )
        conn.commit()

    def delete_observation(self, obs_id: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM memory_observations WHERE id = ?", (obs_id,))
        conn.commit()

    def upsert_mental_model(
        self,
        bank_id: str,
        name: str,
        source_query: str,
        content: Optional[str] = None,
        tags: Optional[list] = None,
        max_tokens: int = 2048,
        trigger_config: Optional[dict] = None,
        model_id: Optional[str] = None,
    ) -> str:
        conn = self._get_conn()
        mid = model_id or _gen_id()
        ch = content_hash(content) if content else None
        existing = conn.execute(
            "SELECT id FROM mental_models WHERE id = ?", (mid,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE mental_models
                SET name = ?, source_query = ?, content = ?, tags = ?, max_tokens = ?,
                    trigger_config = ?, content_hash = ?, last_refreshed_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    source_query,
                    content,
                    json.dumps(tags or [], ensure_ascii=False),
                    max_tokens,
                    json.dumps(trigger_config or {}, ensure_ascii=False),
                    ch,
                    _now_iso(),
                    mid,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO mental_models
                    (id, bank_id, name, source_query, content, tags, max_tokens,
                     trigger_config, content_hash, last_refreshed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    bank_id,
                    name,
                    source_query,
                    content,
                    json.dumps(tags or [], ensure_ascii=False),
                    max_tokens,
                    json.dumps(trigger_config or {}, ensure_ascii=False),
                    ch,
                    _now_iso(),
                    _now_iso(),
                ),
            )
        conn.commit()
        return mid

    def get_mental_model(self, model_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM mental_models WHERE id = ?", (model_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_mental_models_by_tags(self, bank_id: str, tags: Optional[list] = None) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM mental_models WHERE bank_id = ?", (bank_id,)
        ).fetchall()
        results = []
        for r in rows:
            model_tags = json.loads(r["tags"] or "[]")
            if not tags or set(tags).issubset(set(model_tags)):
                results.append(dict(r))
        return results

    def get_stale_models(self, bank_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM mental_models WHERE bank_id = ?", (bank_id,)
        ).fetchall()
        results = []
        for r in rows:
            if not r["last_refreshed_at"]:
                results.append(dict(r))
                continue
            tags = json.loads(r["tags"] or "[]")
            if tags:
                placeholders = ",".join("?" * len(tags))
                new_count = conn.execute(
                    f"SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND created_at > ? AND tags LIKE ?",
                    (bank_id, r["last_refreshed_at"], f'%{tags[0]}%'),
                ).fetchone()["c"]
            else:
                new_count = conn.execute(
                    "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND created_at > ?",
                    (bank_id, r["last_refreshed_at"]),
                ).fetchone()["c"]
            if new_count > 0:
                results.append(dict(r))
        return results

    def save_previous_version(self, model_id: str, version: dict):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT previous_versions FROM mental_models WHERE id = ?", (model_id,)
        ).fetchone()
        if not row:
            return
        versions = json.loads(row["previous_versions"] or "[]")
        versions.append(version)
        if len(versions) > 20:
            versions = versions[-20:]
        conn.execute(
            "UPDATE mental_models SET previous_versions = ? WHERE id = ?",
            (json.dumps(versions, ensure_ascii=False), model_id),
        )
        conn.commit()

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT f.*, bm25(memory_facts_fts) as bm25_score
            FROM memory_facts_fts
            JOIN memory_facts f ON f.rowid = memory_facts_fts.rowid
            WHERE memory_facts_fts MATCH ?
            ORDER BY bm25(memory_facts_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_directives(self, bank_id: str, tags: Optional[list] = None) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM directives WHERE bank_id = ? AND is_active = 1 ORDER BY priority DESC",
            (bank_id,),
        ).fetchall()
        results = []
        for r in rows:
            if tags:
                dtags = json.loads(r["tags"] or "[]")
                if not set(tags).intersection(set(dtags)):
                    continue
            results.append(dict(r))
        return results

    def upsert_directive(
        self,
        bank_id: str,
        name: str,
        content: str,
        priority: int = 0,
        tags: Optional[list] = None,
        is_active: bool = True,
        directive_id: Optional[str] = None,
    ) -> str:
        conn = self._get_conn()
        did = directive_id or _gen_id()
        existing = conn.execute(
            "SELECT id FROM directives WHERE id = ?", (did,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE directives SET name=?, content=?, priority=?, is_active=?, tags=?, updated_at=? WHERE id=?",
                (name, content, priority, 1 if is_active else 0,
                 json.dumps(tags or [], ensure_ascii=False), _now_iso(), did),
            )
        else:
            conn.execute(
                "INSERT INTO directives (id, bank_id, name, content, priority, is_active, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (did, bank_id, name, content, priority, 1 if is_active else 0,
                 json.dumps(tags or [], ensure_ascii=False), _now_iso(), _now_iso()),
            )
        conn.commit()
        return did

    def get_stats(self) -> dict:
        conn = self._get_conn()
        banks = conn.execute("SELECT COUNT(*) as c FROM memory_banks").fetchone()["c"]
        facts = conn.execute("SELECT COUNT(*) as c FROM memory_facts").fetchone()["c"]
        entities = conn.execute("SELECT COUNT(*) as c FROM memory_entities").fetchone()["c"]
        observations = conn.execute("SELECT COUNT(*) as c FROM memory_observations").fetchone()["c"]
        models = conn.execute("SELECT COUNT(*) as c FROM mental_models").fetchone()["c"]
        links = conn.execute("SELECT COUNT(*) as c FROM memory_links").fetchone()["c"]
        directives = conn.execute("SELECT COUNT(*) as c FROM directives").fetchone()["c"]
        return {
            "enabled": True,
            "banks": banks,
            "facts": facts,
            "entities": entities,
            "observations": observations,
            "mental_models": models,
            "links": links,
            "directives": directives,
        }

    def format_recall_for_prompt(self, recall_result: dict) -> str:
        parts: list[str] = []
        results = recall_result.get("results", [])

        observations = [r for r in results if r.get("observation_id")]
        facts = [r for r in results if not r.get("observation_id")]

        if observations:
            parts.append("### 观察总结")
            for obs in observations[:10]:
                proof = obs.get("proof_count", 1)
                text = obs.get("text", "")
                stale = " [待更新]" if obs.get("stale") else ""
                parts.append(f"- {text} (证据: {proof} 条){stale}")

        if facts:
            parts.append("### 相关事实")
            for f in facts[:20]:
                ftype = f.get("fact_type", "world")
                when = f.get("mentioned_at", "")
                text = f.get("fact_text", "")
                parts.append(f"- {text} (类型: {ftype}, 时间: {when})")

        return "\n".join(parts) if parts else ""

    def build_retain_text(
        self,
        conv: Optional[list[dict]],
        artifacts: list[dict],
        task: Optional[dict],
        step_id: str,
    ) -> str:
        parts: list[str] = []

        if task:
            parts.append(f"Task: {task.get('title', '')}")
            parts.append(f"Description: {task.get('description', '')}")
            parts.append(f"Type: {task.get('type', '')}")

        if conv:
            parts.append("\nConversation:")
            for msg in conv[-20:]:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if content:
                    parts.append(f"[{role}] {content}")

        if artifacts:
            parts.append("\nArtifacts:")
            for art in artifacts:
                atype = art.get("artifact_type", "")
                content = art.get("content", "")
                if content:
                    parts.append(f"[{atype}] {content[:2000]}")

        return "\n".join(parts)

    def get_consolidation_freshness(self, bank_id: str) -> dict:
        conn = self._get_conn()
        last_consolidated = conn.execute(
            "SELECT MAX(consolidated_at) as v FROM memory_facts WHERE bank_id = ?", (bank_id,)
        ).fetchone()["v"]
        last_write = conn.execute(
            "SELECT MAX(created_at) as v FROM memory_facts WHERE bank_id = ?", (bank_id,)
        ).fetchone()["v"]
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND consolidated_at IS NULL AND fact_type IN ('world', 'experience')",
            (bank_id,),
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND consolidation_failed_at IS NOT NULL",
            (bank_id,),
        ).fetchone()["c"]
        return {
            "last_consolidated_at": last_consolidated,
            "last_memory_write_at": last_write,
            "pending": pending,
            "failed": failed,
        }

    def get_last_memory_write_at(self, bank_id: str) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT MAX(created_at) as v FROM memory_facts WHERE bank_id = ?", (bank_id,)
        ).fetchone()
        return row["v"] if row else None

    def live_memory_ids(self, bank_id: str, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        conn = self._get_conn()
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id FROM memory_facts WHERE bank_id = ? AND id IN ({placeholders})",
            [bank_id] + ids,
        ).fetchall()
        return {r["id"] for r in rows}

    def find_observations_by_source_fact(self, bank_id: str, fact_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM memory_observations
            WHERE bank_id = ? AND source_fact_ids LIKE ?
            ORDER BY updated_at DESC
            """,
            (bank_id, f'%"{fact_id}"%'),
        ).fetchall()
        results = []
        for r in rows:
            source_ids = json.loads(r["source_fact_ids"] or "[]")
            if fact_id in source_ids:
                results.append(dict(r))
        return results

    def delete_fact(self, fact_id: str):
        from .graph_maintenance import run_graph_maintenance

        bank_id = None
        incoming_facts: list[str] = []
        entity_ids: list[str] = []

        conn = self._get_conn()
        row = conn.execute(
            "SELECT bank_id FROM memory_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if row:
            bank_id = row["bank_id"]

        if bank_id:
            rows = conn.execute(
                "SELECT source_fact_id FROM memory_links WHERE target_fact_id = ?",
                (fact_id,),
            ).fetchall()
            incoming_facts = [r["source_fact_id"] for r in rows]

            rows = conn.execute(
                "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?",
                (fact_id,),
            ).fetchall()
            entity_ids = [r["entity_id"] for r in rows]

        conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
        conn.commit()

        if bank_id:
            try:
                run_graph_maintenance(
                    storage=self,
                    bank_id=bank_id,
                    deleted_fact_ids=[fact_id],
                )
            except Exception as e:
                logger.warning(f"Graph maintenance failed for deleted fact {fact_id}: {e}")
