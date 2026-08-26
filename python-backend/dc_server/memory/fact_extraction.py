
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .utils import (
    is_degenerate_text,
    parse_llm_json,
    sanitize_text,
)

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 3000
DEFAULT_STRUCTURED_CHUNK_SIZE = 8192
_MIN_SPLIT_CHUNK_CHARS = 500
_SECONDS_PER_FACT = 0.01

VALID_FACT_TYPES = {"world", "experience"}
VALID_FACT_KINDS = {"event", "conversation"}

def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    structured_chunk_size: int = DEFAULT_STRUCTURED_CHUNK_SIZE,
) -> list[str]:
    text = text.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return _chunk_json_array(data, structured_chunk_size)
        except json.JSONDecodeError:
            pass

    lines = text.split("\n")
    if len(lines) > 1 and all(_is_jsonish(l) for l in lines if l.strip()):
        return _chunk_jsonl(lines, structured_chunk_size)

    return _chunk_text_recursive(text, chunk_size)

def _is_jsonish(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("{") or stripped.startswith("[")

def _chunk_json_array(data: list, max_size: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for item in data:
        item_str = json.dumps(item, ensure_ascii=False)
        item_size = len(item_str)
        if current and current_size + item_size > max_size:
            chunks.append(json.dumps(current, ensure_ascii=False))
            current = []
            current_size = 0
        current.append(item)
        current_size += item_size
    if current:
        chunks.append(json.dumps(current, ensure_ascii=False))
    return chunks

def _chunk_jsonl(lines: list[str], max_size: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if not line.strip():
            continue
        line_size = len(line)
        if current and current_size + line_size > max_size:
            chunks.append("\n".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks

_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ", ""]

def _chunk_text_recursive(text: str, max_size: int, separators: Optional[list[str]] = None) -> list[str]:
    if len(text) <= max_size:
        return [text]

    seps = separators or _SEPARATORS
    for i, sep in enumerate(seps):
        if sep and sep not in text:
            continue
        if sep:
            parts = text.split(sep)
        else:
            parts = [text[j : j + max_size] for j in range(0, len(text), max_size)]

        chunks: list[str] = []
        current = ""
        for part in parts:
            candidate = current + sep + part if current else part
            if len(candidate) > max_size and current:
                chunks.append(current)
                current = part
            else:
                current = candidate
        if current:
            chunks.append(current)

        if any(len(c) > max_size for c in chunks):
            next_seps = seps[i + 1 :]
            if next_seps:
                result = []
                for c in chunks:
                    if len(c) > max_size:
                        result.extend(_chunk_text_recursive(c, max_size, next_seps))
                    else:
                        result.append(c)
                return result
        return chunks

    return [text]

class ExtractedFact:

    def __init__(
        self,
        what: str,
        when: str = "N/A",
        where: str = "N/A",
        who: str = "N/A",
        why: str = "N/A",
        fact_type: str = "world",
        fact_kind: str = "conversation",
        occurred_start: Optional[str] = None,
        occurred_end: Optional[str] = None,
        entities: Optional[list[str]] = None,
        causal_relations: Optional[list[dict]] = None,
    ):
        self.what = what
        self.when = when
        self.where = where
        self.who = who
        self.why = why
        self.fact_type = "experience" if fact_type == "assistant" else fact_type
        if self.fact_type not in VALID_FACT_TYPES:
            self.fact_type = "world"
        self.fact_kind = fact_kind if fact_kind in VALID_FACT_KINDS else "conversation"
        self.occurred_start = occurred_start
        self.occurred_end = occurred_end
        self.entities = self._coerce_entity_strings(entities or [])
        self.causal_relations = self._validate_causal_relations(causal_relations or [])

    @staticmethod
    def _coerce_entity_strings(entities: list) -> list[str]:
        result = []
        for e in entities:
            if isinstance(e, str):
                result.append(e.strip())
            elif isinstance(e, dict):
                val = e.get("text") or e.get("name") or str(e)
                if isinstance(val, str):
                    result.append(val.strip())
            else:
                result.append(str(e).strip())
        return [e for e in result if e]

    @staticmethod
    def _validate_causal_relations(relations: list) -> list[dict]:
        if not relations:
            return []
        valid = []
        for r in relations[:2]:
            if not isinstance(r, dict):
                continue
            target_idx = r.get("target_index")
            if target_idx is not None and isinstance(target_idx, int) and target_idx >= 0:
                rel_type = r.get("relation_type", "caused_by")
                if rel_type not in ("caused_by", "causes", "enables", "prevents"):
                    rel_type = "caused_by"
                valid.append({"target_index": target_idx, "relation_type": rel_type})
        return valid

    @property
    def fact_text(self) -> str:
        parts = [self.what]
        if self.when and self.when != "N/A":
            parts.append(f"When: {self.when}")
        if self.who and self.who != "N/A":
            parts.append(f"Involving: {self.who}")
        if self.why and self.why != "N/A":
            parts.append(self.why)
        return " | ".join(parts)

    def is_degenerate(self) -> bool:
        return is_degenerate_text(self.what)

class FactExtractor:

    def __init__(
        self,
        llm_client,
        model: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        structured_chunk_size: int = DEFAULT_STRUCTURED_CHUNK_SIZE,
        extraction_mode: str = "concise",
        mission: str = "",
        output_language: str = "",
        custom_instructions: str = "",
        max_retries: int = 3,
    ):
        self.llm_client = llm_client
        self.model = model
        self.chunk_size = chunk_size
        self.structured_chunk_size = structured_chunk_size
        self.extraction_mode = extraction_mode
        self.mission = mission
        self.output_language = output_language
        self.custom_instructions = custom_instructions
        self.max_retries = max_retries

    async def extract(
        self,
        text: str,
        event_date: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> list[ExtractedFact]:
        text = sanitize_text(text)
        if not text or is_degenerate_text(text):
            return []

        chunks = chunk_text(text, self.chunk_size, self.structured_chunk_size)
        if not chunks:
            return []

        tasks = [
            self._extract_from_chunk(chunk, event_date, document_id, i)
            for i, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_facts: list[ExtractedFact] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Chunk {i} extraction failed: {result}")
                raise result
            all_facts.extend(result)

        return all_facts

    async def _extract_from_chunk(
        self,
        chunk: str,
        event_date: Optional[str],
        document_id: Optional[str],
        chunk_index: int,
    ) -> list[ExtractedFact]:
        for attempt in range(self.max_retries + 1):
            try:
                facts = await self._call_llm_extract(chunk, event_date)
                return self._process_facts(facts, event_date, chunk_index)
            except _OutputTooLongError:
                if len(chunk) < _MIN_SPLIT_CHUNK_CHARS:
                    raise
                mid = len(chunk) // 2
                split_pos = _find_split_pos(chunk, mid)
                left = chunk[:split_pos]
                right = chunk[split_pos:]
                left_facts = await self._extract_from_chunk(left, event_date, document_id, chunk_index)
                right_facts = await self._extract_from_chunk(
                    right, event_date, document_id, chunk_index + 1
                )
                return left_facts + right_facts
            except Exception as e:
                if attempt < self.max_retries:
                    logger.warning(f"Chunk {chunk_index} attempt {attempt+1} failed: {e}, retrying")
                    continue
                raise

        return []

    async def _call_llm_extract(
        self, chunk: str, event_date: Optional[str]
    ) -> list[dict]:
        from .utils import output_language_directive

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(chunk, event_date)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        full_response = ""
        async for event in self.llm_client.stream_chat(messages, tools=None, signal=None):
            if event.get("type") == "text":
                full_response += event.get("text", "")
            elif event.get("type") == "usage":
                output_tokens = event.get("output_tokens", 0)
                if output_tokens > 0 and output_tokens > 4000:
                    raise _OutputTooLongError(f"Output tokens: {output_tokens}")

        parsed = parse_llm_json(full_response)

        if isinstance(parsed, dict) and "facts" in parsed:
            return parsed["facts"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]

        return []

    def _build_system_prompt(self) -> str:
        from .utils import escape_for_prompt, output_language_directive

        import os
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts",
            "memory",
            "fact-extraction.md",
        )
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()
        except FileNotFoundError:
            system_prompt = _DEFAULT_SYSTEM_PROMPT

        system_prompt += output_language_directive(self.output_language)

        return system_prompt

    def _build_user_prompt(self, chunk: str, event_date: Optional[str]) -> str:
        parts = []
        if self.mission:
            parts.append(f"MISSION: {self.mission}")
        if event_date:
            parts.append(f"REFERENCE DATE: {event_date}")
        parts.append(f"CONTENT TO ANALYZE:\n{chunk}")
        return "\n\n".join(parts)

    def _process_facts(
        self, raw_facts: list[dict], event_date: Optional[str], chunk_index: int
    ) -> list[ExtractedFact]:
        processed = []
        for i, raw in enumerate(raw_facts):
            if not isinstance(raw, dict):
                continue
            what = raw.get("what") or raw.get("factual_core") or raw.get("text") or ""
            if not what:
                continue
            fact = ExtractedFact(
                what=what,
                when=raw.get("when", "N/A"),
                where=raw.get("where", "N/A"),
                who=raw.get("who", "N/A"),
                why=raw.get("why", "N/A"),
                fact_type=raw.get("fact_type", "world"),
                fact_kind=raw.get("fact_kind", "conversation"),
                occurred_start=raw.get("occurred_start"),
                occurred_end=raw.get("occurred_end"),
                entities=raw.get("entities"),
                causal_relations=raw.get("causal_relations"),
            )
            if fact.is_degenerate():
                continue
            if not fact.occurred_start and event_date and fact.fact_kind == "event":
                fact.occurred_start = _infer_temporal_date(fact.what, event_date)
            processed.append(fact)
        return processed

class _OutputTooLongError(Exception):
    pass

def _find_split_pos(text: str, mid: int) -> int:
    for offset in range(100):
        for pos in [mid + offset, mid - offset]:
            if 0 <= pos < len(text) and text[pos] in ".\n!?\n":
                return pos + 1
    return mid

_RELATIVE_TIME_PATTERNS = [
    (re.compile(r"yesterday", re.I), -1),
    (re.compile(r"today", re.I), 0),
    (re.compile(r"tomorrow", re.I), 1),
    (re.compile(r"last\s+week", re.I), -7),
    (re.compile(r"this\s+week", re.I), 0),
    (re.compile(r"next\s+week", re.I), 7),
    (re.compile(r"last\s+month", re.I), -30),
    (re.compile(r"next\s+month", re.I), 30),
    (re.compile(r"last\s+year", re.I), -365),
    (re.compile(r"next\s+year", re.I), 365),
]

def _infer_temporal_date(text: str, event_date: str) -> Optional[str]:
    if not event_date or not text:
        return None
    try:
        base = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    for pattern, day_offset in _RELATIVE_TIME_PATTERNS:
        if pattern.search(text):
            result = base + timedelta(days=day_offset)
            return result.isoformat()

    return None

_DEFAULT_SYSTEM_PROMPT = """You are a fact extraction assistant. Extract memorable facts from the given content.

For each fact, provide:
- what: The core fact (1-2 sentences, concise but complete)
- when: When it happened (or "N/A")
- where: Location (or "N/A")
- who: People involved and relationships (or "N/A")
- why: Importance/motivation/context (or "N/A")
- fact_type: "world" (about the user/world) or "assistant" (about your own behavior)
- fact_kind: "event" (specific dated occurrence) or "conversation" (ongoing state/preference)
- entities: Array of entity name strings (not objects)
- causal_relations: Optional array of {target_index, relation_type}

SELECTIVITY (concise mode): Only extract facts worth remembering 6 months from now.
Skip greetings, filler, procedural dialogue, and trivial observations.

Output as JSON array: [{"what": "...", "when": "...", ...}]
"""
