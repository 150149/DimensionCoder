from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx

from .. import config

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
TOOL_CACHE_TTL = 60.0
CALL_TIMEOUT = 60.0
INIT_TIMEOUT = 30.0


class McpError(Exception):
    pass


def _to_openai_tool(server: str, t: dict) -> dict:
    name = f"mcp__{server}__{t.get('name', '')}"
    desc = str(t.get("description") or "")[:500]
    schema = t.get("inputSchema") or {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc or f"（MCP 工具，来自 {server}）",
            "parameters": {
                "type": "object",
                "properties": schema.get("properties") or {},
                "required": required,
            },
        },
    }


def _parse_sse_body(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    result: dict = {}
    for line in stripped.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict):
                result = data
    return result


class McpClient:

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self._req_id = 0
        self._initialized = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._session_id: Optional[str] = None
        self._tools: list = []
        self._tools_at = 0.0
        self._lock = asyncio.Lock()


    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _ensure(self) -> None:
        if self._initialized:
            if self._proc is not None and self._proc.returncode is not None:
                logger.warning(f"MCP[{self.name}]: stdio 子进程已退出，重启并重握手")
                await self._teardown()
            else:
                return
        await self._handshake()

    async def _teardown(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.returncode is None:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        self._proc.kill()
            except (ProcessLookupError, OSError):
                pass
            self._proc = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None
        self._initialized = False
        self._session_id = None
        self._tools = []
        self._tools_at = 0.0

    async def _handshake(self) -> None:
        if self.cfg["type"] == "stdio":
            env = dict(os.environ)
            env.update(self.cfg.get("env") or {})
            self._proc = await asyncio.create_subprocess_exec(
                self.cfg["command"], *self.cfg.get("args", []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        else:
            self._http = httpx.AsyncClient()
        await self._request_raw("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dimensioncoder", "version": "1.0"},
        }, timeout=INIT_TIMEOUT)
        try:
            await self._notify("notifications/initialized", {})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"MCP[{self.name}]: initialized 通知发送失败（忽略）: {e}")
        self._initialized = True
        logger.info(f"MCP[{self.name}]: 握手成功（{self.cfg['type']}）")

    async def _notify(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._proc is not None:
            self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        elif self._http is not None:
            headers = {"Accept": "application/json, text/event-stream",
                       "Content-Type": "application/json"}
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            resp = await self._http.post(self.cfg["url"], json=msg, headers=headers,
                                         timeout=INIT_TIMEOUT)
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            if resp.status_code >= 400:
                raise McpError(f"{method}: HTTP {resp.status_code}")

    async def _request_raw(self, method: str, params: dict, timeout: float) -> dict:
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self._proc is not None:
            if self._proc.returncode is not None:
                raise McpError("stdio 子进程已退出")
            self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            while True:
                try:
                    line = await asyncio.wait_for(self._proc.stdout.readline(), timeout)
                except asyncio.TimeoutError:
                    raise McpError(f"{method}: 等待响应超时（{int(timeout)}s）")
                if not line:
                    raise McpError("stdio 子进程已终止（stdout EOF）")
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if data.get("id") != rid:
                    continue
                if "error" in data:
                    raise McpError(f"{method}: {data['error']}")
                return data.get("result") or {}
        if self._http is not None:
            headers = {"Accept": "application/json, text/event-stream",
                       "Content-Type": "application/json"}
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            try:
                resp = await self._http.post(self.cfg["url"], json=msg, headers=headers,
                                             timeout=timeout)
            except httpx.HTTPError as e:
                raise McpError(f"{method}: 网络错误 {e}")
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            if resp.status_code >= 400:
                raise McpError(f"{method}: HTTP {resp.status_code}")
            try:
                data = _parse_sse_body(resp.text)
            except (json.JSONDecodeError, ValueError) as e:
                raise McpError(f"{method}: 响应体解析失败 {e}")
            if not data:
                raise McpError(f"{method}: 空响应")
            if "error" in data:
                raise McpError(f"{method}: {data['error']}")
            return data.get("result") or {}
        raise McpError("传输未初始化")

    async def _request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> dict:
        async with self._lock:
            await self._ensure()
            try:
                return await self._request_raw(method, params, timeout)
            except McpError:
                raise


    async def list_tools(self) -> list:
        now = time.monotonic()
        if self._initialized and self._tools and now - self._tools_at < TOOL_CACHE_TTL:
            return self._tools
        result = await self._request("tools/list", {}, timeout=INIT_TIMEOUT)
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            tools = []
        self._tools = tools
        self._tools_at = now
        return tools

    async def call_tool(self, tool: str, args: dict) -> str:
        result = await self._request("tools/call", {"name": tool, "arguments": args or {}})
        parts = []
        for c in result.get("content") or []:
            if not isinstance(c, dict):
                parts.append(str(c))
            elif c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif c.get("type") == "json":
                parts.append(json.dumps(c.get("json"), ensure_ascii=False))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        text = "\n".join(parts)[:8000]
        if result.get("isError"):
            return f"[Error] MCP 工具执行失败: {text}"
        return text or "（空返回）"

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()


class McpManager:

    def __init__(self):
        self._clients: dict[str, McpClient] = {}
        self._sig: Optional[str] = None

    @staticmethod
    def _sig_of(servers: list) -> str:
        return json.dumps(servers, sort_keys=True, ensure_ascii=False)

    async def _sync(self) -> None:
        servers = [s for s in config.get_mcp_config() if s.get("enabled")]
        sig = self._sig_of(servers)
        if sig == self._sig:
            return
        new_map = {s["name"]: s for s in servers}
        for name, client in list(self._clients.items()):
            if name not in new_map or self._sig_of(client.cfg) != self._sig_of(new_map[name]):
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                del self._clients[name]
        for name, s in new_map.items():
            if name not in self._clients:
                self._clients[name] = McpClient(s)
        self._sig = sig

    async def openai_tools(self) -> list:
        try:
            await self._sync()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MCP: 配置同步失败: {e}")
            return []
        tools: list = []
        for name, client in self._clients.items():
            try:
                for t in await client.list_tools():
                    if isinstance(t, dict) and t.get("name"):
                        tools.append(_to_openai_tool(name, t))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"MCP[{name}]: 拉取工具列表失败: {e}")
        return tools

    async def call(self, server: str, tool: str, args: dict) -> str:
        await self._sync()
        client = self._clients.get(server)
        if client is None:
            raise McpError(f"未找到已启用的 MCP server: {server}")
        return await client.call_tool(tool, args)

    async def test(self, cfg: dict) -> list:
        client = McpClient(cfg)
        try:
            return await client.list_tools()
        finally:
            await client.close()

    async def close_all(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients = {}
        self._sig = None


mcp_manager = McpManager()
