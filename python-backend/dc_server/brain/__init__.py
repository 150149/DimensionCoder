"""
dc_server.brain — 大脑层（v3.0，原 TS Orchestrator Python 化）

本包当前提供（SWP3-A）：
- llm_client.py  OpenAI 兼容异步客户端（流式 + 工具调用）
- sse_hub.py     SSE 事件中心（seq/lastSeq/补发/溢出）

其余模块由后续 SWP 提供：orchestrator（SWP3-B1）、planner/tasks（SWP3-B2）。
"""
