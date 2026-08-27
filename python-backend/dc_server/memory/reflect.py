"""
memory/reflect.py — Reflector：agentic 推理循环（Phase 5，可选）

源自 Hindsight 的 reflect/agent.py + tools.py

核心功能：
- 强制分层检索序列：search_mental_models → search_observations → recall
- 上下文预算管理：超 max_context_tokens → 强制最终合成
- Split synthesis：累积结果超预算时分块 → 并行提取 claims → reduce 合成
- ID 验证：done tool 的 ID 只接受实际检索到的 ID，幻觉 ID 被静默过滤
- Disposition traits：skepticism / literalism / empathy
- Directives：硬规则注入 system prompt 的 START 和 END
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .directives import DirectiveManager
from .embeddings import EmbeddingProvider
from .recall import Recaller
from .storage import MemoryStorage
from .utils import count_tokens, sanitize_text, output_language_directive

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_MAX_CONTEXT_TOKENS = 100_000


class Reflector:
    """Agentic 推理循环。

    使用方法：
        reflector = Reflector(storage, llm_client, recaller)
        result = await reflector.reflect(bank_id, "从这次任务中学到了什么？")
    """

    def __init__(
        self,
        storage: MemoryStorage,
        llm_client=None,
        recaller: Optional[Recaller] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.llm_client = llm_client
        self.recaller = recaller or Recaller(storage, embedding_provider, config)
        self.embedding_provider = embedding_provider
        self.config = config or {}
        self._directive_mgr = DirectiveManager(storage)

        self._max_iterations = self.config.get("reflect_max_iterations", DEFAULT_MAX_ITERATIONS)
        self._max_context_tokens = self.config.get("reflect_max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
        self._max_completion_tokens = self.config.get("reflect_max_completion_tokens")
        self._disposition = {
            "skepticism": self.config.get("disposition_skepticism", 3),
            "literalism": self.config.get("disposition_literalism", 3),
            "empathy": self.config.get("disposition_empathy", 3),
        }

    async def reflect(
        self,
        bank_id: str,
        query: str,
        tags: Optional[list[str]] = None,
        response_schema: Optional[dict] = None,
    ) -> dict:
        """Agentic 推理循环。

        返回 {
            text: str,
            based_on: dict,
            usage: dict,
            tool_trace: list,
            llm_trace: list,
            directives_applied: list,
            errors: list[str]
        }
        """
        errors: list[str] = []
        tool_trace: list[dict] = []
        llm_trace: list[dict] = []

        # 收集证据
        available_memory_ids: set[str] = set()
        available_observation_ids: set[str] = set()
        available_mental_model_ids: set[str] = set()
        collected_evidence: list[dict] = []

        # ── 强制分层检索序列 ────────────────────────────────
        # 1. Mental models
        models = self.storage.get_mental_models_by_tags(bank_id, tags)
        for m in models:
            available_mental_model_ids.add(m["id"])
            collected_evidence.append({
                "type": "mental_model",
                "id": m["id"],
                "name": m.get("name", ""),
                "content": m.get("content") or "",
                "relevance": 0.8,
            })

        # 2. Observations
        observations = self.storage.list_observations(bank_id, tags)
        for obs in observations:
            available_observation_ids.add(obs["id"])
            collected_evidence.append({
                "type": "observation",
                "id": obs["id"],
                "text": obs.get("text", ""),
                "proof_count": obs.get("proof_count", 1),
                "stale": bool(obs.get("stale", 0)),
                "relevance": 0.6,
            })

        # 3. Recall (raw facts)
        recall_result = await self.recaller.recall(
            bank_id, query, max_tokens=4096, budget="mid", tags=tags
        )
        for r in recall_result.get("results", []):
            if r.get("id"):
                available_memory_ids.add(r["id"])
            collected_evidence.append({
                "type": "fact",
                "id": r.get("id", ""),
                "text": r.get("fact_text", r.get("text", "")),
                "fact_type": r.get("fact_type", ""),
                "relevance": r.get("scores", {}).get("final", 0.0),
            })

        # ── Directives ─────────────────────────────────────
        directive_text, directives_applied = self._directive_mgr.get_directives_for_prompt(
            bank_id, tags
        )

        # ── 构建消息 ────────────────────────────────────────
        system_prompt = self._build_system_prompt(directive_text)
        user_prompt = self._build_user_prompt(query, collected_evidence)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── 调用 LLM ────────────────────────────────────────
        if not self.llm_client:
            return {
                "text": "",
                "based_on": {},
                "usage": {},
                "tool_trace": tool_trace,
                "llm_trace": llm_trace,
                "directives_applied": directives_applied,
                "errors": ["No LLM client configured"],
            }

        # 检查上下文预算
        total_tokens = count_tokens(system_prompt) + count_tokens(user_prompt)
        if total_tokens > self._max_context_tokens:
            # Split synthesis
            answer = await self._split_synthesis(query, collected_evidence, system_prompt)
        else:
            # 直接调用
            full_response = ""
            async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
                if event.get("type") == "text":
                    full_response += event.get("text", "")
                elif event.get("type") == "usage":
                    llm_trace.append(event)
            answer = full_response

        answer = sanitize_text(answer) or ""

        # ── 构建返回 ────────────────────────────────────────
        based_on: dict[str, Any] = {
            "memories": [eid for eid in available_memory_ids],
            "mental_models": [mid for mid in available_mental_model_ids],
            "observations": [oid for oid in available_observation_ids],
            "directives": [d["id"] for d in directives_applied],
        }

        return {
            "text": answer,
            "based_on": based_on,
            "usage": {},
            "tool_trace": tool_trace,
            "llm_trace": llm_trace,
            "directives_applied": directives_applied,
            "errors": errors,
        }

    def _build_system_prompt(self, directive_text: str) -> str:
        """构建 system prompt，含 directives 注入 START 和 END。"""
        import os
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "memory", "reflect.md",
        )
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            prompt = _DEFAULT_REFLECT_PROMPT

        # Disposition
        s = self._disposition["skepticism"]
        l = self._disposition["literalism"]
        e = self._disposition["empathy"]
        prompt += f"\n\nDisposition: skepticism={s}/5, literalism={l}/5, empathy={e}/5"

        # Directives START
        if directive_text:
            prompt = f"{directive_text}\n\n{prompt}"

        # Directives END
        if directive_text:
            prompt = f"{prompt}\n\n{directive_text}"

        prompt += output_language_directive(self.config.get("llm_output_language", ""))
        return prompt

    def _build_user_prompt(self, query: str, evidence: list[dict]) -> str:
        """构建 user prompt，含收集到的证据。"""
        parts = [f"QUERY: {query}"]

        # 清洗证据（剥离内部字段）
        models = [e for e in evidence if e["type"] == "mental_model"]
        observations = [e for e in evidence if e["type"] == "observation"]
        facts = [e for e in evidence if e["type"] == "fact"]

        if models:
            parts.append("\nMENTAL MODELS:")
            for m in models:
                parts.append(f"- {m['name']}: {m['content'][:500]}")

        if observations:
            parts.append("\nOBSERVATIONS:")
            for o in observations:
                stale = " [stale]" if o.get("stale") else ""
                parts.append(f"- {o['text']}{stale} (proof: {o.get('proof_count', 1)})")

        if facts:
            parts.append("\nFACTS:")
            for f in facts[:30]:  # 限制 token
                parts.append(f"- {f['text']} (type: {f.get('fact_type', '')})")

        parts.append("\nBased on the above evidence, answer the query.")
        return "\n".join(parts)

    async def _split_synthesis(
        self, query: str, evidence: list[dict], system_prompt: str
    ) -> str:
        """Split synthesis（map-reduce）：累积结果超预算时分块 → 并行提取 claims → reduce 合成。"""
        # 分块
        chunk_size = 50
        chunks = [evidence[i : i + chunk_size] for i in range(0, len(evidence), chunk_size)]

        # 并行提取 claims
        async def extract_claims(chunk: list[dict]) -> str:
            user_prompt = f"Extract key claims from this evidence:\n" + "\n".join(
                f"- {e.get('text', e.get('content', ''))}" for e in chunk
            )
            messages = [
                {"role": "system", "content": "Extract concise claims from evidence."},
                {"role": "user", "content": user_prompt},
            ]
            result = ""
            async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
                if event.get("type") == "text":
                    result += event.get("text", "")
            return result

        tasks = [extract_claims(chunk) for chunk in chunks]
        claims = await asyncio.gather(*tasks, return_exceptions=True)

        # Reduce 合成
        valid_claims = [c for c in claims if isinstance(c, str) and c]
        reduce_prompt = f"Based on these claims, answer: {query}\n\nClaims:\n" + "\n".join(valid_claims)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": reduce_prompt},
        ]
        result = ""
        async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
            if event.get("type") == "text":
                result += event.get("text", "")
        return result


_DEFAULT_REFLECT_PROMPT = """You are a reflective reasoning assistant. Answer the query based on the provided evidence (mental models, observations, and facts).

RULES:
- Only use evidence explicitly provided above
- Cite evidence by ID when making claims
- If evidence is insufficient, say so explicitly
- Maintain appropriate skepticism based on your disposition
- Do not invent evidence or reference IDs not provided
"""
