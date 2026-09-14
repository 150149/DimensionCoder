# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

OLD = "dcflow_run_cmd"
NEW = "dcflow_run_code"

MARKER = "历史调用迁移"
MARKER_DATE = "2026-09-10"

_BINARY_HINT_RES = re.compile(r"请改用\s*" + re.escape(OLD) + r"(?:\s*\+\s*python)?")
_BINARY_HINT_REPL = "请改用 " + NEW
_BINARY_HINT_PROBE = "请改用 " + OLD

_BATCH = 500
_SAMPLE_CAP = 3
_UNPARSABLE_CAP = 20

_STRUCTURAL_COLUMNS = {
    ("step_messages", "tool_name"),
}

_PROSE_COLUMNS = {
    ("step_messages", "content"),
    ("task_steps", "description"),
    ("events", "content"),
}

_PROSE_SPECS = (
    ("step_messages", "content", None, ()),
    ("step_messages", "tool_output", None, ()),
    ("step_messages", "tool_input", "COALESCE(tool_name,'') NOT IN (?,?)", (OLD, NEW)),
    ("artifacts", "content", "artifact_type != 'compress_map'", ()),
    ("task_steps", "description", None, ()),
    ("events", "content", None, ()),
)




def wrap_command(cmd: str) -> str:
    return (
        f"# {MARKER}（{MARKER_DATE}：原为单条 shell 命令，现由 {NEW} 承接）\n"
        "import subprocess\n"
        f"r = subprocess.run({cmd!r}, shell=True, capture_output=True, text=True)\n"
        "print(r.stdout)\n"
        "if r.stderr:\n"
        "    print(r.stderr)\n"
    )


def _description_of(cmd: str) -> str:
    first = cmd.splitlines()[0] if cmd.splitlines() else ""
    return first[:60]


def rewrite_tool_input(raw: str) -> tuple:
    if not raw or not raw.strip():
        return raw, "unparsable"
    try:
        d = json.loads(raw)
    except Exception:                                   # noqa: BLE001
        return raw, "unparsable"
    if not isinstance(d, dict):
        return raw, "unparsable"
    if "code" in d:
        return raw, "already_new"
    cmd = d.get("command")
    if not isinstance(cmd, str):
        return raw, "unparsable"
    out = {"description": _description_of(cmd), "code": wrap_command(cmd)}
    if "timeout_seconds" in d:
        out["timeout_seconds"] = d["timeout_seconds"]
    return json.dumps(out, ensure_ascii=False), "migrated"


def rewrite_tool_calls(raw: str) -> tuple:
    if not raw or OLD not in raw:
        return raw, 0
    try:
        arr = json.loads(raw)
    except Exception:                                   # noqa: BLE001
        return raw, 0
    if not isinstance(arr, list):
        return raw, 0
    n = 0
    for el in arr:
        if not isinstance(el, dict):
            continue
        fn = el.get("function")
        if not isinstance(fn, dict) or fn.get("name") != OLD:
            continue
        fn["name"] = NEW
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str):
            new_args, status = rewrite_tool_input(args_raw)
            if status in ("migrated", "already_new"):
                fn["arguments"] = new_args
        n += 1
    if n == 0:
        return raw, 0
    return json.dumps(arr, ensure_ascii=False), n


def rewrite_binary_hint(text: str) -> tuple:
    if not text or OLD not in text:
        return text, 0
    return _BINARY_HINT_RES.subn(_BINARY_HINT_REPL, text)




def _existing_tables(conn) -> set:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def _batch_update(conn, sql: str, rows: list) -> int:
    for i in range(0, len(rows), _BATCH):
        conn.executemany(sql, rows[i:i + _BATCH])
        conn.commit()
    return len(rows)


def _count(conn, sql: str, params=()) -> int:
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error:
        return 0


def _add_sample(bucket: list, item) -> None:
    if len(bucket) < _SAMPLE_CAP:
        bucket.append(item)




