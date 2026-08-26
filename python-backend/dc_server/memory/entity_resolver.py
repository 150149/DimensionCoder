
from __future__ import annotations

import difflib
import logging
import math
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_MERGE_MIN_SIMILARITY = 0.3
_INTRABATCH_MERGE_SIMILARITY = 0.5
_IDENTICAL_TRIGRAMS = 1.0
_MAX_UNIQUE_NAMES = 250

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

def _trigram_set(text: str) -> frozenset[str]:
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
    if not ta and not tb:
        return 0.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)

def _tokens_are_compatible(name1: str, name2: str) -> bool:
    tokens1 = set(_WORD_RE.findall(name1.lower()))
    tokens2 = set(_WORD_RE.findall(name2.lower()))
    if not tokens1 or not tokens2:
        return True
    if tokens1.isdisjoint(tokens2):
        return False
    for t1 in tokens1:
        for t2 in tokens2:
            if t1 != t2 and len(t1) <= 3 and len(t2) <= 3:
                if t1 not in tokens2 and t2 not in tokens1:
                    return False
    return True

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

class EntityResolver:

    def __init__(self, storage):
        self.storage = storage

    async def resolve_entities_batch(
        self,
        bank_id: str,
        entity_names: list[str],
        label_entities: Optional[set[str]] = None,
    ) -> dict[str, str]:
        if not entity_names:
            return {}

        label_set = label_entities or set()
        result: dict[str, str] = {}

        regular_names = [n for n in entity_names if n not in label_set]

        canonical_map = self._intrabatch_dedup(regular_names)

        canonical_to_id: dict[str, str] = {}
        for canonical_name in set(canonical_map.values()):
            entity_id = self.storage.find_or_create_entity(
                bank_id, canonical_name, entity_kind="regular"
            )
            if entity_id:
                canonical_to_id[canonical_name] = entity_id

        for original_name in regular_names:
            canonical = canonical_map.get(original_name, original_name)
            entity_id = canonical_to_id.get(canonical)
            if entity_id:
                result[original_name] = entity_id

        for name in label_set:
            if name in entity_names:
                entity_id = self.storage.find_or_create_entity(
                    bank_id, name, entity_kind="label"
                )
                if entity_id:
                    result[name] = entity_id

        return result

    def _intrabatch_dedup(self, names: list[str]) -> dict[str, str]:
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

        trigrams = [_trigram_set(name) for name in unique_names]
        name_to_idx = {name: i for i, name in enumerate(unique_names)}

        uf = _UnionFind(n)
        for i in range(n):
            for j in range(i + 1, n):
                if uf.find(i) == uf.find(j):
                    continue
                sim = _trigram_set_similarity(trigrams[i], trigrams[j])
                if sim >= _INTRABATCH_MERGE_SIMILARITY:
                    if _tokens_are_compatible(unique_names[i], unique_names[j]):
                        uf.union(i, j)

        clusters: dict[int, list[str]] = {}
        for i, name in enumerate(unique_names):
            root = uf.find(i)
            clusters.setdefault(root, []).append(name)

        cluster_canonical: dict[int, str] = {}
        for root, members in clusters.items():
            freq = {m: names.count(m) for m in members}
            max_freq = max(freq.values())
            candidates = [m for m in members if freq[m] == max_freq]
            if len(candidates) == 1:
                cluster_canonical[root] = candidates[0]
            else:
                candidates.sort(key=lambda x: (len(x), x))
                cluster_canonical[root] = candidates[0]

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
        ta = _trigram_set(candidate_name)
        tb = _trigram_set(canonical_name)
        if ta == tb:
            name_score = _IDENTICAL_TRIGRAMS
        else:
            trigram_sim = _trigram_set_similarity(ta, tb)
            if trigram_sim < _MERGE_MIN_SIMILARITY:
                return 0.0
            if not _tokens_are_compatible(candidate_name, canonical_name):
                return 0.0
            seq_ratio = difflib.SequenceMatcher(
                None, candidate_name.lower(), canonical_name.lower()
            ).ratio()
            name_score = max(trigram_sim, seq_ratio)

        name_weight = 0.5 * name_score

        cooccurrences = self.storage.get_cooccurrences(bank_id, candidate_entity_id)
        degree = len(cooccurrences)
        cooccur_weight = 0.0
        if cooccurrences:
            cooccur_weight = 0.3 * (1.0 / math.sqrt(max(degree, 1)))

        time_weight = 0.2 * max(0.0, 1.0 - time_diff_days / 7.0)

        return name_weight + cooccur_weight + time_weight
