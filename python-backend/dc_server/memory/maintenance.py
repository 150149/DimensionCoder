
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .storage import MemoryStorage

logger = logging.getLogger(__name__)

_TICK_SECONDS = 60
_RETENTION_INTERVAL = 3600
_CONSOLIDATION_RECONCILE_INTERVAL = 300
_MM_REFRESH_INTERVAL = 300

class MaintenanceLoop:

    def __init__(
        self,
        storage: MemoryStorage,
        consolidator=None,
        mental_model_manager=None,
        config: Optional[dict] = None,
    ):
        self.storage = storage
        self.consolidator = consolidator
        self.mental_model_manager = mental_model_manager
        self.config = config or {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_retention: Optional[datetime] = None
        self._last_consolidation: Optional[datetime] = None
        self._last_mm_refresh: Optional[datetime] = None

    def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        try:
            self._task = asyncio.create_task(self._run())
        except RuntimeError:
            logger.warning("No event loop, maintenance loop not started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def run(self):
        await self._run()

    async def _run(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc)

                if self._is_due(self._last_retention, _RETENTION_INTERVAL):
                    self._retention_sweep()
                    self._last_retention = now

                if self._is_due(self._last_consolidation, _CONSOLIDATION_RECONCILE_INTERVAL):
                    await self._consolidation_reconcile()
                    self._last_consolidation = now

                if self._is_due(self._last_mm_refresh, _MM_REFRESH_INTERVAL):
                    await self._mm_refresh_check()
                    self._last_mm_refresh = now

            except Exception as e:
                logger.error(f"Maintenance loop error: {e}", exc_info=True)

            await asyncio.sleep(_TICK_SECONDS)

    def _is_due(self, last: Optional[datetime], interval: int) -> bool:
        if last is None:
            return True
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= interval

    def _retention_sweep(self):
        logger.debug("Retention sweep (no-op for now)")

    async def _consolidation_reconcile(self):
        if not self.consolidator:
            return
        stats = self.storage.get_stats()
        if stats.get("facts", 0) > 0:
            conn = self.storage._get_conn()
            banks = conn.execute("SELECT id FROM memory_banks").fetchall()
            for bank in banks:
                bank_id = bank["id"]
                freshness = self.storage.get_consolidation_freshness(bank_id)
                if freshness.get("pending", 0) > 0:
                    try:
                        await self.consolidator.consolidate(bank_id)
                    except Exception as e:
                        logger.warning(f"Consolidation reconcile failed for bank {bank_id}: {e}")

    async def _mm_refresh_check(self):
        if not self.mental_model_manager:
            return
        conn = self.storage._get_conn()
        banks = conn.execute("SELECT id FROM memory_banks").fetchall()
        for bank in banks:
            bank_id = bank["id"]
            try:
                stale_models = self.mental_model_manager.get_stale_models(bank_id)
                for model in stale_models:
                    try:
                        await self.mental_model_manager.refresh_model(model["id"])
                    except Exception as e:
                        logger.warning(f"MM refresh failed for model {model['id']}: {e}")
            except Exception as e:
                logger.warning(f"MM refresh check failed for bank {bank_id}: {e}")
