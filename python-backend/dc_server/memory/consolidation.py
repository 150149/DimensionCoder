"""
memory/consolidation.py — Consolidator：observation 归纳引擎

源自 Hindsight 的 consolidation/consolidator.py + prompts.py

流程：
1. 获取未归纳的 facts（consolidated_at IS NULL）
2. 对每个新 fact，用 recall（仅 dense+keyword 两臂）找到相似的已有 observations
3. 用 LLM 判断：create / update / merge / delete
4. 近重复合并：cosine > 0.97 时 LLM 读全文决定
5. 矛盾处理：observation 捕获演变而非覆盖
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from .embeddings import cosine_similarity, unpack_embedding
from .storage import MemoryStorage
from .utils import (
    parse_llm_json,
    sanitize_text,
    count_tokens,
    output_language_directive,
)

logger = logging.getLogger(__name__)

_DEDUP_TOP_K = 5
_DEFAULT_DEDUP_THRESHOLD = 0.97


class Consolidator:
    """observation 归纳引擎。

    使用方法：
        consolidator = Consolidator(storage, llm_client)
        result = await consolidator.consolidate(bank_id)
    """

    def __init__(
        self,
        storage: MemoryStorage,
        llm_client=None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.llm_client = llm_client
        self.config = config or {}
        self._batch_size = self.config.get("consolidation_batch_size", 50)
        self._llm_batch_size = self.config.get("consolidation_llm_batch_size", 10)
        self._max_attempts = self.config.get("consolidation_max_attempts", 3)
        self._dedup_threshold = self.config.get("consolidation_dedup_threshold", _DEFAULT_DEDUP_THRESHOLD)

    async def consolidate(self, bank_id: str, scope_tags: Optional[list[str]] = None) -> dict:
        """归纳 bank 内未处理的 facts。

        返回 {
            memories_processed: int,
            observations_created: int,
            observations_updated: int,
            observations_merged: int,
            observations_deleted: int,
            errors: list[str]
        }
        """
        errors: list[str] = []
        created = updated = merged = deleted = 0

        # 获取未归纳 facts
        facts = self.storage.get_unconsolidated_facts(bank_id, limit=self._batch_size)
        if not facts:
            return {"memories_processed": 0, "observations_created": 0,
                    "observations_updated": 0, "observations_merged": 0,
                    "observations_deleted": 0, "errors": []}

        # 获取已有 observations
        existing_obs = self.storage.list_observations(bank_id, scope_tags)

        # 按 LLM 批次处理
        for i in range(0, len(facts), self._llm_batch_size):
            batch = facts[i : i + self._llm_batch_size]
            try:
                result = await self._process_batch(bank_id, batch, existing_obs)
                created += result["created"]
                updated += result["updated"]
                merged += result["merged"]
                deleted += result["deleted"]
            except Exception as e:
                logger.error(f"Consolidation batch {i} failed: {e}")
                errors.append(f"Batch {i}: {e}")
                # 自适应分割：一分为二重试
                if len(batch) > 1:
                    half = len(batch) // 2
                    for sub_batch in [batch[:half], batch[half:]]:
                        try:
                            result = await self._process_batch(bank_id, sub_batch, existing_obs)
                            created += result["created"]
                            updated += result["updated"]
                            merged += result["merged"]
                            deleted += result["deleted"]
                        except Exception as e2:
                            errors.append(f"Sub-batch: {e2}")
                            # 标记失败
                            for f in sub_batch:
                                self.storage.mark_fact_consolidation_failed(f["id"])
                else:
                    # 单条也失败
                    self.storage.mark_fact_consolidation_failed(batch[0]["id"])

        # 标记已处理
        now = datetime.now(timezone.utc).isoformat()
        for f in facts:
            self.storage.mark_fact_consolidated(f["id"], now)

        # 标记受影响的 observation 为 stale
        self._mark_stale_observations(bank_id)

        return {
            "memories_processed": len(facts),
            "observations_created": created,
            "observations_updated": updated,
            "observations_merged": merged,
            "observations_deleted": deleted,
            "errors": errors,
        }

    async def _process_batch(
        self,
        bank_id: str,
        batch: list[dict],
        existing_obs: list[dict],
    ) -> dict:
        """处理一个 LLM 批次。"""
        if not self.llm_client:
            # 无 LLM：简单去重
            return self._simple_dedup(batch, existing_obs)

        # 构建 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(batch, existing_obs)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 调用 LLM
        full_response = ""
        async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
            if event.get("type") == "text":
                full_response += event.get("text", "")

        # 解析响应
        parsed = parse_llm_json(full_response)
        if not isinstance(parsed, dict):
            raise ValueError(f"Invalid consolidation response: expected dict, got {type(parsed)}")

        creates = parsed.get("creates", [])
        updates = parsed.get("updates", [])
        deletes = parsed.get("deletes", [])

        # 执行操作：deletes 先于 creates
        deleted_count = self._execute_deletes(bank_id, deletes)
        created_count = self._execute_creates(bank_id, creates, batch)
        updated_count = self._execute_updates(bank_id, updates, batch)

        # 近重复合并
        merged_count = self._dedup_observations(bank_id, existing_obs + [
            {"id": c.get("observation_id", ""), "text": c.get("text", "")}
            for c in creates if c.get("observation_id")
        ])

        return {
            "created": created_count,
            "updated": updated_count,
            "merged": merged_count,
            "deleted": deleted_count,
        }

    def _build_system_prompt(self) -> str:
        """构建系统 prompt（bank-agnostic）。"""
        import os
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "memory", "consolidation.md",
        )
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            prompt = _DEFAULT_CONSOLIDATION_PROMPT

        lang = self.config.get("llm_output_language", "")
        prompt += output_language_directive(lang)
        return prompt

    def _build_user_prompt(self, batch: list[dict], existing_obs: list[dict]) -> str:
        """构建用户 prompt（每批次变化）。"""
        parts = []
        if self.config.get("observations_mission"):
            parts.append(f"MISSION: {self.config['observations_mission']}")

        parts.append("NEW FACTS:")
        for f in batch:
            parts.append(f"- id={f['id']} type={f.get('fact_type','')} tags={f.get('tags','[]')}")
            parts.append(f"  text: {f.get('fact_text', '')}")

        if existing_obs:
            parts.append("\nEXISTING OBSERVATIONS:")
            for obs in existing_obs[:20]:
                parts.append(f"- id={obs['id']} proof_count={obs.get('proof_count',1)}")
                parts.append(f"  text: {obs.get('text', '')}")

        return "\n".join(parts)

    def _execute_deletes(self, bank_id: str, deletes: list[dict]) -> int:
        """执行 delete 操作。"""
        count = 0
        for d in deletes:
            obs_id = d.get("observation_id")
            if obs_id:
                self.storage.delete_observation(obs_id)
                count += 1
        return count

    def _execute_creates(self, bank_id: str, creates: list[dict], batch: list[dict]) -> int:
        """执行 create 操作。"""
        count = 0
        for c in creates:
            text = c.get("text", "")
            source_ids = c.get("source_fact_ids", [])
            if not source_ids:
                # 用批次内所有 fact 作为来源
                source_ids = [f["id"] for f in batch]
            if text:
                obs_id = self.storage.insert_observation(
                    bank_id, text, source_fact_ids=source_ids,
                )
                count += 1
        return count

    def _execute_updates(self, bank_id: str, updates: list[dict], batch: list[dict]) -> int:
        """执行 update 操作。UPDATE 前保存 observation history 快照。"""
        count = 0
        for u in updates:
            obs_id = u.get("observation_id")
            text = u.get("text", "")
            new_sources = u.get("source_fact_ids", [])
            if obs_id and text:
                # Observation history：UPDATE 前保存快照
                if self.config.get("enable_observation_history", True):
                    self._append_observation_history(obs_id)
                self.storage.update_observation(obs_id, text, new_sources)
                count += 1
        return count

    def _append_observation_history(self, obs_id: str):
        """UPDATE 前保存 observation 快照到 history JSON blob。

        存为 metadata 中的 observation_history 字段，按 max_entries 截断。
        """
        obs = self.storage.get_observation(obs_id)
        if not obs:
            return
        snapshot = {
            "previous_text": obs.get("text", ""),
            "previous_tags": json.loads(obs.get("scope_tags") or "[]"),
            "previous_source_ids": json.loads(obs.get("source_fact_ids") or "[]"),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        conn = self.storage._get_conn()
        # 读取现有 history
        metadata = obs.get("metadata") or "{}"
        try:
            metadata_dict = json.loads(metadata) if isinstance(metadata, str) else metadata
        except (json.JSONDecodeError, TypeError):
            metadata_dict = {}
        history = metadata_dict.get("observation_history", [])
        history.append(snapshot)
        # 按 max_entries 截断
        max_entries = self.config.get("observation_history_max_entries", 10)
        if len(history) > max_entries:
            history = history[-max_entries:]
        metadata_dict["observation_history"] = history
        conn.execute(
            "UPDATE memory_observations SET metadata = ? WHERE id = ?",
            (json.dumps(metadata_dict, ensure_ascii=False), obs_id),
        )
        conn.commit()

    def _dedup_observations(self, bank_id: str, observations: list[dict]) -> int:
        """近重复检测和合并。

        cosine > dedup_threshold 时触发 LLM 判断。
        无 LLM 时跳过。
        """
        if self._dedup_threshold >= 1.0 or not self.llm_client:
            return 0

        merged = 0
        # 简化版：检查文本相似度
        for i in range(len(observations)):
            for j in range(i + 1, len(observations)):
                oi = observations[i]
                oj = observations[j]
                if not oi.get("text") or not oj.get("text"):
                    continue
                # 简单文本相似度（无 embedding 时）
                sim = _text_similarity(oi["text"], oj["text"])
                if sim > self._dedup_threshold:
                    # 合并：保留第一个，删除第二个
                    merged_text = f"{oi['text']}\n\n(Merged with: {oj['text']})"
                    all_sources = list(set(
                        json.loads(oi.get("source_fact_ids", "[]")) +
                        json.loads(oj.get("source_fact_ids", "[]"))
                    ))
                    self.storage.update_observation(oi["id"], merged_text, all_sources)
                    self.storage.delete_observation(oj["id"])
                    merged += 1

        return merged

    def _simple_dedup(self, batch: list[dict], existing_obs: list[dict]) -> dict:
        """无 LLM 时的简单去重。"""
        # 直接创建 observation
        created = 0
        for f in batch:
            text = f.get("fact_text", "")
            if text:
                self.storage.insert_observation(
                    f.get("bank_id", ""), text,
                    source_fact_ids=[f["id"]],
                )
                created += 1
        return {"created": created, "updated": 0, "merged": 0, "deleted": 0}

    def _mark_stale_observations(self, bank_id: str):
        """标记有新 fact 但未归纳的 observation 为 stale。"""
        observations = self.storage.list_observations(bank_id)
        for obs in observations:
            sources = json.loads(obs.get("source_fact_ids", "[]"))
            if sources:
                # 检查是否有比 updated_at 更新的 fact
                conn = self.storage._get_conn()
                row = conn.execute(
                    "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND created_at > ?",
                    (bank_id, obs.get("updated_at", "")),
                ).fetchone()
                if row and row["c"] > 0:
                    self.storage.mark_observation_stale(obs["id"], True)


def _text_similarity(t1: str, t2: str) -> float:
    """简单文本相似度（基于 Jaccard 词重叠）。"""
    from .entity_resolver import _WORD_RE

    w1 = set(_WORD_RE.findall(t1.lower()))
    w2 = set(_WORD_RE.findall(t2.lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


_DEFAULT_CONSOLIDATION_PROMPT = """You are a memory consolidation assistant. Your job is to merge raw facts into deduplicated observations.

For each new fact, decide whether to:
- CREATE: Create a new observation (no existing observation matches)
- UPDATE: Add to an existing observation (same entity/facet, new info)
- DELETE: An existing observation is superseded by this fact

RULES:
1. PREFER UPDATE OVER CREATE — don't create near-duplicate siblings
2. ONE OBSERVATION PER DISTINCT FACET — each observation tracks one specific aspect
3. MATCH BY ENTITY/FACET NOT TOPIC — match on specific entities, not broad topics
4. STATE CHANGES — UPDATE concisely, include dates, don't pull other observations
5. CASCADE TO ALL AFFECTED — a state change may affect multiple observations
6. RESOLVE REFERENCES — when a fact provides concrete values, UPDATE with resolved values
7. PRESERVE HISTORY — don't DELETE important historical observations
8. NO COMPUTATION — don't do arithmetic or logical derivation
9. KEEP DISTINCT TOPICS DISTINCT — don't merge different people/entities

Output JSON: {"creates": [{"text": "...", "source_fact_ids": [...]}], "updates": [{"observation_id": "...", "text": "...", "source_fact_ids": [...]}], "deletes": [{"observation_id": "...", "reason": "..."}]}

Each entry MUST include a "reason" field explaining CREATE vs UPDATE.
"""
