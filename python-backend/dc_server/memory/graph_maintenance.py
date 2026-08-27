"""
memory/graph_maintenance.py — 删除时 relink top-up + entity prune

源自 Hindsight 的 graph_maintenance.py（简化版）

删除 fact 时在**同一事务内**执行：
1. Relink top-up: 找出出边指向被删 fact 的幸存 fact，补插缺失链接
2. Entity prune: 找出被删 fact 引用的实体，删除无引用的孤立实体

小规模可直接内联执行，不需要队列表。
"""

from __future__ import annotations

import logging
from typing import Optional

from .storage import MemoryStorage, MAX_TEMPORAL_LINKS_PER_UNIT, MAX_SEMANTIC_LINKS_PER_UNIT

logger = logging.getLogger(__name__)


def run_graph_maintenance(
    storage: MemoryStorage,
    bank_id: str,
    deleted_fact_ids: list[str],
) -> dict:
    """删除 fact 后的图谱维护。

    返回 {
        relink_units_processed: int,
        relink_links_added: int,
        entities_examined: int,
        orphan_entities_pruned: int,
    }
    """
    result = {
        "relink_units_processed": 0,
        "relink_links_added": 0,
        "entities_examined": 0,
        "orphan_entities_pruned": 0,
    }

    # 1. Relink top-up: 找出出边/入边指向被删 fact 的幸存 fact
    victim_ids = _find_relink_victims(storage, deleted_fact_ids)
    for victim_id in victim_ids:
        added = _relink_victim(storage, victim_id)
        result["relink_links_added"] += added
        result["relink_units_processed"] += 1

    # 2. Entity prune: 找出被删 fact 引用的实体
    candidate_entities = _find_entity_prune_candidates(storage, deleted_fact_ids)
    result["entities_examined"] = len(candidate_entities)
    for entity_id in candidate_entities:
        if _is_orphan_entity(storage, entity_id):
            _prune_entity(storage, entity_id)
            result["orphan_entities_pruned"] += 1

    return result


def _find_relink_victims(storage: MemoryStorage, deleted_ids: list[str]) -> list[str]:
    """找出出边/入边指向被删 fact 的幸存 fact。

    被删 fact 的链接行会被 ON DELETE CASCADE 自动删除，
    但我们需要找到"曾经"指向它们的幸存 fact，补插新链接。
    """
    # 注意：此时被删 fact 的链接行已经 CASCADE 删除
    # 我们需要通过 memory_fact_entities 找到与被删 fact 共享实体的幸存 fact
    victims: set[str] = set()
    conn = storage._get_conn()

    for fact_id in deleted_ids:
        # 找到被删 fact 引用的实体
        entity_rows = conn.execute(
            "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?",
            (fact_id,),
        ).fetchall()
        for er in entity_rows:
            # 找到引用同一实体的其他 fact
            linked_facts = conn.execute(
                "SELECT fact_id FROM memory_fact_entities WHERE entity_id = ? AND fact_id != ?",
                (er["entity_id"], fact_id),
            ).fetchall()
            for lf in linked_facts:
                # 确保这个 fact 还存在（未被删除）
                exists = conn.execute(
                    "SELECT 1 FROM memory_facts WHERE id = ?", (lf["fact_id"],)
                ).fetchone()
                if exists:
                    victims.add(lf["fact_id"])

    return list(victims)


def _relink_victim(storage: MemoryStorage, victim_id: str) -> int:
    """为 victim fact 补插缺失的链接。

    检查当前每类出边数，低于上限则通过实体共现找候选并补插。
    """
    conn = storage._get_conn()
    links_added = 0

    # 检查当前 temporal 出边数
    temporal_count = storage.count_links(victim_id, "temporal", as_source=True)
    if temporal_count < MAX_TEMPORAL_LINKS_PER_UNIT:
        # 通过共现实体找候选
        entity_rows = conn.execute(
            "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?", (victim_id,)
        ).fetchall()
        for er in entity_rows:
            # 找引用同一实体的其他 fact
            candidates = conn.execute(
                """
                SELECT DISTINCT f.id, f.mentioned_at
                FROM memory_fact_entities fe
                JOIN memory_facts f ON f.id = fe.fact_id
                WHERE fe.entity_id = ? AND f.id != ?
                LIMIT ?
                """,
                (er["entity_id"], victim_id, MAX_TEMPORAL_LINKS_PER_UNIT),
            ).fetchall()
            for c in candidates:
                if temporal_count >= MAX_TEMPORAL_LINKS_PER_UNIT:
                    break
                # 检查链接是否已存在
                exists = conn.execute(
                    "SELECT 1 FROM memory_links WHERE source_fact_id = ? AND target_fact_id = ? AND link_type = 'temporal'",
                    (victim_id, c["id"]),
                ).fetchone()
                if not exists:
                    storage.insert_link(victim_id, c["id"], "temporal", 0.5)
                    links_added += 1
                    temporal_count += 1

    return links_added


def _find_entity_prune_candidates(storage: MemoryStorage, deleted_ids: list[str]) -> list[str]:
    """找出被删 fact 引用的实体。"""
    conn = storage._get_conn()
    candidates: set[str] = set()
    for fact_id in deleted_ids:
        rows = conn.execute(
            "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?", (fact_id,)
        ).fetchall()
        for r in rows:
            candidates.add(r["entity_id"])
    return list(candidates)


def _is_orphan_entity(storage: MemoryStorage, entity_id: str) -> bool:
    """检查实体是否无引用（所有引用它的 fact 都已删除）。"""
    conn = storage._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM memory_fact_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    # CASCADE 已经删除了被删 fact 的 fact_entities 行
    # 所以如果 count=0，说明没有其他 fact 引用这个实体
    return row["c"] == 0 if row else True


def _prune_entity(storage: MemoryStorage, entity_id: str):
    """删除孤立实体及其共现行（FK CASCADE 带走 cooccurrence）。"""
    conn = storage._get_conn()
    # 删除实体（FK CASCADE 会自动删除 cooccurrence 行）
    conn.execute("DELETE FROM memory_entities WHERE id = ?", (entity_id,))
    # 手动删除涉及此实体的 cooccurrence（SQLite 的 FK CASCADE 可能不覆盖联合主键表）
    conn.execute(
        "DELETE FROM memory_entity_cooccurrences WHERE entity_id_1 = ? OR entity_id_2 = ?",
        (entity_id, entity_id),
    )
    conn.commit()
