"""
memory/utils.py — 工具函数（LLM 输出清洗 + Token 计数 + Metadata 规范化）

源自 Hindsight 的 llm_wrapper.py / prompt_utils.py / token_encoding.py / metadata_utils.py
所有函数都是纯 Python，无 DB 依赖，可独立使用。
"""

from __future__ import annotations

import json
import logging
import re
import os
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# ── LLM 输出清洗 ──────────────────────────────────────────

# 控制字符 + Unicode 代理对：0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F, 0x7F, U+D800-U+DFFF
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


def sanitize_text(text: Optional[str]) -> Optional[str]:
    """移除 ASCII 控制字符和 Unicode 代理对，保留 tab/newline/CR。

    必须在 retain/recall/reflect 入口处调用，防止：
    - 交叉编码器 tokenizer 崩溃
    - SQLite UTF-8 编码错误
    """
    if text is None:
        return None
    return _CONTROL_RE.sub("", text)


# alias for backward compat
sanitize_llm_output = sanitize_text


def _escape_control_chars_in_json(text: str) -> str:
    """转义 JSON 字符串值内的控制字符，使其可被 json.loads 解析。

    跟踪字符串状态（是否在引号内），将裸控制字符转义为标准 JSON 转义序列。
    """
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
            if code == 0x0A:  # \n
                result.append("\\n")
            elif code == 0x0D:  # \r
                result.append("\\r")
            elif code == 0x09:  # \t
                result.append("\\t")
            elif code == 0x08:  # backspace
                result.append("\\b")
            elif code == 0x0C:  # \f
                result.append("\\f")
            elif code < 0x20 or code == 0x7F:
                result.append(" ")
            else:
                result.append(ch)
        else:
            result.append(ch)
    return "".join(result)


def parse_llm_json(raw: str) -> Any:
    """三阶段 JSON 解析（从 Hindsight llm_wrapper.py 移植）。

    1. 剥离 markdown fence → json.loads
    2. 转义控制字符 → json.loads
    3. json_repair 结构修复作为最后手段

    空结果 → raise JSONDecodeError
    """
    if not raw or not raw.strip():
        raise json.JSONDecodeError("Empty input", raw, 0)

    # Stage 1: 剥离 markdown fence
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        # 也可能像 ```json\n...\n```
        if text.startswith("json") or text.startswith("JSON"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Stage 2: 转义控制字符
    try:
        escaped = _escape_control_chars_in_json(text)
        return json.loads(escaped)
    except json.JSONDecodeError:
        pass

    # Stage 3: json_repair 结构修复
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


# ── Prompt 安全 ────────────────────────────────────────────

_LONE_OPEN_BRACE = re.compile(r"(?<!\{)\{(?!\{)")
_LONE_CLOSE_BRACE = re.compile(r"(?<!\})\}(?!\})")


def escape_for_prompt(text: str) -> str:
    """将单独的 { / } 双写，使其通过 str.format 不被解析。

    幂等：已有的 {{ / }} 对保持不变。仅处理"单独"大括号（不与同类相邻的）。
    当 prompt 模板用 str.format 填充时必须使用（bank mission 含 JSON 示例会触发 KeyError）。
    """
    text = _LONE_OPEN_BRACE.sub("{{", text)
    text = _LONE_CLOSE_BRACE.sub("}}", text)
    return text


def output_language_directive(language: Optional[str]) -> str:
    """返回强制输出语言的指令。language 为 None 或空串时返回空串。"""
    if not language:
        return ""
    return (
        f"\n\nIMPORTANT: Respond exclusively in {language}. "
        f"Translate any source content into {language}. "
        f"All output text — including fact text, observations, entity names, "
        f"and the final response — must be in {language}."
    )


# ── Token 计数 ─────────────────────────────────────────────

_ENCODING = None


def _get_encoding():
    """懒加载 tiktoken cl100k_base 编码（导入时不联网）。"""
    global _ENCODING
    if _ENCODING is not None:
        return _ENCODING
    try:
        import tiktoken

        class _SafeEncoding:
            """包装 tiktoken.Encoding，禁用 disallowed_special 检查。

            tiktoken 默认 disallowed_special="all" 会使 encode() 在内容中提及
            特殊 token 字面量时 raise（如 <pad>）。_SafeEncoding 设为 () 即不拒绝，
            将其当作普通文本计数。token 数不受影响。
            """

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
    """精确 cl100k token 计数。适合 fact/query/chunk（小文本）。

    无 tiktoken 时退化为 len(text)//4 估算。
    """
    enc = _get_encoding()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))


_COUNT_WINDOW_CHARS = 1024 * 1024  # 1 MiB


def count_tokens_windowed(text: str) -> int:
    """1MiB 窗口计数，O(window) 内存。适合大文档。

    按固定字符偏移切分，每段独立编码后累加。切分可能切断一个 token，
    结果略大于精确值（每窗口边界最多多 1 个 token，45MB body 约 0.0003% 误差）。
    """
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
    """截断到最多 max_tokens 个 cl100k token。

    返回 (截断后文本, 原始 token 数)。
    """
    enc = _get_encoding()
    if enc is None:
        # 退化为字符截断
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


# ── Metadata 清洗 ─────────────────────────────────────────


def drop_null_values(metadata: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """写入时丢弃 null 值 key。

    解决 issue #3209：null 值通过写入路径但在每次读取时验证失败。
    """
    if metadata is None:
        return {}
    return {k: v for k, v in metadata.items() if v is not None}


def as_string_metadata(metadata: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """读取时丢弃 null + 字符串化其余值。

    JSON 往返时整数仍为整数（如 {"original_id": 348}），此函数强制 str→str 契约。
    """
    if metadata is None:
        return {}
    return {str(k): str(v) for k, v in drop_null_values(metadata).items()}


# ── 退化文本检测 ───────────────────────────────────────────

_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]*$", re.UNICODE)


def is_degenerate_text(text: Optional[str]) -> bool:
    """检测零信息量的文本。

    - None / 空字符串 / 纯空白
    - 单个或重复标点符号（...、---、***）
    - 完全由标点和空白组成
    - ≤2 字符且全是非字母数字
    """
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


# ── 时间偏移 ───────────────────────────────────────────────

SECONDS_PER_FACT = 0.01  # 10ms 偏移确保同一文档内 fact 时间戳唯一


def apply_temporal_offset(base_ts: str, index: int) -> str:
    """给同文档的 fact 加 10ms * index 偏移，确保时间戳唯一保留顺序。

    base_ts: ISO 格式时间字符串
    index: fact 在文档中的序号（0-based）
    返回偏移后的 ISO 时间字符串
    """
    if not base_ts:
        return base_ts
    try:
        from datetime import datetime, timedelta, timezone

        # 尝试解析 ISO 格式
        dt = datetime.fromisoformat(base_ts.replace("Z", "+00:00"))
        dt = dt + timedelta(seconds=SECONDS_PER_FACT * index)
        return dt.isoformat()
    except (ValueError, TypeError):
        return base_ts


# ── 哈希 ──────────────────────────────────────────────────


def content_hash(text: str) -> str:
    """SHA256 哈希，用于 chunk delta 去重。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id(bank_id: str, document_id: str, chunk_index: int) -> str:
    """生成 chunk_id。格式：{bank_id}_{document_id}_{chunk_index}"""
    return f"{bank_id}_{document_id}_{chunk_index}"
