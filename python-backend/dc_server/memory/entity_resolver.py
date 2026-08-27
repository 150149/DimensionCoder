"""
memory/entity_resolver.py — EntityResolver：纯 Python trigram 实体解析

源自 Hindsight 的 entity_resolver.py
- 三种查找策略（DimensionCoder 用 "full" 即可，数据量小）
- 实体评分（0-1，阈值 0.6）：名称相似度 0.5 + 共现实体 0.3 + 时间邻近 0.2
- 批内去重：O(N²) trigram 比较，≥0.5 union-find 聚类
- Trigram 计算与 PostgreSQL pg_trgm 逐字节一致
"""

from __future__ import annotations

import difflib
import logging
import math
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────────

_MERGE_MIN_SIMILARITY = 0.3  # trigram 相似度低于此值直接跳过
_INTRABATCH_MERGE_SIMILARITY = 0.5  # 批内去重阈值
_IDENTICAL_TRIGRAMS = 1.0  # 相同 trigram 集直接评 1.0
_MAX_UNIQUE_NAMES = 250  # 批内去重上限

# ── Trigram 计算（与 PostgreSQL pg_trgm 逐字节一致）────────

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _trigram_set(text: str) -> frozenset[str]:
    """计算文本的 trigram 集合。

    小写化 → 按 word 拆分（[^\W_]+ 正则，Unicode 兼容）→
    每个 word 前补 2 个空格后补 1 → 取所有 3 字符窗口。
    """
    if not text:
        return frozenset()
    lower = text.lower()
    words = _WORD_RE.findall(lower)
    trigrams: set[str] = set()
    for word in words:
        padded = f"  {word} "
        for i in range(len(padded) - 2):
            trigrams.add(padded[i : i + 3])
    return frozenset(trigrams)


def _trigram_set_similarity(ta: frozenset[str], tb: frozenset[str]) -> float:
    """Jaccard 指数 = 交集 / 并集。"""
    if not ta and not tb:
        return 0.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


# ── Token 兼容性检查 ──────────────────────────────────────


def _tokens_are_compatible(name1: str, name2: str) -> bool:
    """逐词检查，防止一个长共同词淹没完全不同的短词。

    "John Smith" vs "Jane Smith" → Smith 共同但 John≠Jane → 拒绝
    """
    tokens1 = set(_WORD_RE.findall(name1.lower()))
    tokens2 = set(_WORD_RE.findall(name2.lower()))
    if not tokens1 or not tokens2:
        return True
    # 所有 tokens 都不相同 → 不兼容
    if tokens1.isdisjoint(tokens2):
        return False
    # 有共同 token，但检查是否有不匹配的短 token
    for t1 in tokens1:
        for t2 in tokens2:
            if t1 != t2 and len(t1) <= 3 and len(t2) <= 3:
                # 都是短 token 且不同 → 不兼容
                if t1 not in tokens2 and t2 not in tokens1:
                    return False
    return True


# ── Union-Find（批内去重用）────────────────────────────────


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


# ── EntityResolver ────────────────────────────────────────


