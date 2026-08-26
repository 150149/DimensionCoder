
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import MemoryStorage
from .utils import content_hash, count_tokens, output_language_directive, sanitize_text

logger = logging.getLogger(__name__)

class MentalModelManager:

    def __init__(
        self,
        storage: MemoryStorage,
        llm_client=None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.llm_client = llm_client
        self.config = config or {}

    async def create_model(
        self,
        bank_id: str,
        name: str,
        source_query: str,
        tags: Optional[list[str]] = None,
        max_tokens: int = 2048,
        trigger_config: Optional[dict] = None,
    ) -> str:
        model_id = self.storage.upsert_mental_model(
            bank_id=bank_id,
            name=name,
            source_query=source_query,
            content=None,
            tags=tags,
            max_tokens=max_tokens,
            trigger_config=trigger_config or {"refresh_after_consolidation": True},
        )

        try:
            asyncio.create_task(self.refresh_model(model_id, force=True))
        except RuntimeError:
            pass

        return model_id

    def get_model(self, model_id: str) -> Optional[dict]:
        return self.storage.get_mental_model(model_id)

    def get_models_by_tags(self, bank_id: str, tags: Optional[list[str]] = None) -> list[dict]:
        return self.storage.get_mental_models_by_tags(bank_id, tags)

    def get_stale_models(self, bank_id: str) -> list[dict]:
        return self.storage.get_stale_models(bank_id)

    async def refresh_model(self, model_id: str, force: bool = False) -> bool:
        model = self.storage.get_mental_model(model_id)
        if not model:
            return False

        bank_id = model["bank_id"]
        last_refreshed = model.get("last_refreshed_at")

        if not force and last_refreshed:
            tags = json.loads(model.get("tags", "[]"))
            new_count = self._count_new_facts(bank_id, last_refreshed, tags)
            if new_count == 0:
                logger.debug(f"Model {model_id} is fresh, skipping refresh")
                return False

        tags = json.loads(model.get("tags", "[]"))
        facts = self._get_relevant_facts(bank_id, last_refreshed, tags)
        observations = self.storage.list_observations(bank_id, tags)

        system_prompt = self._build_refresh_prompt()
        user_prompt = self._build_user_prompt(model, facts, observations)

        if not self.llm_client:
            logger.warning("No LLM client, cannot refresh mental model")
            return False

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        full_response = ""
        async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
            if event.get("type") == "text":
                full_response += event.get("text", "")

        new_content = sanitize_text(full_response)
        if not new_content:
            return False

        new_hash = content_hash(new_content)
        old_hash = model.get("content_hash")
        if not force and new_hash == old_hash:
            logger.debug(f"Model {model_id} content unchanged, skipping update")
            return False

        if model.get("content"):
            self.storage.save_previous_version(model_id, {
                "content": model["content"],
                "content_hash": old_hash,
                "timestamp": last_refreshed or "",
            })

        self.storage.upsert_mental_model(
            bank_id=bank_id,
            name=model["name"],
            source_query=model["source_query"],
            content=new_content,
            tags=tags,
            max_tokens=model.get("max_tokens", 2048),
            trigger_config=json.loads(model.get("trigger_config", "{}")),
            model_id=model_id,
        )

        logger.info(f"Mental model {model_id} refreshed")
        return True

    def _count_new_facts(self, bank_id: str, since: str, tags: Optional[list[str]]) -> int:
        conn = self.storage._get_conn()
        if tags:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND created_at > ?",
                (bank_id, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM memory_facts WHERE bank_id = ? AND created_at > ?",
                (bank_id, since),
            ).fetchone()
        return row["c"] if row else 0

    def _get_relevant_facts(self, bank_id: str, since: Optional[str], tags: Optional[list[str]]) -> list[dict]:
        conn = self.storage._get_conn()
        if since:
            rows = conn.execute(
                "SELECT * FROM memory_facts WHERE bank_id = ? AND created_at > ? ORDER BY created_at ASC LIMIT 100",
                (bank_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_facts WHERE bank_id = ? ORDER BY created_at ASC LIMIT 100",
                (bank_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _build_refresh_prompt(self) -> str:
        import os
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "memory", "mental-model-refresh.md",
        )
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            prompt = _DEFAULT_REFRESH_PROMPT
        prompt += output_language_directive(self.config.get("llm_output_language", ""))
        return prompt

    def _build_user_prompt(self, model: dict, facts: list[dict], observations: list[dict]) -> str:
        parts = [
            f"MODEL NAME: {model['name']}",
            f"SOURCE QUERY: {model['source_query']}",
        ]
        if model.get("content"):
            parts.append(f"CURRENT CONTENT:\n{model['content']}")

        if observations:
            parts.append("OBSERVATIONS:")
            for obs in observations[:20]:
                parts.append(f"- {obs.get('text', '')} (proof: {obs.get('proof_count', 1)})")

        if facts:
            parts.append("NEW FACTS:")
            for f in facts[:30]:
                parts.append(f"- {f.get('fact_text', '')} (type: {f.get('fact_type', '')})")

        return "\n\n".join(parts)

_DEFAULT_REFRESH_PROMPT = """You are a mental model refresh assistant. Update the model content based on new information.

RULES:
- Only modify sections affected by the new facts/observations
- Preserve unrelated sections unchanged
- If no relevant new information, return the current content unchanged
- Maintain a clear, structured format
- Base content on observations (consolidated facts) where available
- Do not reference other mental models (prevent feedback loops)
"""
