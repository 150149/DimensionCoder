
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .embeddings import EmbeddingProvider, cosine_similarity, unpack_embedding
from .storage import MemoryStorage
from .utils import count_tokens, truncate_to_tokens

logger = logging.getLogger(__name__)

RRF_K = 60

BUDGET_OVERFETCH = {
    "low": 100,
    "mid": 300,
    "high": 1000,
}

class Recaller:

    def __init__(
        self,
        storage: MemoryStorage,
        embedding_provider: Optional[EmbeddingProvider] = None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.embedding_provider = embedding_provider
        self.config = config or {}

    async def recall(
        self,
        bank_id: str,
        query: str,
        max_tokens: int = 4096,
        budget: str = "mid",
        types: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> dict:
        from .utils import sanitize_text

        query = sanitize_text(query) or ""
        if not query:
            return {"results": [], "trace": {}}

        overfetch = BUDGET_OVERFETCH.get(budget, 300)
        fact_types = types or ["world", "experience"]

        semantic_results = await self._semantic_search(bank_id, query, overfetch, fact_types, tags)

        keyword_results = self._keyword_search(bank_id, query, overfetch, fact_types, tags)

        graph_results = self._graph_search(
            bank_id, semantic_results[:10] + keyword_results[:10], overfetch
        )

        temporal_results = self._temporal_search(bank_id, query, overfetch, fact_types, tags)

        reranking = self.config.get("reranking", "rrf")
        if reranking == "interleave":
            fused = self._interleave_fuse(
                [semantic_results, keyword_results, graph_results, temporal_results],
                bank_id,
            )
        else:
            fused = self._rrf_fuse(
                [semantic_results, keyword_results, graph_results, temporal_results],
                bank_id,
            )

        observations = self._get_observations(bank_id, tags, query)

        boosted = self._apply_boosts(fused, bank_id)

        truncated, truncated_flag = self._token_truncate(boosted, max_tokens)

        all_results = observations + truncated

        return {
            "results": all_results,
            "trace": {
                "semantic_count": len(semantic_results),
                "keyword_count": len(keyword_results),
                "graph_count": len(graph_results),
                "temporal_count": len(temporal_results),
                "fused_count": len(fused),
                "final_count": len(all_results),
            },
            "source_facts_truncated": truncated_flag,
        }

    async def _semantic_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        if not self.embedding_provider or not self.embedding_provider.is_available():
            return []

        query_vec = await self.embedding_provider.embed(query)
        if not query_vec:
            return []

        embeddings = self.storage.get_fact_embeddings(bank_id, limit=limit * 2)
        if not embeddings:
            return []

        scored: list[tuple[float, str]] = []
        for fact_id, emb_blob in embeddings:
            vec = unpack_embedding(emb_blob)
            sim = cosine_similarity(query_vec, vec)
            if sim > 0.1:
                scored.append((sim, fact_id))

        scored.sort(reverse=True)
        top_ids = [fid for _, fid in scored[:limit]]

        results = []
        for fid in top_ids:
            fact = self.storage.get_fact(fid)
            if fact and fact["fact_type"] in fact_types:
                if tags and not _tags_match(fact, tags):
                    continue
                fact["scores"] = {"semantic": scored[top_ids.index(fid)][0] if fid in top_ids else None,
                                  "keyword": None, "final": 0.0, "reranker": None}
                results.append(fact)

        return results

    def _keyword_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        try:
            rows = self.storage.fts_search(query, limit=limit)
        except Exception as e:
            logger.warning(f"FTS5 search failed: {e}")
            return []

        results = []
        for row in rows:
            if row.get("fact_type") in fact_types:
                if tags and not _tags_match(row, tags):
                    continue
                row["scores"] = {
                    "semantic": None,
                    "keyword": row.get("bm25_score", 0),
                    "final": 0.0,
                    "reranker": None,
                }
                results.append(row)
        return results

    def _graph_search(
        self,
        bank_id: str,
        seed_facts: list[dict],
        limit: int,
    ) -> list[dict]:
        if not seed_facts:
            return []

        results: list[dict] = []
        seen_ids: set[str] = set()

        for seed in seed_facts[:10]:
            seed_id = seed.get("id")
            if not seed_id or seed_id in seen_ids:
                continue
            seen_ids.add(seed_id)

            linked = self.storage.get_linked_facts(seed_id, as_source=True)
            linked += self.storage.get_linked_facts(seed_id, as_source=False)

            for lf in linked[:5]:
                lid = lf.get("id")
                if lid and lid not in seen_ids:
                    seen_ids.add(lid)
                    lf["scores"] = {
                        "semantic": None,
                        "keyword": None,
                        "final": 0.0,
                        "reranker": None,
                    }
                    results.append(lf)

            if len(results) >= limit:
                break

        return results[:limit]

    def _temporal_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        time_window = _extract_time_window(query)
        if not time_window:
            return []

        start, end = time_window
        conn = self.storage._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM memory_facts
            WHERE bank_id = ? AND fact_type IN (?, ?)
              AND mentioned_at IS NOT NULL
              AND mentioned_at >= ? AND mentioned_at <= ?
            ORDER BY mentioned_at DESC
            LIMIT ?
            """,
            (bank_id, fact_types[0] if len(fact_types) > 0 else "world",
             fact_types[1] if len(fact_types) > 1 else "experience",
             start, end, limit),
        ).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            if tags and not _tags_match(r, tags):
                continue
            r["scores"] = {
                "semantic": None, "keyword": None,
                "final": 0.0, "reranker": None,
            }
            results.append(r)
        return results

    def _rrf_fuse(self, strategy_results: list[list[dict]], bank_id: str) -> list[dict]:
        rank_maps: list[dict[str, int]] = []
        for results in strategy_results:
            rank_map = {}
            for rank, item in enumerate(results):
                fid = item.get("id")
                if fid:
                    rank_map[fid] = rank
            rank_maps.append(rank_map)

        all_ids: set[str] = set()
        for rm in rank_maps:
            all_ids.update(rm.keys())

        scores: dict[str, float] = {}
        for fid in all_ids:
            score = 0.0
            for rm in rank_maps:
                if fid in rm:
                    score += 1.0 / (RRF_K + rm[fid])
            scores[fid] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for fid, score in ranked:
            fact = self.storage.get_fact(fid)
            if fact:
                fact.setdefault("scores", {})
                fact["scores"]["final"] = score
                fact["scores"]["reranker"] = None
                results.append(fact)

        return results

    def _interleave_fuse(
        self, strategy_results: list[list[dict]], bank_id: str
    ) -> list[dict]:
        seen: set[str] = set()
        results: list[dict] = []
        max_len = max(len(r) for r in strategy_results) if strategy_results else 0
        for pos in range(max_len):
            for strategy_idx, sresults in enumerate(strategy_results):
                if pos < len(sresults):
                    item = sresults[pos]
                    fid = item.get("id")
                    if fid and fid not in seen:
                        seen.add(fid)
                        fact = self.storage.get_fact(fid)
                        if fact:
                            score = 1.0 / (pos + 1) * (1.0 - strategy_idx * 0.01)
                            fact.setdefault("scores", {})
                            fact["scores"]["final"] = score
                            fact["scores"]["reranker"] = "interleave"
                            results.append(fact)
        return results

    def _apply_boosts(self, facts: list[dict], bank_id: str) -> list[dict]:
        if not facts:
            return facts

        now = datetime.now(timezone.utc)
        for f in facts:
            score = f.get("scores", {}).get("final", 0.0)
            mentioned = f.get("mentioned_at")
            if mentioned:
                try:
                    dt = datetime.fromisoformat(mentioned.replace("Z", "+00:00"))
                    days_ago = (now - dt).days
                    recency_signal = max(0.1, min(1.0, 1.0 - days_ago / 365.0))
                    score *= 1 + 0.2 * (recency_signal - 0.5)
                except (ValueError, TypeError):
                    pass

            f["scores"]["final"] = score

        facts.sort(key=lambda x: x.get("scores", {}).get("final", 0.0), reverse=True)
        return facts

    def _get_observations(self, bank_id: str, tags: Optional[list[str]], query: str) -> list[dict]:
        observations = self.storage.list_observations(bank_id, tags)
        results = []
        for obs in observations:
            obs["observation_id"] = obs["id"]
            obs["scores"] = {"final": 0.5, "reranker": None,
                             "semantic": None, "keyword": None}
            results.append(obs)
        return results

    def _token_truncate(self, facts: list[dict], max_tokens: int) -> tuple[list[dict], bool]:
        total = 0
        truncated = False
        results = []
        for f in facts:
            text = f.get("fact_text", "") or f.get("text", "")
            t = count_tokens(text)
            if total + t > max_tokens:
                truncated = True
                continue
            total += t
            results.append(f)
        return results, truncated

def _tags_match(fact: dict, tags: str | list[str]) -> bool:
    import json

    fact_tags = fact.get("tags", "[]")
    if isinstance(fact_tags, str):
        try:
            fact_tags = json.loads(fact_tags)
        except (json.JSONDecodeError, TypeError):
            fact_tags = []
    fact_set = set(fact_tags) if fact_tags else set()

    if isinstance(tags, str):
        mode = tags
        return True
    elif isinstance(tags, list):
        mode = "any"
        req_set = set(tags)
    else:
        return True

    if not req_set:
        return True
    if not fact_set:
        return False

    if mode == "any":
        return bool(fact_set & req_set)
    elif mode == "all":
        return req_set.issubset(fact_set)
    elif mode == "any_strict":
        return bool(fact_set & req_set) and fact_set.issubset(req_set)
    elif mode in ("all_strict", "exact"):
        return fact_set == req_set
    else:
        return bool(fact_set & req_set)

_TIME_PATTERNS = [
    (re.compile(r"去年|last\s+year", re.I), 365),
    (re.compile(r"今年|this\s+year", re.I), 0),
    (re.compile(r"前年", re.I), 730),
    (re.compile(r"上个月|last\s+month", re.I), 30),
    (re.compile(r"这个月|this\s+month", re.I), 0),
    (re.compile(r"上周|last\s+week", re.I), 7),
    (re.compile(r"这周|this\s+week", re.I), 0),
    (re.compile(r"昨天|yesterday", re.I), 1),
    (re.compile(r"今天|today", re.I), 0),
    (re.compile(r"前天", re.I), 2),
    (re.compile(r"(\d{4})\s*年"), None),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?"), None),
]

def _extract_time_window(query: str) -> Optional[tuple[str, str]]:
    if not query:
        return None

    now = datetime.now(timezone.utc)

    year_match = re.search(r"(\d{4})\s*年", query)
    if year_match:
        year = int(year_match.group(1))
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        return start.isoformat(), end.isoformat()

    md_match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", query)
    if md_match:
        month = int(md_match.group(1))
        day = int(md_match.group(2))
        try:
            start = datetime(now.year, month, day, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            return start.isoformat(), end.isoformat()
        except ValueError:
            pass

    for pattern, days_offset in _TIME_PATTERNS:
        if days_offset is not None and pattern.search(query):
            start = now - timedelta(days=days_offset)
            end = now + timedelta(days=1)
            return start.isoformat(), end.isoformat()

    return None
