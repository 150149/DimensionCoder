

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import socket
import sys

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Windows 下 mimetypes 经 winreg 对 .js/.mjs 等推断缺失/错误（text/plain），
# 浏览器严格 MIME 检查会拒绝模块脚本 → 前端白屏（e2e 实测）。
# 必须在 StaticFiles 首次 guess_type 前注册（e2e 修复，生产核心缺陷）。
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/html", ".html")

from . import config
from .rest_api import rest_app, _recover_stale_tasks, _auto_resume_tasks, _graceful_shutdown  # SWP3-C：启动恢复 + 自动续跑 + 优雅退出

logger = logging.getLogger(__name__)

# frontend/dist：vite 构建产物（SWP4-E 产出；B2 阶段不存在 → 静态托管 N/A）
FRONTEND_DIST = os.path.abspath(os.path.join(config.BASE_DIR, "..", "frontend", "dist"))


def _setup_static() -> None:
    """挂载 /assets 静态目录 + SPA fallback（T3.1 步骤 1）。

    - dist 存在：mount /assets + catch-all GET（非 /api、非 /sse）→ index.html
    - dist 不存在：记录 N/A（SWP4-E 产出后由 SWP3-C 补全验证）
    """
    dist = FRONTEND_DIST
    if not os.path.isdir(dist):
        logger.warning(
            f"frontend/dist 不存在（{dist}），静态托管暂不可用（N/A：SWP4-E 产出后补验）"
        )
        return

    assets_dir = os.path.join(dist, "assets")
    rest_app.mount(
        "/assets",
        StaticFiles(directory=assets_dir if os.path.isdir(assets_dir) else dist),
        name="assets",
    )

    @rest_app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # SPA fallback：非 /api、非 /sse 的 GET 返回 index.html
        if full_path.startswith("api/") or full_path.startswith("sse"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = os.path.abspath(os.path.join(dist, full_path))
        # SWP3-A 修复：路径遍历防护——candidate 必须位于 dist 目录内
        # （normcase 归一化兼容 Windows 大小写不敏感路径），越界一律 404
        norm_candidate = os.path.normcase(candidate)
        norm_root = os.path.normcase(os.path.abspath(dist))
        if not (
            norm_candidate == norm_root
            or norm_candidate.startswith(norm_root + os.sep)
        ):
            raise HTTPException(status_code=404, detail="not found")
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        # index.html 必须每次重新校验（no-cache）：vite 产物 bundle 名带 hash 天然防缓存，
        # 但 index.html 本身若被浏览器启发式缓存，会一直引用旧 bundle → 用户看到旧界面
        return FileResponse(
            os.path.join(dist, "index.html"), headers={"Cache-Control": "no-cache"}
        )


# 模块加载时挂载（rest_app 单例；SWP2 测试不 import 本模块，不受影响）
_setup_static()


def _local_ipv4s() -> list:
    """列出本机全部非 internal（非 127.*）IPv4 地址（V-L3，规避 VMware 虚拟网卡）。"""
    ips: set = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        # UDP 探测法补充（不实际发包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                ips.add(ip)
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ips)

def cleanup_dc_tmp() -> None:
    dc_tmp = os.path.join(config.PROJECT_ROOT, ".dc_tmp")
    if not os.path.isdir(dc_tmp):
        return
    live_ids: set[str] = set()
    try:
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('active', 'paused')").fetchall()
            live_ids = {r[0] for r in rows}
        finally:
            conn.close()
    except Exception:
        logger.exception("cleanup_dc_tmp: 查询进行中任务失败，退化为全清")
    import shutil
    kept = 0
    for entry in os.listdir(dc_tmp):
        p = os.path.join(dc_tmp, entry)
        if entry in live_ids:
            kept += 1
            continue
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    if not kept and not os.listdir(dc_tmp):
        # 无进行中任务且已清空 → 删除空目录本身（与原 rmtree 语义一致）
        shutil.rmtree(dc_tmp, ignore_errors=True)
    logger.info(f"启动清理 .dc_tmp 残留: {dc_tmp}（保留 {kept} 个进行中任务目录）")


