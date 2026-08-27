"""
dc_server.brain.sse_hub — SSE 事件中心（WP3 §2.1）

契约要点：
- 事件表 8 种（stepStart / streamChunk / streamEnd / toolCallStart /
  toolCallResult / userMessage / llmError / refreshData）；事件类型经 data 行
  JSON 的 `command` 字段传递，**只发 `data:` 行**（B2），无 `event:` 行。
- hub 维护 per-task `seq` 计数器；所有到达事件计入 seq（N10）。
- 每连接携带 lastSeq：补发 `seq > lastSeq` 的缓冲事件，最多 200 条；
  客户端缺口超出缓冲（溢出）时补发后追加一条 `refreshData`（C4）。
- 多客户端各自独立队列（互不干扰），断开自动清理。

用法（路由挂载由 SWP3-C 在 rest_api.py 实装，本模块只提供事件中心）：
    hub = SseHub()
    hub.publish(task_id, "streamChunk", {"stepId": ..., "chunk": ...})   # 同步
    async for event in hub.subscribe(task_id, last_seq=N): ...          # 异步生成器
    SseHub.format_event(event)  # → "data: {json}\n\n"
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import AsyncIterator, Callable, Deque, Dict, Optional, Set


class SseHub:
    """per-task 事件中心：seq 分配、缓冲（有界）、订阅补发与实时广播。"""

    def __init__(self, max_backlog: int = 200) -> None:
        # max_backlog: 每任务事件缓冲上限（C4：补发最多 200 条，溢出发 refreshData）
        self._max_backlog = max_backlog
        self._seq: Dict[str, int] = {}                 # per-task seq 计数器
        self._buffer: Dict[str, Deque[dict]] = {}      # per-task 事件缓冲（有界）
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}  # per-task 订阅队列

    # ── 事件构造 ────────────────────────────────────────────────

    def _make_event(self, task_id: str, command: str, payload: dict) -> dict:
        """构造 data 行 JSON：command/taskId/seq 由 hub 统一注入（契约字段不可被 payload 覆盖）。"""
        seq = self._seq.get(task_id, 0) + 1
        self._seq[task_id] = seq
        event = dict(payload)
        event["command"] = command
        event["taskId"] = task_id
        event["seq"] = seq
        return event

    # ── 发布 ────────────────────────────────────────────────────

    def publish(self, task_id: str, command: str, payload: dict) -> int:
        """发布事件：分配自增 seq、入缓冲、广播给该 task 全部订阅者。返回 seq。"""
        event = self._make_event(task_id, command, payload)
        buf = self._buffer.setdefault(task_id, deque(maxlen=self._max_backlog))
        buf.append(event)
        for queue in list(self._subscribers.get(task_id, ())):
            queue.put_nowait(event)
        return event["seq"]

    # ── 订阅 ────────────────────────────────────────────────────

    async def subscribe(self, task_id: str, last_seq: int = 0,
                        should_skip: Optional[Callable[[dict], bool]] = None
                        ) -> AsyncIterator[dict]:
        """订阅事件流（async 生成器）。

        先补发缓冲中 `seq > last_seq` 的事件（最多 max_backlog 条）；若客户端
        缺口超出缓冲（溢出，C4），补发后追加一条 refreshData 触发前端全量重拉；
        随后实时推送新事件。调用方 aclose()/退出后自动注销订阅。

        should_skip（2026-08-27）：可选过滤回调，**仅补发阶段**对每个事件调用
        （返回 True 跳过）——用于 live 快照已覆盖的事件防首屏重复；实时阶段
        无条件推送（实时事件 seq 与快照 seq 相同，若过滤会全部丢失）。
        """
        queue: asyncio.Queue = asyncio.Queue()
        subscribers = self._subscribers.setdefault(task_id, set())
        subscribers.add(queue)
        try:
            # 补发阶段
            buf = list(self._buffer.get(task_id, ()))
            missed = [e for e in buf if e["seq"] > last_seq]
            overflow = bool(buf) and len(buf) == self._max_backlog and buf[0]["seq"] > last_seq + 1
            for event in missed:
                if should_skip and should_skip(event):
                    continue
                yield event
            if overflow:
                yield self._make_event(task_id, "refreshData", {})
            # 实时阶段（不过滤——快照只覆盖补发，实时事件必须推送）
            while True:
                event = await queue.get()
                yield event
        finally:
            subscribers.discard(queue)

    # ── 序列化（B2：只发 data 行）────────────────────────────────

    @staticmethod
    def format_event(event: dict) -> str:
        """SSE 文本：只含 `data:` 行（无 `event:` 行），事件类型在 data JSON 的 command 字段。"""
        return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
