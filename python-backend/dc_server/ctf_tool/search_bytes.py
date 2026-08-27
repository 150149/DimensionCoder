"""
search_bytes — 通配符字节搜索工具

解析通配符 pattern（空格分隔的十六进制，`?` 为通配符），在整个二进制中搜索匹配位置，
返回所有匹配的偏移量和上下文 hex dump。

接口：search_bytes(file_path, pattern) -> str
"""

import os
import re

# 最大匹配数量上限
MAX_MATCHES = 1000


def _parse_pattern(pattern_str: str) -> tuple:
    """
    解析通配符 pattern，返回 (regex, error_msg)。
    成功时 error_msg 为 None。
    """
    tokens = pattern_str.strip().split()
    if not tokens:
        return None, "[ERROR] search_bytes: empty pattern"

    regex_parts = []
    for token in tokens:
        if token == '?':
            regex_parts.append(b'.')  # 匹配任意单字节
        else:
            if len(token) != 2 or not all(
                    c in '0123456789abcdefABCDEF' for c in token):
                return None, (f"[ERROR] search_bytes: invalid pattern token "
                              f"'{token}', expected 2-digit hex or '?'")
            regex_parts.append(re.escape(bytes([int(token, 16)])))

    regex = re.compile(b''.join(regex_parts), re.DOTALL)
    return regex, None


def _search_data(data: bytes, regex: 're.Pattern[bytes]') -> list:
    """在数据中搜索所有匹配，最多 MAX_MATCHES 个。"""
    matches = []
    for m in regex.finditer(data):
        matches.append(m.start())
        if len(matches) >= MAX_MATCHES:
            break
    return matches


def _format_context(data: bytes, offset: int, pattern_len: int) -> list:
    """构建单个匹配的上下文 hex dump 行列表。"""
    context_before = 16
    context_after = 16
    start = max(0, offset - context_before)
    end = min(len(data), offset + pattern_len + context_after)
    context = data[start:end]
    match_start = offset - start

    hex_str = ' '.join(f'{b:02X}' for b in context)
    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in context)
    marker = ' ' * (match_start * 3) + '^^ ' * pattern_len

    return [
        f"  Offset: 0x{offset:08X}",
        f"  {hex_str}",
        f"  {marker.rstrip()}",
        f"  {ascii_str}",
        "",
    ]


def _format_output(file_path: str, data: bytes, pattern_str: str,
                   matches: list) -> str:
    """构建搜索结果输出字符串。"""
    lines = []
    lines.append(f"// Search results for pattern: {pattern_str}")
    lines.append(f"// File: {file_path} ({len(data)} bytes)")
    lines.append(f"// Matches found: {len(matches)}" +
                 (f" (truncated at {MAX_MATCHES})" if len(matches) >= MAX_MATCHES else ""))
    lines.append("")

    pattern_len = len(pattern_str.strip().split())

    if matches:
        for offset in matches:
            lines.extend(_format_context(data, offset, pattern_len))
    else:
        lines.append("// No matches found.")

    return "\n".join(lines)


def search_bytes(*args: str) -> str:
    """在二进制文件中搜索通配符字节模式，返回匹配偏移量和上下文 hex dump。"""
    try:
        if len(args) < 2:
            return (f"[ERROR] search_bytes: expected 2 arguments "
                    f"(file_path, pattern), got {len(args)}")

        file_path, pattern_str = args[0], args[1]
        if not os.path.isfile(file_path):
            return f"[ERROR] search_bytes: file not found: {file_path}"

        regex, err = _parse_pattern(pattern_str)
        if err:
            return err

        with open(file_path, 'rb') as f:
            data = f.read()
        if not data:
            return f"[ERROR] search_bytes: file is empty: {file_path}"

        matches = _search_data(data, regex)
        return _format_output(file_path, data, pattern_str, matches)

    except Exception as e:
        return f"[ERROR] search_bytes: {type(e).__name__}: {e}"