async def _run_server() -> None:
    """启动 uvicorn 服务器（注册信号处理，支持优雅退出）。"""
    import signal

    import uvicorn

    # 端口预检（V-17）：已有实例在监听时直接退出——不执行启动恢复/自动续跑，
    # 避免第二个实例把运行中步骤置回 pending 打断现有执行（双实例并发事故：
    # 曾因误启第二实例把 step 从 active 置 pending 导致执行循环僵尸化）
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((config.HOST, config.PORT))
    except OSError:
        logger.warning(f"端口 {config.PORT} 已被占用（已有实例在运行）——本实例退出，跳过启动恢复")
        return
    finally:
        probe.close()

    server = uvicorn.Server(
        uvicorn.Config(
            rest_app,
            host=config.HOST,
            port=config.PORT,
            log_level="info",
            access_log=False,
        )
    )

    # T3.7：启动恢复（V1，N16 容错）——与服务器同一事件循环，避免跨循环 DB 连接
    try:
        recovered = await _recover_stale_tasks()
        if recovered:
            logger.info(f"[DC:recover] 启动恢复完成，{recovered} 个残留 active 步骤已置 pending")
    except Exception:
        logger.exception("启动恢复 _recover_stale_tasks 失败（不影响启动）")

    # V-13：重启后自动恢复未完成任务执行（有执行历史的任务自动 start；新建未启动
    # 的全 pending 任务保持 B4 手动启动语义）——避免重启后任务卡在待执行、
    # 用户发消息无执行循环消费（优雅重启闭环）
    try:
        resumed = await _auto_resume_tasks()
        if resumed:
            logger.info(f"[DC:recover] {resumed} 个任务已自动恢复执行")
    except Exception:
        logger.exception("自动恢复任务执行 _auto_resume_tasks 失败（不影响启动）")

    # 2026-08-25（Hindsight 记忆模块 B-8）：后台维护循环（60s tick：retention/
    # consolidation/mental-model 刷新）——enabled 时启动，shutdown 由 close_memory 停
    try:
        from .config import get_memory_config
        if get_memory_config().get("enabled"):
            from .rest_api import _get_memory_storage
            ms = _get_memory_storage()
            if ms is not None:
                from .memory import get_maintenance_loop
                loop_inst = get_maintenance_loop(ms, get_memory_config())
                asyncio.get_running_loop().create_task(loop_inst.run())
                logger.info("[DC:memory] maintenance loop started (60s tick)")
    except Exception:
        logger.exception("记忆维护循环启动失败（不影响启动）")

    # B3/T3.7 优雅退出：先中止 running 任务 + 置 stopped，再停止 uvicorn
    async def _graceful_exit(sig_name: str):
        try:
            await _graceful_shutdown()
        except Exception:
            logger.exception(f"{sig_name} graceful shutdown failed")
        server.handle_exit(sig_name, None)

    def _make_signal_handler(sig_name: str):
        def _handler():
            loop = asyncio.get_running_loop()
            loop.create_task(_graceful_exit(sig_name))
        return _handler

    try:
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, _make_signal_handler("SIGINT"))
            logger.info("SIGINT handler registered (graceful shutdown)")
        except (NotImplementedError, ValueError) as e:
            # Windows asyncio 不支持 add_signal_handler，交给 uvicorn 自身兜底
            logger.warning(f"SIGINT handler registration skipped: {e}")
        # SIGTERM 分支（B3 修订）：Windows 注册 SIGTERM handler 抛 ValueError，
        # 仅捕获性处理，不中断启动
        try:
            loop.add_signal_handler(signal.SIGTERM, _make_signal_handler("SIGTERM"))
            logger.info("SIGTERM handler registered (graceful shutdown)")
        except (NotImplementedError, ValueError) as e:
            logger.warning(f"SIGTERM handler registration skipped (Windows): {e}")
    except Exception as e:
        logger.warning(f"signal handler setup failed: {e}")

    # 优雅重启竞态兜底：旧进程退出与新进程绑定之间端口可能延迟释放（Windows
    # 监听 socket 释放后立即可重用，但新进程启动耗时不定），绑定失败按 1.5s
    # 间隔重试（最多 10 次），避免优雅重启后服务起不来
    for attempt in range(10):
        try:
            await server.serve()
            return
        except OSError as e:
            winerr = getattr(e, "winerror", None)
            if attempt < 9 and (winerr == 10048 or "address already in use" in str(e).lower()):
                logger.warning(f"端口 {config.PORT} 绑定失败（可能优雅重启竞态），{attempt + 1}/10 重试…")
                await asyncio.sleep(1.5)
                server = uvicorn.Server(
                    uvicorn.Config(
                        rest_app,
                        host=config.HOST,
                        port=config.PORT,
                        log_level="info",
                        access_log=False,
                    )
                )
                continue
            raise


def main() -> None:
    """启动服务器（v3.0 单进程：REST + 静态托管 + SSE 同一端口）"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        logger.error("uvicorn 未安装。请运行: pip install uvicorn fastapi")
        sys.exit(1)

    # 启动前确保数据目录与工作区目录存在
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    os.makedirs(config.PROJECT_ROOT, exist_ok=True)

    # T2.6 C2：DB 初始化后清空 .dc_tmp 全部残留（重启清残留判据）
    cleanup_dc_tmp()

    # stdout 打印端口（前端/脚本解析此行；flush=True 防管道块缓冲——B2 修订）
    print(f"DIMENSIONCODING_PORT:{config.PORT}", flush=True)
    logger.info(f"REST API starting on http://{config.HOST}:{config.PORT}")

    # V-L3：打印全部非 internal IPv4 访问地址（规避 VMware 虚拟网卡）
    for ip in _local_ipv4s():
        logger.info(f"访问地址 http://{ip}:{config.PORT}")

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        # Ctrl+C（SIGINT）：uvicorn 已优雅停止，此处仅确认干净退出
        logger.info("SIGINT received — server stopped cleanly")


if __name__ == "__main__":
    main()
