"""优雅重启/关闭协调器（GracefulManager）

触发方式：POST /api/admin/graceful-restart（前端侧栏「重启」按钮）。
语义（用户定义的安全点——只挑「执行命令前、可重试但未开始」的场景重启）：
- request() 后进入 draining：不再启动任何新命令（run_cmd 执行前检查拒绝）；
  正在执行的命令不受影响（计数等待其自然完成，不杀进程）；
- 执行循环在迭代顶部检查 draining：当前 active 步骤置回 pending（可重试），
  退出循环——重启后启动恢复逻辑自动续跑；
- 命令计数归零后执行重启（Popen 新进程后 os._exit 旧进程）或纯关闭。

线程模型：add_cmd/done_cmd 均在事件循环线程调用（run_cmd 的 Popen/await 段），
无需加锁；wait_idle 用 0.2s 轮询（不用 asyncio.Event——模块级单例跨多个
事件循环（测试/重启用 asyncio.run）时 Event 的 waiter future 会跨 loop 报错）。
"""

import asyncio
import logging

logger = logging.getLogger("DC:graceful")


class _GracefulManager:
    def __init__(self) -> None:
        self._draining = False
        self._action = ""  # "restart" | "shutdown" | ""（取消）
        self._active_cmds = 0

    # ── 状态 ──
    def request(self, action: str) -> None:
        """请求优雅重启/关闭；action="" 取消 draining（恢复执行）。"""
        self._action = action
        self._draining = bool(action)
        logger.info(f"[DC:graceful] drain {'requested' if action else 'cancelled'} (action={action})")

    def is_draining(self) -> bool:
        return self._draining

    @property
    def action(self) -> str:
        return self._action

    # ── 命令计数（run_cmd 执行期间 +1，结束 -1）──
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
        """等待所有正在执行的命令自然完成（不设超时：正在跑的命令让它跑完）。"""
        while self._active_cmds > 0:
            await asyncio.sleep(0.2)


graceful = _GracefulManager()
