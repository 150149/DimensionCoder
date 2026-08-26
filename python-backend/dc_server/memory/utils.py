
from __future__ import annotations

import json
import logging
import re
import os
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")

def sanitize_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return _CONTROL_RE.sub("", text)

sanitize_llm_output = sanitize_text

def _escape_control_chars_in_json(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            result.append(ch)
            escaped = False
            continue
        if ch == "\\":
            result.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            code = ord(ch)
            if code == 0x0A:
                result.append("\\n")
            elif code == 0x0D:
                result.append("\\r")
            elif code == 0x09:
                result.append("\\t")
            elif code == 0x08:
                result.append("\\b")
            elif code == 0x0C:
                result.append("\\f")
            elif code < 0x20 or code == 0x7F:
                result.append(" ")
            else:
                result.append(ch)
        else:
            result.append(ch)
    return "".join(result)

def parse_llm_json(raw: str) -> Any:
    if not raw or not raw.strip():
        raise json.JSONDecodeError("Empty input", raw, 0)

    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.startswith("json") or text.startswith("JSON"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        escaped = _escape_control_chars_in_json(text)
        return json.loads(escaped)
    except json.JSONDecodeError:
        pass

    try:
        import json_repair
        repaired = json_repair.loads(text)
        if repaired is not None:
            return repaired
    except ImportError:
        logger.debug("json_repair not installed, skipping structural repair")
    except Exception:
        pass

    raise json.JSONDecodeError(f"Failed to parse LLM JSON output (len={len(raw)})", raw, 0)

_LONE_OPEN_BRACE = re.compile(r"(?<!\{)\{(?!\{)")
_LONE_CLOSE_BRACE = re.compile(r"(?<!\})\}(?!\})")

def escape_for_prompt(text: str) -> str:
    text = _LONE_OPEN_BRACE.sub("{{", text)
    text = _LONE_CLOSE_BRACE.sub("}}", text)
    return text

def output_language_directive(language: Optional[str]) -> str:
    if not language:
        return ""
    return (
        f"\n\nIMPORTANT: Respond exclusively in {language}. "
        f"Translate any source content into {language}. "
        f"All output text — including fact text, observations, entity names, "
        f"and the final response — must be in {language}."
    )

_ENCODING = None

def _get_encoding():
    global _ENCODING
    if _ENCODING is not None:
        return _ENCODING
    try:
        import tiktoken

        class _SafeEncoding:

            def __init__(self, encoding):
                self._enc = encoding

            def encode(self, text: str) -> list[int]:
                return self._enc.encode(text, disallowed_special=())

            def decode(self, tokens: list[int]) -> str:
                return self._enc.decode(tokens)

        _ENCODING = _SafeEncoding(tiktoken.get_encoding("cl100k_base"))
    except ImportError:
        logger.debug("tiktoken not installed, token counting will be approximate")
        _ENCODING = None
    return _ENCODING

def count_tokens(text: str) -> int:
    enc = _get_encoding()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))

_COUNT_WINDOW_CHARS = 1024 * 1024

def count_tokens_windowed(text: str) -> int:
    enc = _get_encoding()
    if enc is None:
        return max(1, len(text) // 4)
    total = 0
    start = 0
    while start < len(text):
        chunk = text[start : start + _COUNT_WINDOW_CHARS]
        total += len(enc.encode(chunk))
        start += _COUNT_WINDOW_CHARS
    return total

def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, int]:
    enc = _get_encoding()
    if enc is None:
        orig = len(text)
        approx = max(1, orig // 4)
        limit = max(1, max_tokens * 4)
        return text[:limit], approx
    tokens = enc.encode(text)
    orig_count = len(tokens)
    if orig_count <= max_tokens:
        return text, orig_count
    truncated = enc.decode(tokens[:max_tokens])
    return truncated, orig_count

def drop_null_values(metadata: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if metadata is None:
        return {}
    return {k: v for k, v in metadata.items() if v is not None}

def as_string_metadata(metadata: Optional[Mapping[str, Any]]) -> dict[str, str]:
    if metadata is None:
        return {}
    return {str(k): str(v) for k, v in drop_null_values(metadata).items()}

_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]*$", re.UNICODE)

def is_degenerate_text(text: Optional[str]) -> bool:
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) <= 2 and not any(c.isalnum() for c in stripped):
        return True
    if _PUNCT_ONLY_RE.match(stripped):
        return True
    return False

SECONDS_PER_FACT = 0.01

def apply_temporal_offset(base_ts: str, index: int) -> str:
    if not base_ts:
        return base_ts
    try:
        from datetime import datetime, timedelta, timezone

        dt = datetime.fromisoformat(base_ts.replace("Z", "+00:00"))
        dt = dt + timedelta(seconds=SECONDS_PER_FACT * index)
        return dt.isoformat()
    except (ValueError, TypeError):
        return base_ts

def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def chunk_id(bank_id: str, document_id: str, chunk_index: int) -> str:
    return f"{bank_id}_{document_id}_{chunk_index}"
