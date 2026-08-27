"""
dc_server.brain.llm_client — LLM 客户端（OpenAI 兼容 + Gemini 原生协议）

- 异步流式（OpenAI messages 格式，tools 可选）；模型名以 gemini- 开头自动
  走 Gemini 原生协议（generateContent，2026-08-20：New API 等网关
  的 gemini 模型不支持 OpenAI 兼容转换，openai 格式请求报 contents is
  required，实测确认）。
- 产出两种事件 dict：
    {"type": "text", "text": str}
    {"type": "tool_call", "id", "name", "arguments"}  # arguments = 分片拼合后的完整 JSON 字符串
    {"type": "usage", "prompt", "cached", "completion"}  # 流末尾 usage chunk（include_usage 开启时）
- **不做任何重试**（429 退避重试在 orchestrator 层，B1）。
- 非 2xx 抛 LlmError(status, message)；401/403 message 强制带
  「请检查设置页 LLM 配置」前缀（V-L2 auth_error 规范）。
- signal（asyncio.Event）置位 → 抛 LlmAborted 中断（T3.2 abort 判据）。
- 流式空闲超时默认 120s（问题 11），超时抛 LlmError(status=0)。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Dict, List, Optional

import httpx
import openai


class LlmError(Exception):
    """LLM 调用失败。status: HTTP 状态码（本地错误如空闲超时用 0）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"LLM error (status={status}): {message}")
        self.status = status
        self.message = message


class LlmAborted(Exception):
    """调用方通过 signal（asyncio.Event）请求中止流式响应。"""


