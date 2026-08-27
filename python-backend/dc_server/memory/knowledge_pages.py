"""
memory/knowledge_pages.py — KnowledgePageManager：wiki 树 + markdown 投影

Knowledge Pages 是 mental model 的简化封装：
- 基于 observations 而非原始 fact
- 增量刷新
- 不读其他页面（避免反馈循环）
- 更大 content budget（文档而非答案）
- 可投影为 markdown 文件树
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .storage import MemoryStorage

logger = logging.getLogger(__name__)


class KnowledgePageManager:
    """Knowledge Page 管理器。

    使用方法：
        mgr = KnowledgePageManager(storage, mental_model_manager)
        page_id = await mgr.create_page(bank_id, "Architecture/反混淆", "去混淆模式", source_query="...")
        mgr.project_to_disk(bank_id, "/tmp/wiki")
    """

    def __init__(
        self,
        storage: MemoryStorage,
        mental_model_manager=None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.mental_model_manager = mental_model_manager
        self.config = config or {}

    async def create_page(
        self,
        bank_id: str,
        folder_path: str,
        page_name: str,
        source_query: str,
        tags: Optional[list[str]] = None,
    ) -> str:
        """创建知识页面。"""
        # 确保 folder 存在
        folder_id = self._ensure_folder(bank_id, folder_path)

        # 创建 mental model
        model_id = ""
        if self.mental_model_manager:
            model_id = await self.mental_model_manager.create_model(
                bank_id=bank_id,
                name=page_name,
                source_query=source_query,
                tags=tags,
                max_tokens=4096,  # 文档用更大预算
                trigger_config={
                    "refresh_after_consolidation": True,
                    "based_on_observations": True,
                    "dont_read_other_pages": True,
                },
            )

        # 创建 page 记录
        conn = self.storage._get_conn()
        page_id = uuid4().hex
        conn.execute(
            """
            INSERT INTO knowledge_pages (id, bank_id, folder_id, page_name, mental_model_id, frontmatter, last_refreshed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id, bank_id, folder_id, page_name, model_id,
                json.dumps({"title": page_name, "tags": tags or []}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return page_id

    def _ensure_folder(self, bank_id: str, folder_path: str) -> Optional[str]:
        """递归创建文件夹。返回 folder_id。"""
        if not folder_path or folder_path == "/":
            return None

        conn = self.storage._get_conn()
        parts = folder_path.strip("/").split("/")
        parent_id = None
        current_path = ""

        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            existing = conn.execute(
                "SELECT id FROM knowledge_page_folders WHERE bank_id = ? AND parent_id IS ? AND name = ?",
                (bank_id, parent_id, part),
            ).fetchone()

            if existing:
                parent_id = existing["id"]
            else:
                folder_id = uuid4().hex
                conn.execute(
                    "INSERT INTO knowledge_page_folders (id, bank_id, parent_id, name, path) VALUES (?, ?, ?, ?, ?)",
                    (folder_id, bank_id, parent_id, part, current_path),
                )
                conn.commit()
                parent_id = folder_id

        return parent_id

    def list_pages(self, bank_id: str, folder_path: Optional[str] = None) -> list[dict]:
        """列出页面。"""
        conn = self.storage._get_conn()
        if folder_path:
            folder_id = self._ensure_folder(bank_id, folder_path)
            rows = conn.execute(
                "SELECT * FROM knowledge_pages WHERE bank_id = ? AND folder_id IS ?",
                (bank_id, folder_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM knowledge_pages WHERE bank_id = ?", (bank_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def search_pages(self, bank_id: str, query: str) -> list[dict]:
        """搜索知识页面（全文 + 语义融合）。"""
        conn = self.storage._get_conn()
        # 简化版：通过 mental model 内容搜索
        rows = conn.execute(
            """
            SELECT kp.*, mm.content, mm.name as model_name
            FROM knowledge_pages kp
            LEFT JOIN mental_models mm ON mm.id = kp.mental_model_id
            WHERE kp.bank_id = ? AND mm.content LIKE ?
            """,
            (bank_id, f"%{query}%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def project_to_disk(self, bank_id: str, target_dir: str):
        """将整个 wiki 树投影为 markdown 文件。"""
        conn = self.storage._get_conn()
        os.makedirs(target_dir, exist_ok=True)

        # 获取所有页面
        pages = conn.execute(
            """
            SELECT kp.*, mm.content, mm.name as model_name, mm.tags,
                   f.path as folder_path
            FROM knowledge_pages kp
            LEFT JOIN mental_models mm ON mm.id = kp.mental_model_id
            LEFT JOIN knowledge_page_folders f ON f.id = kp.folder_id
            WHERE kp.bank_id = ?
            """,
            (bank_id,),
        ).fetchall()

        for page in pages:
            folder_path = page["folder_path"] or ""
            page_dir = os.path.join(target_dir, folder_path) if folder_path else target_dir
            os.makedirs(page_dir, exist_ok=True)

            # 文件名
            filename = f"{page['page_name']}.md"
            filepath = os.path.join(page_dir, filename)

            # frontmatter
            frontmatter = json.loads(page.get("frontmatter") or "{}")
            tags = json.loads(page.get("tags") or "[]")

            content_parts = ["---"]
            frontmatter.setdefault("title", page["page_name"])
            frontmatter.setdefault("tags", tags)
            frontmatter.setdefault("last_refreshed", page.get("last_refreshed_at", ""))
            frontmatter.setdefault("source_query", "")
            for k, v in frontmatter.items():
                content_parts.append(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}")
            content_parts.append("---\n")

            # 内容
            content_parts.append(page.get("content") or "(empty)")

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(content_parts))

        logger.info(f"Projected {len(pages)} pages to {target_dir}")

    def get_preset_pages(self) -> list[dict]:
        """预设 Knowledge Pages（bank 创建时自动初始化）。"""
        return [
            {
                "folder": "Architecture",
                "name": "项目结构",
                "query": "这个项目的模块结构和依赖关系是什么？",
                "tags": ["architecture", "project-structure"],
            },
            {
                "folder": "Architecture",
                "name": "构建方式",
                "query": "这个项目如何构建和部署？",
                "tags": ["architecture", "build"],
            },
            {
                "folder": "Patterns",
                "name": "反混淆技巧",
                "query": "我们在逆向中发现了哪些去混淆模式？",
                "tags": ["deobfuscation", "patterns"],
            },
            {
                "folder": "Patterns",
                "name": "API-Stub-策略",
                "query": "我们如何伪造 Windows API？",
                "tags": ["api-stub", "patterns"],
            },
            {
                "folder": "Conventions",
                "name": "代码风格",
                "query": "用户偏好的编码风格是什么？",
                "tags": ["conventions", "coding-style"],
            },
            {
                "folder": "Lessons",
                "name": "常见错误",
                "query": "哪些尝试失败了，为什么？",
                "tags": ["lessons", "errors"],
            },
        ]
