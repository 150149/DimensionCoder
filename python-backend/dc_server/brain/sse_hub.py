
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import AsyncIterator, Deque, Dict, Set

class SseHub:

    def __init__(self, max_backlog: int = 200) -> None:
        self._max_backlog = max_backlog
        self._seq: Dict[str, int] = {}
        self._buffer: Dict[str, Deque[dict]] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def _make_event(self, task_id: str, command: str, payload: dict) -> dict:
        seq = self._seq.get(task_id, 0) + 1
        self._seq[task_id] = seq
        event = dict(payload)
        event["command"] = command
        event["taskId"] = task_id
        event["seq"] = seq
        return event

    def publish(self, task_id: str, command: str, payload: dict) -> int:
        event = self._make_event(task_id, command, payload)
        buf = self._buffer.setdefault(task_id, deque(maxlen=self._max_backlog))
        buf.append(event)
        for queue in list(self._subscribers.get(task_id, ())):
            queue.put_nowait(event)
        return event["seq"]

    async def subscribe(self, task_id: str, last_seq: int = 0) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue()
        subscribers = self._subscribers.setdefault(task_id, set())
        subscribers.add(queue)
        try:
            buf = list(self._buffer.get(task_id, ()))
            missed = [e for e in buf if e["seq"] > last_seq]
            overflow = bool(buf) and len(buf) == self._max_backlog and buf[0]["seq"] > last_seq + 1
            for event in missed:
                yield event
            if overflow:
                yield self._make_event(task_id, "refreshData", {})
            while True:
                event = await queue.get()
                yield event
        finally:
            subscribers.discard(queue)

    @staticmethod
    def format_event(event: dict) -> str:
        return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