class EntityResolver:
    """实体解析器（纯 Python，无 DB 依赖）。

    使用方法：
        resolver = EntityResolver(storage)
        resolved = await resolver.resolve_entities_batch(bank_id, ["Alice", "Bob", "alice"])
        # resolved = {"Alice": entity_id_1, "Bob": entity_id_2, "alice": entity_id_1}
    """

    def __init__(self, storage):
        self.storage = storage

    async def resolve_entities_batch(
        self,
        bank_id: str,
        entity_names: list[str],
        label_entities: Optional[set[str]] = None,
    ) -> dict[str, str]:
        """批量解析实体名 → entity_id。

        1. 批内去重（trigram ≥0.5 union-find 聚类）
        2. 对每个 canonical name，查找或创建 entity
        3. label entities 只做精确匹配

        返回 {original_name: entity_id}
        """
        if not entity_names:
            return {}

        label_set = label_entities or set()
        result: dict[str, str] = {}

        # 分离 label 和 regular entities
        regular_names = [n for n in entity_names if n not in label_set]

        # 批内去重
        canonical_map = self._intrabatch_dedup(regular_names)

        # 解析每个 canonical name
        canonical_to_id: dict[str, str] = {}
        for canonical_name in set(canonical_map.values()):
            entity_id = self.storage.find_or_create_entity(
                bank_id, canonical_name, entity_kind="regular"
            )
            if entity_id:
                canonical_to_id[canonical_name] = entity_id

        # 映射回原始名
        for original_name in regular_names:
            canonical = canonical_map.get(original_name, original_name)
            entity_id = canonical_to_id.get(canonical)
            if entity_id:
                result[original_name] = entity_id

        # Label entities: 精确匹配，不参与合并
        for name in label_set:
            if name in entity_names:
                entity_id = self.storage.find_or_create_entity(
                    bank_id, name, entity_kind="label"
                )
                if entity_id:
                    result[name] = entity_id

        return result

    def _intrabatch_dedup(self, names: list[str]) -> dict[str, str]:
        """批内去重。返回 {original_name: canonical_name}。"""
        if len(names) > _MAX_UNIQUE_NAMES:
            logger.warning(
                f"Entity batch too large ({len(names)} > {_MAX_UNIQUE_NAMES}), "
                "skipping intrabatch dedup"
            )
            return {n: n for n in names}

        unique_names = list(set(names))
        n = len(unique_names)
        if n <= 1:
            return {name: name for name in names}

        # 预计算 trigram 集
        trigrams = [_trigram_set(name) for name in unique_names]
        name_to_idx = {name: i for i, name in enumerate(unique_names)}

        # O(N²) trigram 比较
        uf = _UnionFind(n)
        for i in range(n):
            for j in range(i + 1, n):
                if uf.find(i) == uf.find(j):
                    continue
                sim = _trigram_set_similarity(trigrams[i], trigrams[j])
                if sim >= _INTRABATCH_MERGE_SIMILARITY:
                    if _tokens_are_compatible(unique_names[i], unique_names[j]):
                        uf.union(i, j)

        # 每个簇选 canonical name：最高频 > 最短 > 字典序最小
        clusters: dict[int, list[str]] = {}
        for i, name in enumerate(unique_names):
            root = uf.find(i)
            clusters.setdefault(root, []).append(name)

        cluster_canonical: dict[int, str] = {}
        for root, members in clusters.items():
            # 最高频
            freq = {m: names.count(m) for m in members}
            max_freq = max(freq.values())
            candidates = [m for m in members if freq[m] == max_freq]
            if len(candidates) == 1:
                cluster_canonical[root] = candidates[0]
            else:
                # 最短 > 字典序最小
                candidates.sort(key=lambda x: (len(x), x))
                cluster_canonical[root] = candidates[0]

        # 映射原始名 → canonical
        result = {}
        for name in names:
            idx = name_to_idx[name]
            root = uf.find(idx)
            result[name] = cluster_canonical[root]

        return result

    def score_entity_match(
        self,
        candidate_name: str,
        canonical_name: str,
        bank_id: str,
        candidate_entity_id: str,
        time_diff_days: float = 0.0,
    ) -> float:
        """实体匹配评分（0-1，阈值 0.6）。

        信号1 名称相似度(0-0.5): SequenceMatcher.ratio()
        信号2 共现实体(0-0.3): 加权 1.0/sqrt(max(degree,1))
        信号3 时间邻近(0-0.2): max(0, 1.0-days_diff/7)
        """
        # 信号 1: 名称相似度
        ta = _trigram_set(candidate_name)
        tb = _trigram_set(canonical_name)
        if ta == tb:
            name_score = _IDENTICAL_TRIGRAMS
        else:
            trigram_sim = _trigram_set_similarity(ta, tb)
            if trigram_sim < _MERGE_MIN_SIMILARITY:
                return 0.0  # 低于门槛直接跳过
            if not _tokens_are_compatible(candidate_name, canonical_name):
                return 0.0
            seq_ratio = difflib.SequenceMatcher(
                None, candidate_name.lower(), canonical_name.lower()
            ).ratio()
            name_score = max(trigram_sim, seq_ratio)

        name_weight = 0.5 * name_score

        # 信号 2: 共现实体
        cooccurrences = self.storage.get_cooccurrences(bank_id, candidate_entity_id)
        degree = len(cooccurrences)
        cooccur_weight = 0.0
        if cooccurrences:
            # 检查 canonical_name 的实体是否在共现中
            # 这里简化：用 degree 的反比作为 hub 抑制
            cooccur_weight = 0.3 * (1.0 / math.sqrt(max(degree, 1)))

        # 信号 3: 时间邻近
        time_weight = 0.2 * max(0.0, 1.0 - time_diff_days / 7.0)

        return name_weight + cooccur_weight + time_weight
