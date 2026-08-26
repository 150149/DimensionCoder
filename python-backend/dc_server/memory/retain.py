
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

TEMPORAL_WINDOW_H = 72.0

class Retainer:

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
        tags = tags or []
        source_ref = source_ref or {}
        errors: list[str] = []
        text = sanitize_text(text)

        if not text or is_degenerate_text(text):
            return {"facts_extracted": 0, "facts_stored": 0, "entities_resolved": 0,
                    "links_created": 0, "errors": ["Degenerate or empty text"]}

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

        doc_id = document_id or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        embeddings: list[Optional[list[float]]] = []
        if self.embedding_provider and self.embedding_provider.is_available():
            fact_texts = [f.fact_text for f in extracted_facts]
            embeddings = await self.embedding_provider.embed_batch(fact_texts)

        all_entity_names: list[str] = []
        for f in extracted_facts:
            all_entity_names.extend(f.entities)
        entity_map = await self.entity_resolver.resolve_entities_batch(bank_id, all_entity_names)

        stored_fact_ids: list[str] = []
        stored_facts: list[dict] = []
        entities_count = 0

        for i, fact in enumerate(extracted_facts):
            mentioned_at = fact.occurred_start or event_date
            if mentioned_at:
                mentioned_at = apply_temporal_offset(mentioned_at, i)

            emb_bytes = None
            if i < len(embeddings) and embeddings[i]:
                emb_bytes = pack_embedding(embeddings[i])

            cid = chunk_id(bank_id, doc_id, i)

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

                for ename in fact.entities:
                    eid = entity_map.get(ename)
                    if eid:
                        self.storage.link_fact_entity(fact_id, eid)
                        self.storage.update_entity_stats(eid)
                        entities_count += 1

        for fact_data in stored_facts:
            fact = fact_data["fact"]
            entity_ids = [entity_map.get(e) for e in fact.entities if entity_map.get(e)]
            for j in range(len(entity_ids)):
                for k in range(j + 1, len(entity_ids)):
                    self.storage.upsert_cooccurrence(entity_ids[j], entity_ids[k])

        links_created = 0
        links_created += self._create_temporal_links(stored_facts, bank_id)
        links_created += self._create_semantic_links(stored_facts)
        if extract_causal_links:
            links_created += self._create_causal_links(stored_facts, extracted_facts, stored_fact_ids)

        if self.config.get("enable_auto_consolidation", True):
            try:
                asyncio.create_task(self._trigger_consolidation(bank_id))
            except RuntimeError:
                pass

        return {
            "facts_extracted": len(extracted_facts),
            "facts_stored": len(stored_fact_ids),
            "entities_resolved": len(entity_map),
            "links_created": links_created,
            "errors": errors,
        }

    def _create_temporal_links(self, stored_facts: list[dict], bank_id: str) -> int:
        if len(stored_facts) < 2:
            return 0

        links: list[tuple[str, str, str, float]] = []
        for i, fi in enumerate(stored_facts):
            count = 0
            candidates: list[tuple[float, str]] = []
            for j, fj in enumerate(stored_facts):
                if i == j:
                    continue
                if fi["fact"].fact_type != fj["fact"].fact_type:
                    continue
                time_diff_h = self._time_diff_hours(fi["mentioned_at"], fj["mentioned_at"])
                if time_diff_h is None:
                    continue
                weight = max(0.3, 1.0 - time_diff_h / TEMPORAL_WINDOW_H)
                if weight > 0.3:
                    candidates.append((weight, fj["id"]))

            candidates.sort(reverse=True)
            for weight, target_id in candidates[:MAX_TEMPORAL_LINKS_PER_UNIT]:
                links.append((fi["id"], target_id, "temporal", weight))

        if links:
            self.storage.bulk_insert_links(links)
        return len(links)

    def _create_semantic_links(self, stored_facts: list[dict]) -> int:
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
                    links.append((fact_data["id"], target_id, "causal", 1.0))

        if links:
            self.storage.bulk_insert_links(links)
        return len(links)

    def _time_diff_hours(self, ts1: Optional[str], ts2: Optional[str]) -> Optional[float]:
        if not ts1 or not ts2:
            return None
        try:
            dt1 = datetime.fromisoformat(ts1.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(ts2.replace("Z", "+00:00"))
            return abs((dt1 - dt2).total_seconds()) / 3600.0
        except (ValueError, TypeError):
            return None

    async def _trigger_consolidation(self, bank_id: str):
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
        return self.storage.build_retain_text(conv, artifacts, task, step_id)

    def delete_fact(self, fact_id: str) -> bool:
        return self.storage.delete_fact(fact_id)
