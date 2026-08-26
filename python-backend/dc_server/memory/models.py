
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _gen_id() -> str:
    return uuid4().hex

@dataclass
class MemoryBank:

    id: str
    name: str
    config: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "config": json.dumps(self.config, ensure_ascii=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryBank":
        cfg = d.get("config", "{}")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (json.JSONDecodeError, TypeError):
                cfg = {}
        return cls(
            id=d["id"],
            name=d["name"],
            config=cfg,
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

@dataclass
class MemoryFact:

    id: str = ""
    bank_id: str = ""
    fact_text: str = ""
    fact_type: str = "world"
    fact_kind: str = "conversation"
    context: str = ""
    occurred_start: Optional[str] = None
    occurred_end: Optional[str] = None
    mentioned_at: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    chunk_id: Optional[str] = None
    document_id: Optional[str] = None
    tags: list = field(default_factory=list)
    source_ref: dict = field(default_factory=dict)
    consolidated_at: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_id": self.bank_id,
            "fact_text": self.fact_text,
            "fact_type": self.fact_type,
            "fact_kind": self.fact_kind,
            "context": self.context,
            "occurred_start": self.occurred_start,
            "occurred_end": self.occurred_end,
            "mentioned_at": self.mentioned_at,
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "source_ref": json.dumps(self.source_ref, ensure_ascii=False),
            "consolidated_at": self.consolidated_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryFact":
        def _loads(v, default):
            if v is None:
                return default
            if isinstance(v, (dict, list)):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    return default
            return default

        return cls(
            id=d.get("id", ""),
            bank_id=d.get("bank_id", ""),
            fact_text=d.get("fact_text", ""),
            fact_type=d.get("fact_type", "world"),
            fact_kind=d.get("fact_kind", "conversation"),
            context=d.get("context", ""),
            occurred_start=d.get("occurred_start"),
            occurred_end=d.get("occurred_end"),
            mentioned_at=d.get("mentioned_at"),
            metadata=_loads(d.get("metadata"), {}),
            chunk_id=d.get("chunk_id"),
            document_id=d.get("document_id"),
            tags=_loads(d.get("tags"), []),
            source_ref=_loads(d.get("source_ref"), {}),
            consolidated_at=d.get("consolidated_at"),
            created_at=d.get("created_at", ""),
        )

@dataclass
class MemoryEntity:

    id: str = ""
    bank_id: str = ""
    canonical_name: str = ""
    entity_kind: str = "regular"
    metadata: dict = field(default_factory=dict)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    mention_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_id": self.bank_id,
            "canonical_name": self.canonical_name,
            "entity_kind": self.entity_kind,
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "mention_count": self.mention_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntity":
        meta = d.get("metadata", "{}")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return cls(
            id=d.get("id", ""),
            bank_id=d.get("bank_id", ""),
            canonical_name=d.get("canonical_name", ""),
            entity_kind=d.get("entity_kind", "regular"),
            metadata=meta,
            first_seen=d.get("first_seen"),
            last_seen=d.get("last_seen"),
            mention_count=d.get("mention_count", 0),
        )

@dataclass
class MemoryObservation:

    id: str = ""
    bank_id: str = ""
    text: str = ""
    proof_count: int = 1
    source_fact_ids: list = field(default_factory=list)
    evidence_quotes: list = field(default_factory=list)
    scope_tags: list = field(default_factory=list)
    stale: bool = False
    created_at: str = ""
    updated_at: str = ""
    consolidated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_id": self.bank_id,
            "text": self.text,
            "proof_count": self.proof_count,
            "source_fact_ids": json.dumps(self.source_fact_ids, ensure_ascii=False),
            "evidence_quotes": json.dumps(self.evidence_quotes, ensure_ascii=False),
            "scope_tags": json.dumps(self.scope_tags, ensure_ascii=False),
            "stale": 1 if self.stale else 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "consolidated_at": self.consolidated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryObservation":
        def _loads(v, default):
            if v is None:
                return default
            if isinstance(v, (dict, list)):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    return default
            return default

        return cls(
            id=d.get("id", ""),
            bank_id=d.get("bank_id", ""),
            text=d.get("text", ""),
            proof_count=d.get("proof_count", 1),
            source_fact_ids=_loads(d.get("source_fact_ids"), []),
            evidence_quotes=_loads(d.get("evidence_quotes"), []),
            scope_tags=_loads(d.get("scope_tags"), []),
            stale=bool(d.get("stale", 0)),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            consolidated_at=d.get("consolidated_at"),
        )

@dataclass
class MentalModel:

    id: str = ""
    bank_id: str = ""
    name: str = ""
    source_query: str = ""
    content: Optional[str] = None
    tags: list = field(default_factory=list)
    max_tokens: int = 2048
    trigger_config: dict = field(default_factory=dict)
    content_hash: Optional[str] = None
    last_refreshed_at: Optional[str] = None
    previous_versions: list = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_id": self.bank_id,
            "name": self.name,
            "source_query": self.source_query,
            "content": self.content,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "max_tokens": self.max_tokens,
            "trigger_config": json.dumps(self.trigger_config, ensure_ascii=False),
            "content_hash": self.content_hash,
            "last_refreshed_at": self.last_refreshed_at,
            "previous_versions": json.dumps(self.previous_versions, ensure_ascii=False),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MentalModel":
        def _loads(v, default):
            if v is None:
                return default
            if isinstance(v, (dict, list)):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    return default
            return default

        return cls(
            id=d.get("id", ""),
            bank_id=d.get("bank_id", ""),
            name=d.get("name", ""),
            source_query=d.get("source_query", ""),
            content=d.get("content"),
            tags=_loads(d.get("tags"), []),
            max_tokens=d.get("max_tokens", 2048),
            trigger_config=_loads(d.get("trigger_config"), {}),
            content_hash=d.get("content_hash"),
            last_refreshed_at=d.get("last_refreshed_at"),
            previous_versions=_loads(d.get("previous_versions"), []),
            created_at=d.get("created_at", ""),
        )

@dataclass
class Directive:

    id: str = ""
    bank_id: str = ""
    name: str = ""
    content: str = ""
    priority: int = 0
    is_active: bool = True
    tags: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_id": self.bank_id,
            "name": self.name,
            "content": self.content,
            "priority": self.priority,
            "is_active": 1 if self.is_active else 0,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Directive":
        tags = d.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        return cls(
            id=d.get("id", ""),
            bank_id=d.get("bank_id", ""),
            name=d.get("name", ""),
            content=d.get("content", ""),
            priority=d.get("priority", 0),
            is_active=bool(d.get("is_active", 1)),
            tags=tags,
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

@dataclass
class RecallScores:

    final: float = 0.0
    reranker: Optional[float] = None
    semantic: Optional[float] = None
    keyword: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "final": self.final,
            "reranker": self.reranker,
            "semantic": self.semantic,
            "keyword": self.keyword,
        }

@dataclass
class RecallResult:

    results: list = field(default_factory=list)
    trace: Optional[dict] = None
    entities: Optional[dict] = None
    chunks: Optional[dict] = None
    source_facts: Optional[dict] = None
    source_facts_truncated: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "results": self.results,
            "trace": self.trace,
            "entities": self.entities,
            "chunks": self.chunks,
            "source_facts": self.source_facts,
            "source_facts_truncated": self.source_facts_truncated,
        }
