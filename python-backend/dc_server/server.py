
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

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/html", ".html")

from . import config
from .rest_api import rest_app, _recover_stale_tasks, _auto_resume_tasks, _graceful_shutdown

logger = logging.getLogger(__name__)

FRONTEND_DIST = os.path.abspath(os.path.join(config.BASE_DIR, "..", "frontend", "dist"))

def _setup_static() -> None:
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
        if full_path.startswith("api/") or full_path.startswith("sse"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = os.path.abspath(os.path.join(dist, full_path))
        norm_candidate = os.path.normcase(candidate)
        norm_root = os.path.normcase(os.path.abspath(dist))
        if not (
            norm_candidate == norm_root
            or norm_candidate.startswith(norm_root + os.sep)
        ):
            raise HTTPException(status_code=404, detail="not found")
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(
            os.path.join(dist, "index.html"), headers={"Cache-Control": "no-cache"}
        )

_setup_static()

def _local_ipv4s() -> list:
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
        shutil.rmtree(dc_tmp, ignore_errors=True)
    logger.info(f"启动清理 .dc_tmp 残留: {dc_tmp}（保留 {kept} 个进行中任务目录）")

async def _run_server() -> None:
    import signal

    import uvicorn

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

    try:
        recovered = await _recover_stale_tasks()
        if recovered:
            logger.info(f"[DC:recover] 启动恢复完成，{recovered} 个残留 active 步骤已置 pending")
    except Exception:
        logger.exception("启动恢复 _recover_stale_tasks 失败（不影响启动）")

    try:
        resumed = await _auto_resume_tasks()
        if resumed:
            logger.info(f"[DC:recover] {resumed} 个任务已自动恢复执行")
    except Exception:
        logger.exception("自动恢复任务执行 _auto_resume_tasks 失败（不影响启动）")

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
            logger.warning(f"SIGINT handler registration skipped: {e}")
        try:
            loop.add_signal_handler(signal.SIGTERM, _make_signal_handler("SIGTERM"))
            logger.info("SIGTERM handler registered (graceful shutdown)")
        except (NotImplementedError, ValueError) as e:
            logger.warning(f"SIGTERM handler registration skipped (Windows): {e}")
    except Exception as e:
        logger.warning(f"signal handler setup failed: {e}")

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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        logger.error("uvicorn 未安装。请运行: pip install uvicorn fastapi")
        sys.exit(1)

    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    os.makedirs(config.PROJECT_ROOT, exist_ok=True)

    cleanup_dc_tmp()

    print(f"DIMENSIONCODING_PORT:{config.PORT}", flush=True)
    logger.info(f"REST API starting on http://{config.HOST}:{config.PORT}")

    for ip in _local_ipv4s():
        logger.info(f"访问地址 http://{ip}:{config.PORT}")

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        logger.info("SIGINT received — server stopped cleanly")

if __name__ == "__main__":
    main()
