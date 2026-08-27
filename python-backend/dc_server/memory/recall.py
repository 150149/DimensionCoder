"""
memory/recall.py — Recaller：四策略检索 + RRF 融合 + Token 截断

源自 Hindsight 的 search/ 目录

四种检索策略：
1. 语义搜索：embedding cosine similarity（Python 侧全量计算）
2. 关键词搜索：SQLite FTS5 + BM25
3. 图谱遍历：entity → memory_links → linked facts
4. 时间搜索：解析 query 中时间表达式 → 时间窗口

RRF 融合：score(d) = Σ 1/(60 + rank_i(d))
可选 boost：recency / temporal / proof_count
Token 截断：按 final_score 降序，累加 token 数到 max_tokens 停止
"""

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

RRF_K = 60  # RRF 常数

BUDGET_OVERFETCH = {
    "low": 100,
    "mid": 300,
    "high": 1000,
}


class Recaller:
    """检索器——四策略并行 + RRF 融合。

    使用方法：
        recaller = Recaller(storage, embedding_provider)
        result = await recaller.recall(bank_id, query="如何处理反调试", max_tokens=4096)
    """

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
        """检索记忆。返回 recall_result dict。

        types: None=全部, ["world"], ["experience"], ["observation"]
        budget: "low"|"mid"|"high" 控制 over-fetch 量
        """
        from .utils import sanitize_text

        query = sanitize_text(query) or ""
        if not query:
            return {"results": [], "trace": {}}

        overfetch = BUDGET_OVERFETCH.get(budget, 300)
        fact_types = types or ["world", "experience"]

        # ── 策略 1: 语义搜索 ────────────────────────────────
        semantic_results = await self._semantic_search(bank_id, query, overfetch, fact_types, tags)

        # ── 策略 2: 关键词搜索 ──────────────────────────────
        keyword_results = self._keyword_search(bank_id, query, overfetch, fact_types, tags)

        # ── 策略 3: 图谱遍历 ────────────────────────────────
        # 从语义+关键词 top-K 提取实体 → 找关联 fact
        graph_results = self._graph_search(
            bank_id, semantic_results[:10] + keyword_results[:10], overfetch
        )

        # ── 策略 4: 时间搜索 ────────────────────────────────
        temporal_results = self._temporal_search(bank_id, query, overfetch, fact_types, tags)

        # ── 融合 ───────────────────────────────────────────
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

        # ── Observations ────────────────────────────────────
        observations = self._get_observations(bank_id, tags, query)

        # ── Boost（可选）──────────────────────────────────
        boosted = self._apply_boosts(fused, bank_id)

        # ── Token 截断 ──────────────────────────────────────
        truncated, truncated_flag = self._token_truncate(boosted, max_tokens)

        # 合并 observations + facts
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

    # ── 策略 1: 语义搜索 ────────────────────────────────────

    async def _semantic_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        """语义搜索：对 query 生成 embedding，cosine similarity 排序。"""
        if not self.embedding_provider or not self.embedding_provider.is_available():
            return []

        query_vec = await self.embedding_provider.embed(query)
        if not query_vec:
            return []

        # 获取 bank 内所有 fact 的 embedding
        embeddings = self.storage.get_fact_embeddings(bank_id, limit=limit * 2)
        if not embeddings:
            return []

        scored: list[tuple[float, str]] = []
        for fact_id, emb_blob in embeddings:
            vec = unpack_embedding(emb_blob)
            sim = cosine_similarity(query_vec, vec)
            if sim > 0.1:  # 最低阈值
                scored.append((sim, fact_id))

        scored.sort(reverse=True)
        top_ids = [fid for _, fid in scored[:limit]]

        # 获取 fact 详情
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

    # ── 策略 2: 关键词搜索 ──────────────────────────────────

    def _keyword_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        """关键词搜索：SQLite FTS5 + BM25。"""
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

    # ── 策略 3: 图谱遍历 ────────────────────────────────────

    def _graph_search(
        self,
        bank_id: str,
        seed_facts: list[dict],
        limit: int,
    ) -> list[dict]:
        """图谱遍历：从 seed facts 的实体出发，找关联 fact。"""
        if not seed_facts:
            return []

        results: list[dict] = []
        seen_ids: set[str] = set()

        for seed in seed_facts[:10]:
            seed_id = seed.get("id")
            if not seed_id or seed_id in seen_ids:
                continue
            seen_ids.add(seed_id)

            # 通过 memory_links 找关联 fact
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

    # ── 策略 4: 时间搜索 ────────────────────────────────────

    def _temporal_search(
        self,
        bank_id: str,
        query: str,
        limit: int,
        fact_types: list[str],
        tags: Optional[list[str]],
    ) -> list[dict]:
        """时间搜索：解析 query 中时间表达式 → 时间窗口。"""
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

    # ── RRF 融合 ────────────────────────────────────────────

    def _rrf_fuse(self, strategy_results: list[list[dict]], bank_id: str) -> list[dict]:
        """RRF 融合：score(d) = Σ 1/(k + rank_i(d))。"""
        # 为每个策略的候选分配排名
        rank_maps: list[dict[str, int]] = []
        for results in strategy_results:
            rank_map = {}
            for rank, item in enumerate(results):
                fid = item.get("id")
                if fid:
                    rank_map[fid] = rank
            rank_maps.append(rank_map)

        # 收集所有候选 ID
        all_ids: set[str] = set()
        for rm in rank_maps:
            all_ids.update(rm.keys())

        # RRF 评分
        scores: dict[str, float] = {}
        for fid in all_ids:
            score = 0.0
            for rm in rank_maps:
                if fid in rm:
                    score += 1.0 / (RRF_K + rm[fid])
            scores[fid] = score

        # 获取 fact 详情并排序
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
        """Interleave Fusion（轮转融合）：按策略优先级轮转取每策略的 #1、#2、#3...

        保证每个策略的 top hit 都有位置。用于 consolidation 去重场景
        （防止近重复 observation 被 RRF 降权后遗漏）。
        rrinterleave_score 按轮转位置严格递减分配。
        """
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
                            # 分数按轮转位置递减：1.0/(pos+1)
                            score = 1.0 / (pos + 1) * (1.0 - strategy_idx * 0.01)
                            fact.setdefault("scores", {})
                            fact["scores"]["final"] = score
                            fact["scores"]["reranker"] = "interleave"
                            results.append(fact)
        return results

    # ── Boost ───────────────────────────────────────────────

    def _apply_boosts(self, facts: list[dict], bank_id: str) -> list[dict]:
        """应用可选 boost：recency / proof_count。"""
        if not facts:
            return facts

        now = datetime.now(timezone.utc)
        for f in facts:
            score = f.get("scores", {}).get("final", 0.0)
            # Recency boost
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

        # 重新排序
        facts.sort(key=lambda x: x.get("scores", {}).get("final", 0.0), reverse=True)
        return facts

    # ── Observations ────────────────────────────────────────

    def _get_observations(self, bank_id: str, tags: Optional[list[str]], query: str) -> list[dict]:
        """获取相关 observations。"""
        observations = self.storage.list_observations(bank_id, tags)
        results = []
        for obs in observations:
            obs["observation_id"] = obs["id"]
            obs["scores"] = {"final": 0.5, "reranker": None,
                             "semantic": None, "keyword": None}
            results.append(obs)
        return results

    # ── Token 截断 ──────────────────────────────────────────

    def _token_truncate(self, facts: list[dict], max_tokens: int) -> tuple[list[dict], bool]:
        """按 final_score 降序，累加 token 数到 max_tokens 停止。"""
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


