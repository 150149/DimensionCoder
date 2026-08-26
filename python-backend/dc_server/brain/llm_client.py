
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Dict, List, Optional

import httpx
import openai

class LlmError(Exception):

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"LLM error (status={status}): {message}")
        self.status = status
        self.message = message

class LlmAborted(Exception):
    pass

class LlmClient:
    def __init__(self, cfg: dict, idle_timeout: float = 180.0,
                 total_timeout: float = 600.0) -> None:
        self._client = openai.AsyncOpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=0,
            timeout=httpx.Timeout(timeout=600.0, connect=60.0),
            default_headers={"User-Agent": "python-httpx/0.27.2"})
        self._model = cfg["model"]
        self._base_url = cfg["base_url"]
        self._api_key = cfg["api_key"]
        self._idle_timeout = idle_timeout
        self._total_timeout = total_timeout

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _error_message(exc: openai.APIStatusError) -> str:
        raw = getattr(exc, "message", None) or str(exc)
        if exc.status_code in (401, 403):
            return f"请检查设置页 LLM 配置：{raw}"
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
                stream_options={"include_usage": True},
            )
        except openai.APIStatusError as exc:
            raise LlmError(exc.status_code, self._error_message(exc)) from None
        except (LlmError, LlmAborted):
            raise
        except Exception as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        iterator = stream.__aiter__()
        tool_slots: Dict[int, dict] = {}
        t0 = time.monotonic()
        try:
            while True:
                if signal is not None and signal.is_set():
                    raise LlmAborted()
                elapsed = time.monotonic() - t0
                remaining = self._total_timeout - elapsed
                if remaining <= 0:
                    raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）")
                try:
                    if signal is not None:
                        anext_task = asyncio.create_task(iterator.__anext__())
                        sig_task = asyncio.create_task(signal.wait())
                        done, pending = await asyncio.wait(
                            [anext_task, sig_task],
                            timeout=min(self._idle_timeout, remaining),
                            return_when=asyncio.FIRST_COMPLETED)
                        for t in pending:
                            t.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        if anext_task in done:
                            chunk = anext_task.result()
                        elif sig_task in done:
                            raise LlmAborted()
                        else:
                            if time.monotonic() - t0 >= self._total_timeout:
                                raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                            raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                    else:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(), timeout=min(self._idle_timeout, remaining))
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if time.monotonic() - t0 >= self._total_timeout:
                        raise LlmError(0, f"LLM 流式响应总时长超时（>{self._total_timeout:g}s）") from None
                    raise LlmError(0, f"LLM 流式响应空闲超时（{self._idle_timeout:g}s 无数据）") from None
                except (LlmError, LlmAborted):
                    raise
                except Exception as exc:
                    raise LlmError(0, f"LLM 流式响应中断：{type(exc).__name__}: {exc}") from None
                usage = getattr(chunk, "usage", None)
                if usage is not None and getattr(usage, "prompt_tokens", None) is not None:
                    details = getattr(usage, "prompt_tokens_details", None) or {}
                    cached = details.get("cached_tokens") if isinstance(details, dict) \
                        else getattr(details, "cached_tokens", 0)
                    yield {"type": "usage", "prompt": usage.prompt_tokens,
                           "cached": cached or 0,
                           "completion": getattr(usage, "completion_tokens", 0) or 0}
                    continue
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
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
                                yield {"type": "tool_call_delta", "index": tc.index,
                                       "id": slot["id"], "name": slot["name"],
                                       "delta": tc.function.arguments}
        except openai.APIStatusError as exc:
            raise LlmError(exc.status_code, self._error_message(exc)) from None
        finally:
            await stream.close()

        for index in sorted(tool_slots):
            slot = tool_slots[index]
            yield {"type": "tool_call", "id": slot["id"], "name": slot["name"],
                   "arguments": "".join(slot["arguments"])}

    @staticmethod
    def _is_gemini(model: str) -> bool:
        return str(model or "").strip().lower().startswith("gemini-")

    @staticmethod
    def _gemini_base(base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        for tail in ("/v1", "/v1beta"):
            if url.endswith(tail):
                return url[: -len(tail)] + "/v1beta"
        return url + "/v1beta"

    @staticmethod
    def _to_gemini_payload(messages: list[dict], tools: Optional[list[dict]]) -> dict:
        system_parts: List[dict] = []
        contents: List[dict] = []
        pending_calls: List[tuple] = []
        paired_idx: set = set()
        call_seq: List[int] = []
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
                    continue
                call_id, name = pending_calls.pop(0)
                response = content
                if isinstance(content, str):
                    try:
                        response = json.loads(content)
                    except (ValueError, TypeError):
                        response = None
                if not isinstance(response, dict):
                    response = {"result": response if response is not None else content}
                append_content("user", [{"functionResponse": {"name": name, "response": response}}])
                continue
            text = m.get("content")
            parts: List[dict] = []
            if text:
                parts.append({"text": str(text)})
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    seq = next(fc_seq)
                    if seq not in paired_idx:
                        continue
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
        got_output = False
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        raise self._gemini_error(resp.status_code, text)
                    lines = resp.aiter_lines()
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
                            last_usage = data["usageMetadata"]
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
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        if last_usage:
            yield {"type": "usage",
                   "prompt": last_usage.get("promptTokenCount") or 0,
                   "cached": last_usage.get("cachedContentTokenCount") or 0,
                   "completion": (last_usage.get("candidatesTokenCount") or 0)
                                  + (last_usage.get("thoughtsTokenCount") or 0)}
        for tc in tool_calls:
            yield {"type": "tool_call", "id": tc["id"], "name": tc["name"],
                   "arguments": tc["arguments"]}

    _ANTHROPIC_MAX_TOKENS = 8192

    @staticmethod
    def _is_claude(model: str) -> bool:
        return str(model or "").strip().lower().startswith("claude-")

    @staticmethod
    def _to_anthropic_payload(messages: list[dict],
                              tools: Optional[list[dict]],
                              max_tokens: int = _ANTHROPIC_MAX_TOKENS) -> dict:
        from collections import deque

        system_texts: List[str] = []
        contents: List[dict] = []
        pending: List[tuple] = []
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
                    continue
                tuid, _name = pending.pop(0)
                content = m.get("content")
                if content is None:
                    content = ""
                append_content("user", [{"type": "tool_result",
                                         "tool_use_id": tuid,
                                         "content": str(content)}])
                continue
            text = m.get("content")
            blocks: List[dict] = []
            if text:
                blocks.append({"type": "text", "text": str(text)})
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    seq = next(fc_seq)
                    if seq not in paired_idx:
                        continue
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

        if not contents or contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "content": [{"type": "text", "text": " "}]})

        payload: dict = {"model": "", "max_tokens": max_tokens,
                         "messages": contents}
        if system_texts:
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
        import httpx

        url = self._base_url.rstrip("/") + "/messages"
        payload = self._to_anthropic_payload(messages, tools)
        payload["model"] = self._model
        payload["stream"] = True
        headers = {"x-api-key": self._api_key,
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json",
                   "User-Agent": "python-httpx/0.27.2"}
        if signal is not None and signal.is_set():
            raise LlmAborted()
        tool_blocks: Dict[int, dict] = {}
        tool_order: List[int] = []
        usage_input = usage_cached = usage_output = 0
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        raise self._anthropic_error(resp.status_code, text)
                    lines = resp.aiter_lines()
                    cur_event = ""
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
                        ev_type = data.get("type") or cur_event
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
                            out = usage.get("output_tokens") or 0
                            if out > usage_output:
                                usage_output = out
        except (LlmError, LlmAborted):
            raise
        except httpx.HTTPError as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None
        except Exception as exc:
            raise LlmError(0, f"LLM 请求失败：{type(exc).__name__}: {exc}") from None

        if usage_input or usage_output:
            yield {"type": "usage", "prompt": usage_input,
                   "cached": usage_cached, "completion": usage_output}
        for idx in sorted(tool_order):
            tb = tool_blocks[idx]
            yield {"type": "tool_call", "id": tb["id"], "name": tb["name"],
                   "arguments": "".join(tb["args"])}
