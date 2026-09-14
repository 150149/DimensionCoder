from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time

logger = logging.getLogger(__name__)

SLOW_DB_MS = 200.0
LOOP_LAG_MS = 500.0
WATCHDOG_INTERVAL_S = 0.2
CHUNK_RETENTION_EVERY_S = 600.0


def _slow_db_logged(name: str, fn):

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await fn(self, *args, **kwargs)
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            if dt_ms >= SLOW_DB_MS:
                logger.warning(
                    "[DC:db-slow] %s 耗时 %.0f ms（阈值 %.0f ms）——同步 sqlite 跑在事件循环里，"
                    "这段时间全站无响应", name, dt_ms, SLOW_DB_MS)

    wrapper._slow_db_logged = True
    return wrapper


def install_slow_db_logging(cls):
    for name, fn in list(vars(cls).items()):
        if name.startswith("_") or not inspect.iscoroutinefunction(fn):
            continue
        if getattr(fn, "_slow_db_logged", False):
            continue
        setattr(cls, name, _slow_db_logged(name, fn))
    return cls


async def loop_lag_watchdog(interval: float = WATCHDOG_INTERVAL_S,
                            threshold_ms: float | None = None) -> None:
    limit = LOOP_LAG_MS if threshold_ms is None else threshold_ms
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag_ms = (time.perf_counter() - t0 - interval) * 1000.0
        if lag_ms >= limit:
            logger.warning(
                "[DC:loop-lag] 事件循环被同步调用堵住 %.0f ms（阈值 %.0f ms）——期间全站无响应；"
                "对照同时段 [DC:db-slow] 可定位到具体调用", lag_ms, limit)


async def chunk_retention_loop(storage, every_s: float = CHUNK_RETENTION_EVERY_S) -> None:
    while True:
        try:
            deleted = await storage.purge_stale_chunks()
            if deleted:
                logger.info("[DC:chunk-retention] 清理已结束步骤的 stream_chunks %d 行", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[DC:chunk-retention] 清理失败（不影响主流程，下个周期重试）")
        await asyncio.sleep(every_s)
