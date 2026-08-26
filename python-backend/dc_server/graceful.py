
import asyncio
import logging

logger = logging.getLogger("DC:graceful")

class _GracefulManager:
    def __init__(self) -> None:
        self._draining = False
        self._action = ""
        self._active_cmds = 0

    def request(self, action: str) -> None:
        self._action = action
        self._draining = bool(action)
        logger.info(f"[DC:graceful] drain {'requested' if action else 'cancelled'} (action={action})")

    def is_draining(self) -> bool:
        return self._draining

    @property
    def action(self) -> str:
        return self._action

    def add_cmd(self) -> None:
        self._active_cmds += 1

    def done_cmd(self) -> None:
        if self._active_cmds > 0:
            self._active_cmds -= 1
        if self._active_cmds == 0 and self._draining:
            logger.info("[DC:graceful] all commands finished — ready to restart")

    @property
    def active_cmds(self) -> int:
        return self._active_cmds

    async def wait_idle(self) -> None:
        while self._active_cmds > 0:
            await asyncio.sleep(0.2)

graceful = _GracefulManager()
