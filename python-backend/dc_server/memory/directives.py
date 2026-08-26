
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .storage import MemoryStorage

logger = logging.getLogger(__name__)

class DirectiveManager:

    def __init__(self, storage: MemoryStorage):
        self.storage = storage

    def upsert_directive(
        self,
        bank_id: str,
        name: str,
        content: str,
        priority: int = 0,
        tags: Optional[list[str]] = None,
        is_active: bool = True,
        directive_id: Optional[str] = None,
    ) -> str:
        return self.storage.upsert_directive(
            bank_id=bank_id,
            name=name,
            content=content,
            priority=priority,
            tags=tags,
            is_active=is_active,
            directive_id=directive_id,
        )

    def get_directives(
        self, bank_id: str, tags: Optional[list[str]] = None
    ) -> list[dict]:
        return self.storage.get_directives(bank_id, tags)

    def get_directives_for_prompt(
        self, bank_id: str, tags: Optional[list[str]] = None
    ) -> tuple[str, list[dict]]:
        directives = self.get_directives(bank_id, tags)
        if not directives:
            return "", []

        parts = ["## Rules"]
        applied = []
        for d in directives:
            parts.append(f"- {d['content']}")
            applied.append({
                "id": d["id"],
                "name": d["name"],
                "content": d["content"],
            })

        directive_text = "\n".join(parts)
        return directive_text, applied

    def delete_directive(self, directive_id: str):
        conn = self.storage._get_conn()
        conn.execute(
            "UPDATE directives SET is_active = 0, updated_at = ? WHERE id = ?",
            (_now_iso(), directive_id),
        )
        conn.commit()

    def list_directives(self, bank_id: str, include_inactive: bool = False) -> list[dict]:
        conn = self.storage._get_conn()
        if include_inactive:
            rows = conn.execute(
                "SELECT * FROM directives WHERE bank_id = ? ORDER BY priority DESC",
                (bank_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM directives WHERE bank_id = ? AND is_active = 1 ORDER BY priority DESC",
                (bank_id,),
            ).fetchall()
        return [dict(r) for r in rows]

def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