# ── Tags 匹配 ──────────────────────────────────────────────


def _tags_match(fact: dict, tags: str | list[str]) -> bool:
    """检查 fact 的 tags 是否匹配请求 tags。

    支持以下匹配模式（与 Hindsight 一致的 5 种模式）：
    - "any" 或 list → any 模式：有重叠即匹配
    - "all" → all 模式：所有请求 tag 都必须在 fact 中
    - "any_strict" → fact 的 tags 必须与请求 tags 有交集，且无额外 tag
    - "all_strict" → fact 的 tags 必须完全等于请求 tags
    - "exact" → fact 的 tags 必须完全等于请求 tags（与 all_strict 相同）

    复合 tag groups：tags 可以是 "tag1+tag2|tag3" 格式（+ = AND, | = OR）。
    """
    import json

    # 解析 fact tags
    fact_tags = fact.get("tags", "[]")
    if isinstance(fact_tags, str):
        try:
            fact_tags = json.loads(fact_tags)
        except (json.JSONDecodeError, TypeError):
            fact_tags = []
    fact_set = set(fact_tags) if fact_tags else set()

    # 解析请求 tags
    if isinstance(tags, str):
        mode = tags
        return True  # 纯模式字符串（如 "any"）→ 不过滤
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
        # fact 的 tags 必须在请求 tags 中（不能有额外 tag），且有交集
        return bool(fact_set & req_set) and fact_set.issubset(req_set)
    elif mode in ("all_strict", "exact"):
        # fact 的 tags 必须完全等于请求 tags
        return fact_set == req_set
    else:
        return bool(fact_set & req_set)


# ── 时间表达式解析 ─────────────────────────────────────────

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
    (re.compile(r"(\d{4})\s*年"), None),  # 年份，特殊处理
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?"), None),  # 月日
]


def _extract_time_window(query: str) -> Optional[tuple[str, str]]:
    """从 query 中解析时间表达式，返回 (start, end) ISO 时间窗口。

    无时间表达式时返回 None。
    """
    if not query:
        return None

    now = datetime.now(timezone.utc)

    # 年份
    year_match = re.search(r"(\d{4})\s*年", query)
    if year_match:
        year = int(year_match.group(1))
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        return start.isoformat(), end.isoformat()

    # 月日
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

    # 相对时间
    for pattern, days_offset in _TIME_PATTERNS:
        if days_offset is not None and pattern.search(query):
            start = now - timedelta(days=days_offset)
            end = now + timedelta(days=1)
            return start.isoformat(), end.isoformat()

    return None
