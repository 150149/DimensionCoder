"""
memory/retain.py — Retainer：存入完整管线

源自 Hindsight 的 retain/orchestrator.py + link_creation.py

三阶段架构：
- Phase 0: chunk_text → 每 chunk LLM 提取 facts → 并行 asyncio.gather
- Phase 1: embedding 生成 → 退化文本拒绝 → entity resolution → 临时偏移
- Phase 2: BEGIN TXN → insert facts(BLOB) → fact_entities → temporal links
  → semantic links → causal links → entity stats → cooccurrences → COMMIT
- Phase 3: 异步触发 consolidation

四种连接类型：
1. Entity connections: 通过 memory_fact_entities 表间接连接（查询时派生）
2. Temporal connections: 时间接近的 fact 互链，weight=max(0.3, 1.0-time_diff_h/time_window_h)
3. Semantic connections: embedding cosine > threshold
4. Causal connections: 从 extraction 的 causal_relations 构建，仅 "caused_by"
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .embeddings import EmbeddingProvider, cosine_similarity, pack_embedding
from .entity_resolver import EntityResolver
from .fact_extraction import FactExtractor, ExtractedFact
from .storage import MemoryStorage, MAX_TEMPORAL_LINKS_PER_UNIT, MAX_SEMANTIC_LINKS_PER_UNIT
from .utils import (
    SECONDS_PER_FACT,
    apply_temporal_offset,
    chunk_id,
    content_hash,
    is_degenerate_text,
    sanitize_text,
)

logger = logging.getLogger(__name__)

# 时间窗口（小时）——超过此窗口的 temporal link 权重降至最低 0.3
TEMPORAL_WINDOW_H = 72.0  # 3 天


class Retainer:
    """存入管线——从原始文本到结构化记忆。

    使用方法：
        retainer = Retainer(storage, llm_client, embedding_provider)
        await retainer.retain(bank_id, text, tags=["deobfuscation"],
                              source_ref={"task_id": "...", "step_id": "..."})
    """

    def __init__(
        self,
        storage: MemoryStorage,
        llm_client=None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        entity_resolver: Optional[EntityResolver] = None,
        fact_extractor: Optional[FactExtractor] = None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.llm_client = llm_client
        self.embedding_provider = embedding_provider
        self.entity_resolver = entity_resolver or EntityResolver(storage)
        self.config = config or {}
        self._fact_extractor = fact_extractor

    def _get_extractor(self) -> FactExtractor:
        if self._fact_extractor:
            return self._fact_extractor
        if not self.llm_client:
            raise RuntimeError("No LLM client configured for fact extraction")
        self._fact_extractor = FactExtractor(
            llm_client=self.llm_client,
            model=self.config.get("retain_model", ""),
            chunk_size=self.config.get("retain_chunk_size", 3000),
            structured_chunk_size=self.config.get("retain_structured_chunk_size", 8192),
            extraction_mode=self.config.get("retain_extraction_mode", "concise"),
            mission=self.config.get("retain_mission", ""),
            output_language=self.config.get("llm_output_language", ""),
            max_retries=self.config.get("llm_max_retries", 3),
        )
        return self._fact_extractor

    async def retain(
        self,
        bank_id: str,
        text: str,
        tags: Optional[list[str]] = None,
        source_ref: Optional[dict] = None,
        event_date: Optional[str] = None,
        document_id: Optional[str] = None,
        extract_causal_links: bool = True,
    ) -> dict:
        """存入文本，提取事实，生成连接。

        返回 {
            facts_extracted: int,
            facts_stored: int,
            entities_resolved: int,
            links_created: int,
            errors: list[str]
        }
        """
        tags = tags or []
        source_ref = source_ref or {}
        errors: list[str] = []
        text = sanitize_text(text)

        if not text or is_degenerate_text(text):
            return {"facts_extracted": 0, "facts_stored": 0, "entities_resolved": 0,
                    "links_created": 0, "errors": ["Degenerate or empty text"]}

        # ── Phase 0: LLM 提取 ──────────────────────────────
        try:
            extractor = self._get_extractor()
            extracted_facts = await extractor.extract(text, event_date, document_id)
        except Exception as e:
            logger.error(f"Fact extraction failed: {e}")
            errors.append(f"Extraction error: {e}")
            return {"facts_extracted": 0, "facts_stored": 0, "entities_resolved": 0,
                    "links_created": 0, "errors": errors}

        if not extracted_facts:
            errors.append("No facts extracted")
            return {"facts_extracted": 0, "facts_stored": 0, "entities_resolved": 0,
                    "links_created": 0, "errors": errors}

        # ── Phase 1: embedding + entity resolution ─────────
        doc_id = document_id or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        # 生成 embedding
        embeddings: list[Optional[list[float]]] = []
        if self.embedding_provider and self.embedding_provider.is_available():
            fact_texts = [f.fact_text for f in extracted_facts]
            embeddings = await self.embedding_provider.embed_batch(fact_texts)

        # 实体解析
        all_entity_names: list[str] = []
        for f in extracted_facts:
            all_entity_names.extend(f.entities)
        entity_map = await self.entity_resolver.resolve_entities_batch(bank_id, all_entity_names)

        # ── Phase 2: 原子写入 ───────────────────────────────
        stored_fact_ids: list[str] = []
        stored_facts: list[dict] = []  # 用于 link creation
        entities_count = 0

        for i, fact in enumerate(extracted_facts):
            # 临时偏移
            mentioned_at = fact.occurred_start or event_date
            if mentioned_at:
                mentioned_at = apply_temporal_offset(mentioned_at, i)

            # embedding
            emb_bytes = None
            if i < len(embeddings) and embeddings[i]:
                emb_bytes = pack_embedding(embeddings[i])

            # chunk_id
            cid = chunk_id(bank_id, doc_id, i)

            # metadata
            meta = {
                "where": fact.where,
                "why": fact.why,
                "when": fact.when,
            }
            if source_ref:
                meta["source_task_id"] = source_ref.get("task_id", "")
                meta["source_step_id"] = source_ref.get("step_id", "")

            fact_id = self.storage.insert_fact(
                bank_id=bank_id,
                fact_text=fact.fact_text,
                fact_type=fact.fact_type,
                fact_kind=fact.fact_kind,
                context="",
                occurred_start=fact.occurred_start,
                occurred_end=fact.occurred_end,
                mentioned_at=mentioned_at,
                metadata=meta,
                chunk_id=cid,
                document_id=doc_id,
                tags=tags,
                source_ref=source_ref,
                embedding=emb_bytes,
            )

            if fact_id:
                stored_fact_ids.append(fact_id)
                stored_facts.append({
                    "id": fact_id,
                    "fact": fact,
                    "embedding": embeddings[i] if i < len(embeddings) else None,
                    "mentioned_at": mentioned_at,
                    "tags": tags,
                })

                # 链接 fact-entity
                for ename in fact.entities:
                    eid = entity_map.get(ename)
                    if eid:
                        self.storage.link_fact_entity(fact_id, eid)
                        self.storage.update_entity_stats(eid)
                        entities_count += 1

        # 共现更新
        for fact_data in stored_facts:
            fact = fact_data["fact"]
            entity_ids = [entity_map.get(e) for e in fact.entities if entity_map.get(e)]
            for j in range(len(entity_ids)):
                for k in range(j + 1, len(entity_ids)):
                    self.storage.upsert_cooccurrence(entity_ids[j], entity_ids[k])

        # ── 创建连接 ────────────────────────────────────────
        links_created = 0
        links_created += self._create_temporal_links(stored_facts, bank_id)
        links_created += self._create_semantic_links(stored_facts)
        if extract_causal_links:
            links_created += self._create_causal_links(stored_facts, extracted_facts, stored_fact_ids)

        # ── Phase 3: 异步触发 consolidation ─────────────────
        if self.config.get("enable_auto_consolidation", True):
            try:
                asyncio.create_task(self._trigger_consolidation(bank_id))
            except RuntimeError:
                pass  # 无 event loop 时跳过

        return {
            "facts_extracted": len(extracted_facts),
            "facts_stored": len(stored_fact_ids),
            "entities_resolved": len(entity_map),
            "links_created": links_created,
            "errors": errors,
        }

    def _create_temporal_links(self, stored_facts: list[dict], bank_id: str) -> int:
        """创建时间链接。

        权重：max(0.3, 1.0 - time_diff_h / time_window_h)
        批内只同 fact_type 互链
        上限 MAX_TEMPORAL_LINKS_PER_UNIT=20
        """
        if len(stored_facts) < 2:
            return 0

        links: list[tuple[str, str, str, float]] = []
        for i, fi in enumerate(stored_facts):
            count = 0
            candidates: list[tuple[float, str]] = []
            for j, fj in enumerate(stored_facts):
                if i == j:
                    continue
                # 只同 fact_type 互链
                if fi["fact"].fact_type != fj["fact"].fact_type:
                    continue
                # 计算时间差
                time_diff_h = self._time_diff_hours(fi["mentioned_at"], fj["mentioned_at"])
                if time_diff_h is None:
                    continue
                weight = max(0.3, 1.0 - time_diff_h / TEMPORAL_WINDOW_H)
                if weight > 0.3:
                    candidates.append((weight, fj["id"]))

            # 按权重降序，取前 MAX_TEMPORAL_LINKS_PER_UNIT
            candidates.sort(reverse=True)
            for weight, target_id in candidates[:MAX_TEMPORAL_LINKS_PER_UNIT]:
                links.append((fi["id"], target_id, "temporal", weight))

        if links:
            self.storage.bulk_insert_links(links)
        return len(links)

    def _create_semantic_links(self, stored_facts: list[dict]) -> int:
        """创建语义连接。cosine > threshold（默认 0.3）。"""
        threshold = self.config.get("semantic_link_min_similarity", 0.3)
        links: list[tuple[str, str, str, float]] = []

        for i, fi in enumerate(stored_facts):
            if not fi.get("embedding"):
                continue
            count = 0
            candidates: list[tuple[float, str]] = []
            for j, fj in enumerate(stored_facts):
                if i == j or not fj.get("embedding"):
                    continue
                sim = cosine_similarity(fi["embedding"], fj["embedding"])
                if sim > threshold:
                    candidates.append((sim, fj["id"]))

            candidates.sort(reverse=True)
            for sim, target_id in candidates[:MAX_SEMANTIC_LINKS_PER_UNIT]:
                links.append((fi["id"], target_id, "semantic", sim))

        if links:
            self.storage.bulk_insert_links(links)
        return len(links)

    def _create_causal_links(
        self,
        stored_facts: list[dict],
        extracted_facts: list[ExtractedFact],
        stored_ids: list[str],
    ) -> int:
        """创建因果连接。仅 "caused_by"。"""
        links: list[tuple[str, str, str, float]] = []

        for i, (fact_data, extracted) in enumerate(zip(stored_facts, extracted_facts)):
            if not extracted.causal_relations:
                continue
            for rel in extracted.causal_relations:
                target_idx = rel.get("target_index")
                if target_idx is None or target_idx < 0 or target_idx >= i:
                    continue
                target_id = stored_ids[target_idx] if target_idx < len(stored_ids) else None
                if target_id:
                    # caused_by: 当前 fact 被目标 fact 导致
                    # link direction: source=当前, target=目标, type=caused_by
                    links.append((fact_data["id"], target_id, "causal", 1.0))

        if links:
            self.storage.bulk_insert_links(links)
        return len(links)

    def _time_diff_hours(self, ts1: Optional[str], ts2: Optional[str]) -> Optional[float]:
        """计算两个 ISO 时间戳的小时差。"""
        if not ts1 or not ts2:
            return None
        try:
            dt1 = datetime.fromisoformat(ts1.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(ts2.replace("Z", "+00:00"))
            return abs((dt1 - dt2).total_seconds()) / 3600.0
        except (ValueError, TypeError):
            return None

    async def _trigger_consolidation(self, bank_id: str):
        """异步触发 consolidation（不阻塞主流程）。"""
        try:
            from .consolidation import Consolidator
            consolidator = Consolidator(
                self.storage,
                llm_client=self.llm_client,
                config=self.config,
            )
            await consolidator.consolidate(bank_id)
        except Exception as e:
            logger.warning(f"Auto-consolidation failed (non-blocking): {e}")

    def build_retain_text(
        self,
        conv: Optional[list[dict]],
        artifacts: list[dict],
        task: Optional[dict],
        step_id: str,
    ) -> str:
        """将 DC 的 step 数据转换为 retain 输入文本。"""
        return self.storage.build_retain_text(conv, artifacts, task, step_id)

    def delete_fact(self, fact_id: str) -> bool:
        """删除 fact 并触发 graph maintenance（relink top-up + entity prune）。

        删除时在**同一事务内**执行：
        1. Relink top-up: 找出出边指向被删 fact 的幸存 fact，补插缺失链接
        2. Entity prune: 找出被删 fact 引用的实体，删除无引用的孤立实体
        """
        return self.storage.delete_fact(fact_id)
