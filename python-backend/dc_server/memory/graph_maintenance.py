
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
    result = {
        "relink_units_processed": 0,
        "relink_links_added": 0,
        "entities_examined": 0,
        "orphan_entities_pruned": 0,
    }

    victim_ids = _find_relink_victims(storage, deleted_fact_ids)
    for victim_id in victim_ids:
        added = _relink_victim(storage, victim_id)
        result["relink_links_added"] += added
        result["relink_units_processed"] += 1

    candidate_entities = _find_entity_prune_candidates(storage, deleted_fact_ids)
    result["entities_examined"] = len(candidate_entities)
    for entity_id in candidate_entities:
        if _is_orphan_entity(storage, entity_id):
            _prune_entity(storage, entity_id)
            result["orphan_entities_pruned"] += 1

    return result

def _find_relink_victims(storage: MemoryStorage, deleted_ids: list[str]) -> list[str]:
    victims: set[str] = set()
    conn = storage._get_conn()

    for fact_id in deleted_ids:
        entity_rows = conn.execute(
            "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?",
            (fact_id,),
        ).fetchall()
        for er in entity_rows:
            linked_facts = conn.execute(
                "SELECT fact_id FROM memory_fact_entities WHERE entity_id = ? AND fact_id != ?",
                (er["entity_id"], fact_id),
            ).fetchall()
            for lf in linked_facts:
                exists = conn.execute(
                    "SELECT 1 FROM memory_facts WHERE id = ?", (lf["fact_id"],)
                ).fetchone()
                if exists:
                    victims.add(lf["fact_id"])

    return list(victims)

def _relink_victim(storage: MemoryStorage, victim_id: str) -> int:
    conn = storage._get_conn()
    links_added = 0

    temporal_count = storage.count_links(victim_id, "temporal", as_source=True)
    if temporal_count < MAX_TEMPORAL_LINKS_PER_UNIT:
        entity_rows = conn.execute(
            "SELECT entity_id FROM memory_fact_entities WHERE fact_id = ?", (victim_id,)
        ).fetchall()
        for er in entity_rows:
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
    conn = storage._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM memory_fact_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row["c"] == 0 if row else True

def _prune_entity(storage: MemoryStorage, entity_id: str):
    conn = storage._get_conn()
    conn.execute("DELETE FROM memory_entities WHERE id = ?", (entity_id,))
    conn.execute(
        "DELETE FROM memory_entity_cooccurrences WHERE entity_id_1 = ? OR entity_id_2 = ?",
        (entity_id, entity_id),
    )
    conn.commit()