async def migrate_run_cmd_to_run_code(storage, include_prose: bool = False) -> dict:
    conn = await storage._get_conn()
    have = _existing_tables(conn)
    st = {
        "tool_rows": 0,
        "tool_name_rows": 0,
        "already_new": 0,
        "unparsable": 0,
        "assistant_rows": 0,
        "tool_call_elements": 0,
        "stream_chunks": 0,
        "artifacts": 0,
        "binary_hints": 0,
        "prose_rows": 0,
        "prose_skipped": 0,
        "unparsable_samples": [],
        "samples": {"tool_input": [], "tool_calls": [], "stream_chunks": [],
                    "binary_hint": [], "artifacts": [], "prose": []},
        "prose_breakdown": {},
        "include_prose": bool(include_prose),
    }
    like = f"%{OLD}%"

    if "step_messages" in have:
        rows = conn.execute(
            "SELECT id, tool_input FROM step_messages WHERE tool_name = ?",
            (OLD,)).fetchall()
        buf = []
        for rid, ti in rows:
            ti = ti or ""
            new_ti, status = rewrite_tool_input(ti)
            st["tool_name_rows"] += 1
            if status == "migrated":
                st["tool_rows"] += 1
                _add_sample(st["samples"]["tool_input"], (ti[:200], new_ti[:400]))
                buf.append((NEW, new_ti, rid))
            elif status == "already_new":
                st["already_new"] += 1
                buf.append((NEW, ti, rid))
            else:
                st["unparsable"] += 1
                if len(st["unparsable_samples"]) < _UNPARSABLE_CAP:
                    st["unparsable_samples"].append(f"id={rid} tool_input={ti[:400]!r}")
                buf.append((NEW, ti, rid))
        _batch_update(conn,
                      "UPDATE step_messages SET tool_name=?, tool_input=? WHERE id=?",
                      buf)

        rows = conn.execute(
            "SELECT id, tool_calls FROM step_messages WHERE tool_calls LIKE ?",
            (like,)).fetchall()
        buf = []
        for rid, tc in rows:
            new_tc, n = rewrite_tool_calls(tc or "")
            if n:
                st["assistant_rows"] += 1
                st["tool_call_elements"] += n
                _add_sample(st["samples"]["tool_calls"], ((tc or "")[:200], new_tc[:400]))
                buf.append((new_tc, rid))
        _batch_update(conn, "UPDATE step_messages SET tool_calls=? WHERE id=?", buf)

        rows = conn.execute(
            "SELECT id, tool_output FROM step_messages WHERE tool_output LIKE ?",
            (like,)).fetchall()
        buf = []
        for rid, to in rows:
            new_to, n = rewrite_binary_hint(to or "")
            if n:
                st["binary_hints"] += n
                _add_sample(st["samples"]["binary_hint"], ((to or "")[:200], new_to[:200]))
                buf.append((new_to, rid))
        _batch_update(conn, "UPDATE step_messages SET tool_output=? WHERE id=?", buf)

    if "stream_chunks" in have:
        rows = conn.execute(
            "SELECT id, content FROM stream_chunks WHERE content LIKE ?",
            (like,)).fetchall()
        buf = [(c.replace(OLD, NEW), rid) for rid, c in rows]
        st["stream_chunks"] = len(buf)
        for rid, c in rows[:_SAMPLE_CAP]:
            _add_sample(st["samples"]["stream_chunks"], ((c or "")[:120],))
        _batch_update(conn, "UPDATE stream_chunks SET content=? WHERE id=?", buf)

    if "artifacts" in have:
        rows = conn.execute(
            "SELECT id, content FROM artifacts "
            "WHERE artifact_type='compress_map' AND content LIKE ?", (like,)).fetchall()
        buf = [(c.replace(OLD, NEW), rid) for rid, c in rows]
        st["artifacts"] = len(buf)
        for rid, c in rows[:_SAMPLE_CAP]:
            _add_sample(st["samples"]["artifacts"], ((c or "")[:200],))
        _batch_update(conn, "UPDATE artifacts SET content=? WHERE id=?", buf)

    for table, column, extra, extra_params in _PROSE_SPECS:
        if table not in have:
            continue
        where = f"{column} LIKE ?" + (f" AND {extra}" if extra else "")
        sql = f"SELECT COUNT(1) FROM {table} WHERE {where}"
        cnt = _count(conn, sql, (like,) + tuple(extra_params))
        key = f"{table}.{column}"
        if not include_prose:
            st["prose_skipped"] += cnt
            if cnt:
                st["prose_breakdown"][key] = cnt
            continue
        rows = conn.execute(
            f"SELECT id, {column} FROM {table} WHERE {where}",
            (like,) + tuple(extra_params)).fetchall()
        buf = []
        for rid, val in rows:
            if isinstance(val, str) and OLD in val:
                buf.append((val.replace(OLD, NEW), rid))
        st["prose_rows"] += _batch_update(
            conn, f"UPDATE {table} SET {column}=? WHERE id=?", buf)
        if buf:
            st["prose_breakdown"][key] = len(buf)
            _add_sample(st["samples"]["prose"], (key, str(buf[0][0])[:200]))

    conn.commit()
    return st