class LlmClient:
    def __init__(self, cfg: dict, idle_timeout: float = 180.0,
                 total_timeout: float = 600.0) -> None:
        # cfg: {base_url, api_key, model}
        # 2026-08-19：覆盖默认 UA（AsyncOpenAI/Python x.y）——实测 New API 网关
        # 等 New API 网关对 `AsyncOpenAI/Python` UA 的请求直接 403
        # （bad_response_status_code，反滥用屏蔽）；伪装成普通 httpx 库 UA 后
        # 同请求 200。SDK default_headers 在 default_headers property 中最后
        # 合并（**self._custom_headers），可覆盖固定 UA。
        self._client = openai.AsyncOpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=0,
            # 2026-08-23（用户反馈 cr-r1 round=35：秒级 APITimeoutError）：
            # SDK 默认 connect=5s 太激进——网关繁忙/排队时 TCP 连接 5 秒未建立
            # 即抛 ConnectTimeout → 频繁 network 重试（日志刷屏/延迟叠加）；
            # 放宽 connect 至 60s；read 保持 600s（流式内由 idle_timeout 180s /
            # total_timeout 600s 管理，不受此影响）
            timeout=httpx.Timeout(timeout=600.0, connect=60.0),
            default_headers={"User-Agent": "python-httpx/0.27.2"})
        self._model = cfg["model"]
        self._base_url = cfg["base_url"]  # Gemini 原生分支用（httpx 直连，不走 openai SDK）
        self._api_key = cfg["api_key"]
        self._idle_timeout = idle_timeout  # 流式空闲超时秒数（默认 180s——2026-08-21 复现
        # e726f3e6 step-15 空闲超时：120s 对慢网关/大上下文太激进，grok 首 chunk 可超 60s）
        self._total_timeout = total_timeout  # 流式总时长超时秒数（默认 600s）

        # 防挂死兜底：DeepSeek 对超大上下文可静默数小时且期间持续发空增量/keepalive
        # → 单次 anext 永不超时（120s 空闲超时失效）→ 执行循环无限挂起（实测 6 小时）。
        # 每次 anext 用剩余总时长封顶：即使每块都有数据，超过 total_timeout 也抛错
        # （status=0 空闲类错误 → orchestrator 不重试、置 stopped 等人工）

    async def close(self) -> None:
        """显式关闭底层 HTTP 客户端（释放 asyncio transport）。生产进程生命周期内
        可省略；测试 teardown 必须调用——否则 Proactor pipe transport 在 event loop
        关闭后析构触发 PytestUnraisableExceptionWarning（Windows 实测）。"""
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _error_message(exc: openai.APIStatusError) -> str:
        """错误文案：auth 类（401/403）强制前缀「请检查设置页 LLM 配置」（V-L2）；
        New API 通道 model_not_found（模型不在该 Key 的分组/渠道）→ 明确中文提示。"""
        raw = getattr(exc, "message", None) or str(exc)
        if exc.status_code in (401, 403):
            return f"请检查设置页 LLM 配置：{raw}"
        # New API/One API：{error:{message, type:"model_not_found"}}——
        # 模型名不在该 Key 可用的分组/渠道（通道后台问题，非本机配置问题）。
        # 注意：openai SDK 对 APIStatusError.body 已剥掉 error 外层（实测流式
        # 404 的 body 直接是 {message,type,param,code}），两种形态都要兼容。
        body = None
        try:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                err = body.get("error") if isinstance(body.get("error"), dict) else body
                if str(err.get("type") or "") == "model_not_found":
                    detail = err.get("message") or raw
                    return (f"模型不被该通道可用（{detail}）。请在 New API 后台检查："
                            f"① 渠道/账号是否启用且有余额；② 模型重定向是否配置；"
                            f"③ 该 Key 所属分组是否绑定了该模型对应的渠道")
        except Exception:
            pass
        return raw

    async def stream_chat(self, messages: list[dict], tools: Optional[list[dict]],
                          signal: Optional[asyncio.Event]) -> AsyncIterator[dict]:
        # 产出 {"type":"text","text":str} | {"type":"tool_call","id","name","arguments"}
        #       | {"type":"usage","prompt","cached","completion"}（流末尾 usage chunk）
        # 实现按 §3.6 旧规则 1-6（OpenAI messages 格式、tool 配对由 orchestrator 重建）
        # 不做任何重试（B1）；非 2xx 抛 LlmError(status, message)
        # 2026-08-20：gemini- 前缀模型走 Gemini 原生协议分支（网关不支持
        # OpenAI 兼容转换——openai 格式请求实测报 contents is required）；
        # claude- 前缀走 Anthropic Messages API（x-api-key + anthropic-version）
        if self._is_gemini(self._model):
            async for ev in self._stream_gemini(messages, tools, signal):
                yield ev
            return
        if self._is_claude(self._model):
            async for ev in self._stream_anthropic(messages, tools, signal):
                yield ev
            return
        if signal is not None and signal.is_set():
            raise LlmAborted()
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                stream=True,
                # Token 展示：流末尾追加 usage chunk（include_usage），
                # 否则 OpenAI 兼容接口流式响应不返回 token 用量
                stream_options={"include_usage": True},
            )
        except openai.APIStatusError as exc:
            raise LlmError(exc.status_code, self._error_message(exc)) from None
        except (LlmError, LlmAborted):
            raise
        except Exception as exc:
            # 网络层错误（httpx.ReadError / openai.APIConnectionError 等）→ 统一转
            # LlmError（retryable），否则异常冒泡导致执行循环 crash
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        iterator = stream.__aiter__()
        tool_slots: Dict[int, dict] = {}  # index -> {id, name, arguments: [分片]}
        t0 = time.monotonic()  # 总时长计时起点（每次 anext 用剩余时间封顶）
        try:
            while True:
                # abort：每次迭代前检查 signal（T3.2 abort 判据）
                if signal is not None and signal.is_set():
                    raise LlmAborted()
                # 总时长兜底：剩余时间 ≤0 → 抛空闲类错误（不重试、置 stopped）
                elapsed = time.monotonic() - t0
                remaining = self._total_timeout - elapsed
                if remaining <= 0:
                    raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）")
                try:
                    if signal is not None:
                        # 2026-08-21（复现 e726f3e6 15:01）：abort 后阻塞中的 anext
                        # 不响应 signal——wait_for 只等 anext 超时，用户打断后旧执行
                        # 循环白等空闲超时（120s+）。双路等待：anext 任务 vs signal
                        # 等待，先到者胜；signal 先到 → 取消 anext → 立即 LlmAborted
                        anext_task = asyncio.create_task(iterator.__anext__())
                        sig_task = asyncio.create_task(signal.wait())
                        done, pending = await asyncio.wait(
                            [anext_task, sig_task],
                            timeout=min(self._idle_timeout, remaining),
                            return_when=asyncio.FIRST_COMPLETED)
                        for t in pending:
                            t.cancel()
                        if pending:
                            # 回收取消的任务（避免 GC 时 unraisable 噪音）
                            await asyncio.gather(*pending, return_exceptions=True)
                        if anext_task in done:
                            chunk = anext_task.result()
                        elif sig_task in done:
                            raise LlmAborted()
                        else:
                            # asyncio.wait 超时（idle_timeout 无数据 / total_timeout
                            # 耗尽）→ 与 signal=None 分支同语义抛 LlmError（含「超时」
                            # → orchestrator 归 network retryable 自动退避重试）；
                            # 此前误抛 LlmAborted——空消息（str(LlmAborted())==""）被
                            # 分类 unknown 不重试直接停步骤（2026-08-23 DB 实证
                            # 10092ff1 step-2：网关 200 OK 后 180s 无数据被误停）
                            if time.monotonic() - t0 >= self._total_timeout:
                                raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                            raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                    else:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(), timeout=min(self._idle_timeout, remaining))
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # 空闲超时（120s 无数据）或总时长耗尽（keepalive 拖满 total）
                    if time.monotonic() - t0 >= self._total_timeout:
                        raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                    raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                except (LlmError, LlmAborted):
                    raise
                except Exception as exc:
                    # 流式中断（httpx/httpcore ReadError、连接重置等）→ 转 LlmError
                    # （retryable 由 _classify_error 判定），不再让执行循环 crash
                    raise LlmError(0, f"LLM 流式响应中断：{type(exc).__name__}: {exc}") from None
                # usage chunk 判定以 usage 字段为准（不能只看 choices 空）：
                # OpenAI 标准形态是 choices=[] + usage；DeepSeek 实测形态是
                # choices=[{delta:{content:""}, finish_reason:"stop"}] + usage——
                # 只按 choices 空判定会漏掉 DeepSeek 的 usage（此前导致
                # token 统计全 0：_add_step_tokens 从未执行）
                usage = getattr(chunk, "usage", None)
                if usage is not None and getattr(usage, "prompt_tokens", None) is not None:
                    # openai SDK 的 CompletionUsage extra='allow'：prompt_tokens_details
                    # 以原始 dict 形式保留（旧 SDK 无该字段 → None）；兼容 dict/对象两种形态
                    details = getattr(usage, "prompt_tokens_details", None) or {}
                    cached = details.get("cached_tokens") if isinstance(details, dict) \
                        else getattr(details, "cached_tokens", 0)
                    yield {"type": "usage", "prompt": usage.prompt_tokens,
                           "cached": cached or 0,
                           "completion": getattr(usage, "completion_tokens", 0) or 0}
                    continue
                if not chunk.choices:
                    # 无 usage 的空 choices chunk（OpenAI 标准流末尾形态）
                    continue
                delta = chunk.choices[0].delta
                # 思考过程（deepseek reasoning_content，OpenAI 兼容扩展字段）：
                # 独立事件类型，供 UI 流式展示，不参与 tool_calls 组装
                if getattr(delta, "reasoning_content", None):
                    yield {"type": "reasoning", "text": delta.reasoning_content}
                if delta.content:
                    yield {"type": "text", "text": delta.content}
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = tool_slots.setdefault(tc.index, {"id": None, "name": None, "arguments": []})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"].append(tc.function.arguments)
                                # V-18：工具参数 delta 增量事件（前端逐字动画用）；id/name
                                # 一般已就绪（OpenAI delta 顺序：id 先于 arguments），未就绪为 None
                                yield {"type": "tool_call_delta", "index": tc.index,
                                       "id": slot["id"], "name": slot["name"],
                                       "delta": tc.function.arguments}
        except openai.APIStatusError as exc:
            # 流中途非 2xx（如流式限流）
            raise LlmError(exc.status_code, self._error_message(exc)) from None
        finally:
            await stream.close()  # 正常/异常路径都关闭底层流

        # 流结束：按 index 顺序产出拼合后的完整 tool_call（分片拼合）
        for index in sorted(tool_slots):
            slot = tool_slots[index]
            yield {"type": "tool_call", "id": slot["id"], "name": slot["name"],
                   "arguments": "".join(slot["arguments"])}

    # ═══════════════════════════════════════════════════════════════════
    # Gemini 原生协议分支（2026-08-20 实测：New API 等网关
    # 的 gemini 模型只支持 generateContent 原生协议——openai 格式请求
    # /v1/chat/completions 报 500 contents is required；原生端点
    # /v1beta/models/{model}:streamGenerateContent?alt=sse 正常。
    # 事件协议与 openai 分支一致（text/tool_call/usage/reasoning）。
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_gemini(model: str) -> bool:
        """模型名以 gemini- 开头（大小写不敏感）→ 走 Gemini 原生协议。"""
        return str(model or "").strip().lower().startswith("gemini-")

    @staticmethod
    def _gemini_base(base_url: str) -> str:
        """OpenAI 兼容 base_url（https://host/v1 或裸域名）→ Gemini 原生域名
        前缀（https://host/v1beta）。已含 /v1beta 的路径原样保留。"""
        url = (base_url or "").strip().rstrip("/")
        for tail in ("/v1", "/v1beta"):
            if url.endswith(tail):
                return url[: -len(tail)] + "/v1beta"
        return url + "/v1beta"

    @staticmethod
    def _to_gemini_payload(messages: list[dict], tools: Optional[list[dict]]) -> dict:
        """OpenAI messages/tools → Gemini 原生 payload（contents/systemInstruction/tools）。

        - system → systemInstruction（合并全部 system 文本）
        - user/assistant → contents（role: user/model），连续同角色消息合并 parts
          （Gemini 要求 user/model 交替，否则 400）
        - assistant.tool_calls → parts 内 functionCall；紧随的 tool 消息 →
          functionResponse（并入 user content，与 functionCall 按序配对）
        - 空内容消息跳过；孤儿 tool 消息（无对应 functionCall）丢弃
        - tools → [{functionDeclarations: [{name, description, parameters}]}]
          （parameters 为 JSON Schema，与 openai 格式直接透传）
        """
        system_parts: List[dict] = []
        contents: List[dict] = []
        pending_calls: List[tuple] = []  # (call_id, name) 待配对的 functionCall
        # 2026-08-20 修复：Gemini 要求 functionCall 必须紧跟 functionResponse
        # （400：function call turn comes immediately after a user turn or after
        # a function response turn）。中断/重启/旧数据可能遗留悬空 functionCall
        # （assistant 调用了工具但结果未落库）→ 预扫描标记配对，转换时丢弃未配对者
        paired_idx: set = set()
        call_seq: List[int] = []  # 全局 functionCall 序号（预扫描用）
        from collections import deque

        pending_seq = deque()
        for m in messages or []:
            if m.get("role") == "assistant":
                for _ in m.get("tool_calls") or []:
                    call_seq.append(len(call_seq))
                    pending_seq.append(call_seq[-1])
            elif m.get("role") == "tool" and pending_seq:
                paired_idx.add(pending_seq.popleft())

        fc_seq = iter(call_seq)

        def append_content(role: str, parts: list) -> None:
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": list(parts)})

        for m in messages or []:
            role = m.get("role")
            if role == "system":
                text = str(m.get("content") or "").strip()
                if text:
                    system_parts.append({"text": text})
                continue
            if role == "tool":
                content = m.get("content")
                if content is None:
                    content = ""
                if not pending_calls:
                    continue  # 孤儿 tool 消息（无对应 functionCall）丢弃
                call_id, name = pending_calls.pop(0)
                response = content
                if isinstance(content, str):
                    try:
                        response = json.loads(content)
                    except (ValueError, TypeError):
                        response = None  # 非 JSON → 走下方对象包装
                # 2026-08-20 修复：Gemini 要求 functionResponse.response 必须是
                # JSON 对象（网关 Go 字段类型 map[string]interface{}——字符串/
                # 数组实测 500：cannot unmarshal string into ...response）。
                # 非对象结果统一包装 {"result": <值>}（工具结果多为数组/文本）
                if not isinstance(response, dict):
                    response = {"result": response if response is not None else content}
                append_content("user", [{"functionResponse": {"name": name, "response": response}}])
                continue
            # user / assistant
            text = m.get("content")
            parts: List[dict] = []
            if text:
                parts.append({"text": str(text)})
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    seq = next(fc_seq)
                    if seq not in paired_idx:
                        continue  # 悬空 functionCall（无配对 tool 结果）→ 丢弃
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    args = fn.get("arguments") or "{}"
                    if isinstance(args, str):
                        try:
                            args_obj = json.loads(args)
                        except (ValueError, TypeError):
                            args_obj = {}
                    else:
                        args_obj = args or {}
                    parts.append({"functionCall": {"name": name, "args": args_obj}})
                    pending_calls.append((tc.get("id"), name))
            if not parts:
                continue
            append_content("model" if role == "assistant" else "user", parts)

        # 2026-08-20 修复：Gemini 要求 functionCall 轮必须紧跟 user 轮或
        # functionResponse 轮——对话开头/中间出现 functionCall 且前一轮不是
        # user（中断重建数据、system 抽走后开头即 functionCall）→ 补空 user 轮
        # （400：function call turn comes immediately after a user turn or...）
        for i, c in enumerate(contents):
            if c["role"] != "model":
                continue
            if not any("functionCall" in p for p in c.get("parts", [])):
                continue
            prev = contents[i - 1] if i > 0 else None
            if prev is None or prev["role"] != "user":
                contents.insert(i, {"role": "user", "parts": [{"text": " "}]})

        payload: dict = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        decls: List[dict] = []
        for t in tools or []:
            fn = t.get("function") if isinstance(t, dict) else t
            if not isinstance(fn, dict):
                continue
            decl: dict = {"name": fn.get("name") or ""}
            if fn.get("description"):
                decl["description"] = fn["description"]
            if fn.get("parameters"):
                decl["parameters"] = fn["parameters"]
            if decl["name"]:
                decls.append(decl)
        if decls:
            payload["tools"] = [{"functionDeclarations": decls}]
        return payload

    def _gemini_error(self, status: int, text: str) -> LlmError:
        """Gemini 错误体 {error:{message,code,status}} 提取 + auth 前缀（同 openai 分支）。"""
        msg = text[:2000]
        try:
            body = json.loads(text)
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict) and err.get("message"):
                msg = err["message"]
        except (ValueError, TypeError):
            pass
        if status in (401, 403):
            return LlmError(status, f"请检查设置页 LLM 配置：{msg}")
        return LlmError(status, msg)

    async def _stream_gemini(self, messages: list[dict], tools: Optional[list[dict]],
                             signal: Optional[asyncio.Event]) -> AsyncIterator[dict]:
        """Gemini 原生协议流式（streamGenerateContent SSE，alt=sse）。

        与 openai 分支同事件协议：text/reasoning/tool_call/usage。
        - usageMetadata 每个 chunk 都带（累计值）→ 只在流末 yield 一次
          （orchestrator 按轮累加，重复发会 token 统计翻倍）
        - functionCall 每个 chunk 都是完整对象（实测：同名多次调用分多个
          chunk，args 为完整 dict 非增量分片）→ 按序收集，流末统一 yield
        - thoughtSignature part（2.5 思考签名）不带思考文本时忽略；带文本
          时作为 reasoning 事件（与 deepseek reasoning_content 同协议）
        """
        import httpx

        url = (self._gemini_base(self._base_url)
               + f"/models/{self._model}:streamGenerateContent?alt=sse")
        payload = self._to_gemini_payload(messages, tools)
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json",
                   "User-Agent": "python-httpx/0.27.2"}
        if signal is not None and signal.is_set():
            raise LlmAborted()
        tool_calls: List[dict] = []
        last_usage: Optional[dict] = None
        got_output = False  # 有产出（文本/工具调用）→ SAFETY 拒绝不再抛错
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        raise self._gemini_error(resp.status_code, text)
                    lines = resp.aiter_lines()
                    t0 = time.monotonic()  # 总时长计时起点（每次 anext 用剩余时间封顶）
                    while True:
                        # abort：每次迭代前检查 signal（T3.2 abort 判据）
                        if signal is not None and signal.is_set():
                            raise LlmAborted()
                        elapsed = time.monotonic() - t0
                        remaining = self._total_timeout - elapsed
                        if remaining <= 0:
                            raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）")
                        try:
                            line = await asyncio.wait_for(
                                lines.__anext__(), timeout=min(self._idle_timeout, remaining))
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            if time.monotonic() - t0 >= self._total_timeout:
                                raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                            raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                        except httpx.HTTPError as exc:
                            raise LlmError(0, f"LLM 流式响应中断：{type(exc).__name__}: {exc}") from None
                        line = (line or "").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except (ValueError, TypeError):
                            continue
                        if data.get("usageMetadata"):
                            last_usage = data["usageMetadata"]  # 每块累计值，只留最后
                        for cand in data.get("candidates") or []:
                            for part in (cand.get("content") or {}).get("parts") or []:
                                if "functionCall" in part:
                                    fc = part["functionCall"] or {}
                                    name = fc.get("name") or ""
                                    args = fc.get("args")
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except (ValueError, TypeError):
                                            args = {}
                                    tool_calls.append({
                                        "id": f"call_gemini_{len(tool_calls)}",
                                        "name": name,
                                        "arguments": json.dumps(args or {}, ensure_ascii=False)})
                                    got_output = True
                                text = part.get("text")
                                if text and (part.get("thought")
                                            or part.get("thoughtSignature")):
                                    # 思考内容（thoughtSignature part，2.5 思考签名）
                                    # → reasoning 事件（与 deepseek 同协议）
                                    got_output = True
                                    yield {"type": "reasoning", "text": text}
                                elif text:
                                    got_output = True
                                    yield {"type": "text", "text": text}
                            fr = cand.get("finishReason")
                            if fr in ("SAFETY", "RECITATION") and not got_output:
                                raise LlmError(0, f"Gemini 拒绝生成（finishReason={fr}）")
        except (LlmError, LlmAborted):
            raise
        except httpx.HTTPError as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None
        except Exception as exc:
            # 网络层异常统一转 LlmError（retryable），否则异常冒泡导致执行循环 crash
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        # 流末：usage 只发一次（每块累计值，取最后一块）→ 再按序产出完整 tool_call
        if last_usage:
            yield {"type": "usage",
                   "prompt": last_usage.get("promptTokenCount") or 0,
                   "cached": last_usage.get("cachedContentTokenCount") or 0,
                   "completion": (last_usage.get("candidatesTokenCount") or 0)
                                  + (last_usage.get("thoughtsTokenCount") or 0)}
        for tc in tool_calls:
            yield {"type": "tool_call", "id": tc["id"], "name": tc["name"],
                   "arguments": tc["arguments"]}

    # ═══════════════════════════════════════════════════════════════
    # Anthropic Messages API 分支（2026-08-20：claude- 前缀模型）
    # 端点 {base}/v1/messages；x-api-key + anthropic-version header；
    # max_tokens 必填；tools 用 input_schema（JSON Schema 透传）；
    # tool_use 后必须紧跟 tool_result（同 Gemini 严格配对）。
    # 事件协议与 openai 分支一致（text/tool_call/usage/reasoning）。
    # ═══════════════════════════════════════════════════════════════

    _ANTHROPIC_MAX_TOKENS = 8192  # Messages API 必填输出上限

    @staticmethod
    def _is_claude(model: str) -> bool:
        """模型名以 claude- 开头（大小写不敏感）→ 走 Anthropic Messages API。"""
        return str(model or "").strip().lower().startswith("claude-")

    @staticmethod
    def _to_anthropic_payload(messages: list[dict],
                              tools: Optional[list[dict]],
                              max_tokens: int = _ANTHROPIC_MAX_TOKENS) -> dict:
        """OpenAI messages/tools → Anthropic Messages API payload。

        - system → 顶层 system（content blocks 数组，带 cache_control 缓存断点——
          Anthropic 前缀缓存需显式断点，纯字符串形态不支持；2026-08-23）
        - user/assistant → content blocks（text/tool_use）；tool 结果 →
          tool_result block（并入紧随的 user 消息，Anthropic 要求 tool_use
          后必须紧跟 tool_result，否则 400）
        - 连续同角色消息合并（Anthropic 要求 user/assistant 交替）
        - 悬空 tool_use（无配对 tool_result）预扫描丢弃；首条非 user 补空
          user（Anthropic 要求 messages 以 user 开头）
        - tools → [{name, description, input_schema}]（input_schema 透传
          openai parameters，同为 JSON Schema）
        """
        from collections import deque

        system_texts: List[str] = []
        contents: List[dict] = []
        pending: List[tuple] = []  # (tool_use_id, name) 待配对的 tool_use
        # 预扫描配对（同 Gemini 分支：悬空 tool_use 丢弃——中断/旧数据遗留）
        paired_idx: set = set()
        call_seq: List[int] = []
        pending_seq = deque()
        for m in messages or []:
            if m.get("role") == "assistant":
                for _ in m.get("tool_calls") or []:
                    call_seq.append(len(call_seq))
                    pending_seq.append(call_seq[-1])
            elif m.get("role") == "tool" and pending_seq:
                paired_idx.add(pending_seq.popleft())
        fc_seq = iter(call_seq)

        def append_content(role: str, blocks: list) -> None:
            if contents and contents[-1]["role"] == role:
                contents[-1]["content"].extend(blocks)
            else:
                contents.append({"role": role, "content": list(blocks)})

        for m in messages or []:
            role = m.get("role")
            if role == "system":
                text = str(m.get("content") or "").strip()
                if text:
                    system_texts.append(text)
                continue
            if role == "tool":
                if not pending:
                    continue  # 孤儿 tool 消息（无对应 tool_use）丢弃
                tuid, _name = pending.pop(0)
                content = m.get("content")
                if content is None:
                    content = ""
                append_content("user", [{"type": "tool_result",
                                         "tool_use_id": tuid,
                                         "content": str(content)}])
                continue
            # user / assistant
            text = m.get("content")
            blocks: List[dict] = []
            if text:
                blocks.append({"type": "text", "text": str(text)})
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    seq = next(fc_seq)
                    if seq not in paired_idx:
                        continue  # 悬空 tool_use（无配对 tool_result）→ 丢弃
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    args = fn.get("arguments") or "{}"
                    if isinstance(args, str):
                        try:
                            args_obj = json.loads(args)
                        except (ValueError, TypeError):
                            args_obj = {}
                    else:
                        args_obj = args or {}
                    tuid = tc.get("id") or f"toolu_{len(pending)}"
                    blocks.append({"type": "tool_use", "id": tuid,
                                   "name": name, "input": args_obj})
                    pending.append((tuid, name))
            if not blocks:
                continue
            append_content("model" if role == "assistant" else "user", blocks)

        # Anthropic 要求 messages 非空且第一条是 user——开头即 assistant（中断重建
        # 数据、system 抽走后）或全空（悬空 tool_use 全被丢弃）→ 补空 user
        # （400：first message must use the user role / messages must not be empty）
        if not contents or contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "content": [{"type": "text", "text": " "}]})

        payload: dict = {"model": "", "max_tokens": max_tokens,
                         "messages": contents}
        if system_texts:
            # 2026-08-23：Anthropic 前缀缓存需显式 cache_control 断点（claude- 模型
            # 此前 system 为纯字符串 → 吃不到官方缓存，输入全价）；system 稳定（
            # system/user 分离）→ 缓存可命中，缓存读取输入成本 -90%。system 用
            # content blocks 数组携带断点（官方格式），纯字符串不支持该字段。
            payload["system"] = [{"type": "text", "text": "\n\n".join(system_texts),
                                  "cache_control": {"type": "ephemeral"}}]
        decls: List[dict] = []
        for t in tools or []:
            fn = t.get("function") if isinstance(t, dict) else t
            if not isinstance(fn, dict):
                continue
            decl: dict = {"name": fn.get("name") or ""}
            if fn.get("description"):
                decl["description"] = fn["description"]
            if fn.get("parameters"):
                decl["input_schema"] = fn["parameters"]
            if decl["name"]:
                decls.append(decl)
        if decls:
            payload["tools"] = decls
        return payload

    def _anthropic_error(self, status: int, text: str) -> LlmError:
        """Anthropic 错误体 {type,error:{type,message}} 提取 + auth 前缀。"""
        msg = text[:2000]
        try:
            body = json.loads(text)
            if isinstance(body, dict):
                err = body.get("error") if isinstance(body.get("error"), dict) else body
                if err.get("message"):
                    msg = err["message"]
        except (ValueError, TypeError):
            pass
        if status in (401, 403):
            return LlmError(status, f"请检查设置页 LLM 配置：{msg}")
        return LlmError(status, msg)

    async def _stream_anthropic(self, messages: list[dict],
                                tools: Optional[list[dict]],
                                signal: Optional[asyncio.Event]) -> AsyncIterator[dict]:
        """Anthropic Messages API 流式（SSE）。

        事件协议与 openai/gemini 分支一致：text/reasoning/tool_call/usage。
        - usage：input_tokens 在 message_start（含 cache_read_input_tokens）；
          output_tokens 在 message_delta（增量）→ 累加；流末 yield 一次
        - tool_use input 是 input_json_delta 分片（partial_json 字符串拼接，
          同 openai arguments 分片模式）
        - thinking_delta → reasoning 事件（thinking blocks）
        """
        import httpx

        url = self._base_url.rstrip("/") + "/messages"
        payload = self._to_anthropic_payload(messages, tools)
        payload["model"] = self._model
        # 2026-08-20 修复：漏传 stream=True → 网关返回一次性 JSON（非 SSE）
        # → 行解析全跳过（真机实测：e2e 零事件）
        payload["stream"] = True
        headers = {"x-api-key": self._api_key,
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json",
                   "User-Agent": "python-httpx/0.27.2"}
        if signal is not None and signal.is_set():
            raise LlmAborted()
        tool_blocks: Dict[int, dict] = {}  # index -> {id, name, args: [分片]}
        tool_order: List[int] = []  # content_block_stop 完成顺序
        usage_input = usage_cached = usage_output = 0
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        raise self._anthropic_error(resp.status_code, text)
                    lines = resp.aiter_lines()
                    cur_event = ""  # 上一行 event: xxx（真机 message_start 无顶层 type，需兜底）
                    t0 = time.monotonic()
                    while True:
                        if signal is not None and signal.is_set():
                            raise LlmAborted()
                        elapsed = time.monotonic() - t0
                        remaining = self._total_timeout - elapsed
                        if remaining <= 0:
                            raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）")
                        try:
                            line = await asyncio.wait_for(
                                lines.__anext__(), timeout=min(self._idle_timeout, remaining))
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            if time.monotonic() - t0 >= self._total_timeout:
                                raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                            raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                        except httpx.HTTPError as exc:
                            raise LlmError(0, f"LLM 流式响应中断：{type(exc).__name__}: {exc}") from None
                        line = (line or "").strip()
                        if not line:
                            continue
                        if line.startswith("event:"):
                            cur_event = line[len("event:"):].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except (ValueError, TypeError):
                            continue
                        ev_type = data.get("type") or cur_event  # 真机 message_start 无 type
                        if ev_type == "error":
                            err = data.get("error") or {}
                            raise LlmError(0, str(err.get("message") or err))
                        if ev_type == "message_start":
                            msg = data.get("message") or {}
                            usage = msg.get("usage") or {}
                            usage_input = usage.get("input_tokens") or 0
                            usage_cached = usage.get("cache_read_input_tokens") or 0
                        elif ev_type == "content_block_start":
                            cb = data.get("content_block") or {}
                            if cb.get("type") == "tool_use":
                                idx = data.get("index", len(tool_blocks))
                                tool_blocks[idx] = {"id": cb.get("id"),
                                                    "name": cb.get("name"),
                                                    "args": []}
                                if cb.get("input"):
                                    tool_blocks[idx]["args"].append(
                                        json.dumps(cb["input"], ensure_ascii=False))
                        elif ev_type == "content_block_delta":
                            delta = data.get("delta") or {}
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                yield {"type": "text", "text": delta.get("text") or ""}
                            elif dtype == "thinking_delta":
                                # 思考内容（thinking blocks）→ reasoning 事件
                                yield {"type": "reasoning",
                                       "text": delta.get("thinking") or ""}
                            elif dtype == "input_json_delta":
                                tb = tool_blocks.get(data.get("index"))
                                if tb:
                                    tb["args"].append(delta.get("partial_json") or "")
                        elif ev_type == "content_block_stop":
                            idx = data.get("index")
                            if idx is not None and idx in tool_blocks:
                                tool_order.append(idx)
                        elif ev_type == "message_delta":
                            usage = data.get("usage") or {}
                            # 2026-08-20：官方规范 output_tokens 为增量，但真机网关
                            # 实测给累计值（单条 53=总量）→ 取 max 兼容两种形态
                            # （累计取最大正确；增量形态不翻倍，略偏小可接受）
                            out = usage.get("output_tokens") or 0
                            if out > usage_output:
                                usage_output = out
        except (LlmError, LlmAborted):
            raise
        except httpx.HTTPError as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None
        except Exception as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        # 流末：usage 一次 + 按完成顺序产出完整 tool_call（分片拼接）
        if usage_input or usage_output:
            yield {"type": "usage", "prompt": usage_input,
                   "cached": usage_cached, "completion": usage_output}
        for idx in sorted(tool_order):
            tb = tool_blocks[idx]
            yield {"type": "tool_call", "id": tb["id"], "name": tb["name"],
                   "arguments": "".join(tb["args"])}
