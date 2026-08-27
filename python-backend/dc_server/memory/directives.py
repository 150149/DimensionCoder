"""
memory/directives.py — DirectiveManager：硬规则 CRUD + tag scope 过滤

源自 Hindsight 的 directives/models.py

Directive 是用户显式定义的指令，注入 reflect system prompt 的 START 和 END。
与 mental models（自动归纳）不同，directives 始终注入。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .storage import MemoryStorage

logger = logging.getLogger(__name__)


class DirectiveManager:
    """Directive 管理器。

    使用方法：
        mgr = DirectiveManager(storage)
        did = mgr.upsert_directive(bank_id, "安全规则", "不要修改系统文件", priority=10)
        directives = mgr.get_directives_for_prompt(bank_id, tags=["security"])
    """

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
        """创建或更新 directive。"""
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
        """获取活跃的 directives，按 priority 降序。"""
        return self.storage.get_directives(bank_id, tags)

    def get_directives_for_prompt(
        self, bank_id: str, tags: Optional[list[str]] = None
    ) -> tuple[str, list[dict]]:
        """获取格式化的 directive 文本 + 引用列表，注入 reflect prompt。

        返回 (directive_text, applied_directives)
        directive_text 注入 system prompt 的 START 和 END
        applied_directives 用于 ReflectResult.directives_applied
        """
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
        """删除 directive（设置为 inactive）。"""
        conn = self.storage._get_conn()
        conn.execute(
            "UPDATE directives SET is_active = 0, updated_at = ? WHERE id = ?",
            (_now_iso(), directive_id),
        )
        conn.commit()

    def list_directives(self, bank_id: str, include_inactive: bool = False) -> list[dict]:
        """列出所有 directives。"""
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