def _text_columns(conn) -> list:
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        try:
            info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        except sqlite3.Error:
            continue
        for row in info:
            ctype = str(row[2] or "").upper()
            if ctype in ("TEXT", "VARCHAR", "CLOB", "LONGTEXT", ""):
                out.append((name, row[1]))
    return out


def _classify_hits(conn, table: str, column: str, like: str, total: int) -> list:
    key = (table, column)
    if key in _STRUCTURAL_COLUMNS:
        return [(column, total, "structural")]
    if key in _PROSE_COLUMNS:
        return [(column, total, "prose")]
    if key == ("step_messages", "tool_calls"):
        rows = conn.execute(
            "SELECT tool_calls FROM step_messages WHERE tool_calls LIKE ?",
            (like,)).fetchall()
        s = kept = 0
        for (tc,) in rows:
            try:
                arr = json.loads(tc or "")
            except Exception:                   # noqa: BLE001
                arr = None
            if not isinstance(arr, list):
                s += 1
            elif any(isinstance(e, dict) and isinstance(e.get("function"), dict)
                     and e["function"].get("name") == OLD for e in arr):
                s += 1
            else:
                kept += 1
        return [("tool_calls(旧名元素未迁)", s, "structural"),
                ("tool_calls(原命令文本字面量)", kept, "preserved")]
    if key == ("step_messages", "tool_input"):
        rows = conn.execute(
            "SELECT tool_name, tool_input FROM step_messages WHERE tool_input LIKE ?",
            (like,)).fetchall()
        s = kept = p = 0
        for tn, ti in rows:
            if (tn or "") not in (OLD, NEW):
                p += 1
                continue
            try:
                d = json.loads(ti or "")
            except Exception:                   # noqa: BLE001
                d = None
            if isinstance(d, dict) and "code" in d and "command" not in d:
                kept += 1
            else:
                s += 1
        return [("tool_input(旧形状未迁)", s, "structural"),
                ("tool_input(原命令文本字面量)", kept, "preserved"),
                ("tool_input(AI 代码串)", p, "prose")]
    if key == ("stream_chunks", "content"):
        full = _count(conn,
                      "SELECT COUNT(1) FROM stream_chunks WHERE content LIKE ?",
                      (f"%{OLD}%",))
        return [("stream_chunks(旧名未迁)", full, "structural"),
                ("stream_chunks(输出文本裸名)", max(total - full, 0), "preserved")]
    if key == ("step_messages", "tool_output"):
        mach = _count(conn,
                      "SELECT COUNT(1) FROM step_messages WHERE tool_output LIKE ?",
                      (f"%{_BINARY_HINT_PROBE}%",))
        return [("tool_output(二进制提示句式)", mach, "structural"),
                ("tool_output(程序输出原文)", max(total - mach, 0), "prose")]
    if key == ("artifacts", "content"):
        cm = _count(conn,
                    "SELECT COUNT(1) FROM artifacts WHERE content LIKE ? "
                    "AND artifact_type='compress_map'", (like,))
        return [("artifacts(compress_map)", cm, "structural"),
                ("artifacts(AI 产物)", max(total - cm, 0), "prose")]
    return [(column, total, "other")]


async def scan_residue(storage, needle: str = "run_cmd") -> dict:
    conn = await storage._get_conn()
    like = f"%{needle}%"
    hits = []
    for table, column in _text_columns(conn):
        total = _count(conn, f'SELECT COUNT(1) FROM "{table}" WHERE "{column}" LIKE ?',
                       (like,))
        if not total:
            continue
        for label, cnt, category in _classify_hits(conn, table, column, like, total):
            if cnt:
                hits.append({"table": table, "column": column, "label": label,
                             "count": cnt, "category": category})
    return {
        "needle": needle,
        "total": sum(h["count"] for h in hits),
        "structural": sum(h["count"] for h in hits if h["category"] == "structural"),
        "preserved": sum(h["count"] for h in hits if h["category"] == "preserved"),
        "prose": sum(h["count"] for h in hits if h["category"] == "prose"),
        "other": sum(h["count"] for h in hits if h["category"] == "other"),
        "hits": hits,
    }




def format_audit_report(stats: dict, residue: Optional[dict] = None) -> str:
    L = []
    L.append("=" * 72)
    L.append(f"{OLD} → {NEW} 迁移审计（{MARKER_DATE}）")
    L.append("=" * 72)
    L.append("[计数]")
    for k in ("tool_name_rows", "tool_rows", "already_new", "unparsable",
              "assistant_rows", "tool_call_elements", "stream_chunks",
              "artifacts", "binary_hints", "prose_rows", "prose_skipped"):
        L.append(f"  {k:<22} {stats.get(k, 0)}")
    L.append(f"  include_prose          {stats.get('include_prose')}")
    if stats.get("prose_breakdown"):
        L.append("[AI 叙述类分布（默认不迁；include_prose 时为已扫净行数）]")
        for k, v in sorted(stats["prose_breakdown"].items()):
            L.append(f"  {k:<40} {v}")
    if stats.get("unparsable_samples"):
        L.append(f"[畸形 tool_input 原文（只改名、input 原样保留，共 "
                 f"{stats.get('unparsable')} 行，最多列 {_UNPARSABLE_CAP} 条）]")
        for s in stats["unparsable_samples"]:
            L.append(f"  {s}")
    for title, key in (("tool_input", "tool_input"), ("tool_calls", "tool_calls"),
                       ("stream_chunks", "stream_chunks"),
                       ("compress_map artifact", "artifacts"),
                       ("二进制提示句式", "binary_hint"), ("prose", "prose")):
        samples = (stats.get("samples") or {}).get(key) or []
        if not samples:
            continue
        L.append(f"[抽样前后对比：{title}]")
        for s in samples:
            if len(s) == 1:
                L.append(f"  before: {s[0]}")
            elif key == "prose":
                L.append(f"  {s[0]}: {s[1]}")
            else:
                L.append(f"  before: {s[0]}")
                L.append(f"  after : {s[1]}")
    if residue:
        L.append("[全库残留扫描] needle=%r" % residue.get("needle"))
        L.append(f"  total={residue.get('total')} structural={residue.get('structural')} "
                 f"preserved={residue.get('preserved')} "
                 f"prose={residue.get('prose')} other={residue.get('other')}")
        L.append("  逐表逐列（structural 必须为 0）：")
        for h in residue.get("hits", []):
            L.append(f"    [{h['category']:<10}] {h['table']}.{h['column']}"
                     f" ({h['label']}) = {h['count']}")
    return "\n".join(L) + "\n"
