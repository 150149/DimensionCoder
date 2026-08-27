"""
config.py — 统一配置入口（端口/DB/工作区/LLM 配置，v3.0）

优先级：环境变量 > config.json > 默认值。
- PORT/HOST/PROJECT_ROOT：env > config.json > 默认 8501/0.0.0.0/python-backend/workspace
  （C12：port/host 数据来源统一在本模块收敛；V-12：projectRoot 启动时读 config.json，env 优先）
- LLM：LLM_BASE_URL/LLM_API_KEY/LLM_LIGHT_MODEL/LLM_POWER_MODEL 环境变量 > config.json
- LLM 配置每次调用实时读 config.json（V-L7：改 Key/模型后立即生效，无需重启）
- config.json 解析失败回退默认值 + 日志警告（V-L9）
- config.json 读写（§3.1 旧规则）：apiKey 不回传（读取侧仅返回 hasApiKey）、
  空串保留旧值、原子写（临时文件 + os.replace）
- config.json 路径（第 9 轮 M8）：os.path.join(BASE_DIR, "..", "config.json")
  （即 dimensioncoder-web/config.json），BASE_DIR = python-backend/
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # python-backend/
CONFIG_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "config.json"))  # dimensioncoder-web/config.json（M8）

DEFAULT_PORT = 8501
DEFAULT_HOST = "0.0.0.0"  # v3.0 反转 A5：单进程无 Node 中继，必须对外绑定供其他电脑浏览器访问（V-01 修订）
DEFAULT_PROJECT_ROOT = os.path.join(BASE_DIR, "workspace")

DEFAULT_CONTEXT_WINDOW = 1_048_576  # Token 展示：模型上下文窗口总容量（DeepSeek V4 真实 1M 上限；400K 只是压缩触发线，不是窗口上限。config.json contextWindow 可覆盖，设置页可编辑）


def _env(name: str) -> Optional[str]:
    """读环境变量，空串视为未设置。"""
    value = os.environ.get(name)
    return value if value is not None and value != "" else None


def _load_config() -> dict:
    """读 config.json；文件缺失返回空 dict，解析失败回退默认值 + 日志警告（V-L9）。

    utf-8-sig：兼容 Windows 记事本保存产生的 UTF-8 BOM（否则 json 解析失败回退
    默认值 → 用户配置的 Key/BaseURL 全部丢失，LLM 调用回退默认端点）。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json 顶层必须是 JSON 对象")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"config.json 解析失败，回退默认值: {e}（{CONFIG_PATH}）")
        return {}


def _resolve_project_root(value: str) -> str:
    """config.json 的 projectRoot 相对路径按 config.json 所在目录（dimensioncoder-web/）解析。"""
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(os.path.dirname(CONFIG_PATH), expanded))


# ── 启动时静态配置（env > config.json > 默认）──
_cfg = _load_config()

_port_raw = _env("DIMENSIONCODING_PORT") or _cfg.get("port") or DEFAULT_PORT
try:
    PORT = int(_port_raw)
except (TypeError, ValueError):
    logger.warning(f"config: 无效 port={_port_raw!r}，回退默认 {DEFAULT_PORT}")
    PORT = DEFAULT_PORT

HOST = _env("DIMENSIONCODING_HOST") or str(_cfg.get("host") or DEFAULT_HOST)

DB_PATH = _env("DIMENSIONCODING_DB_PATH") or os.path.join(BASE_DIR, "data", "dimensioncoding.db")

_env_project_root = _env("DIMENSIONCODING_PROJECT_ROOT")
if _env_project_root is not None:
    PROJECT_ROOT = _env_project_root
else:
    PROJECT_ROOT = _resolve_project_root(str(_cfg.get("projectRoot") or DEFAULT_PROJECT_ROOT))
    # 复制/迁移健壮性：config.json 里残留的绝对路径可能指向不存在的盘符
    # （如旧机器 E:\，复制项目到新环境后仍指向旧盘）→ 启动 os.makedirs 崩
    # WinError 3。盘符不存在时回退默认 workspace（相对 BASE_DIR，随副本移动）
    drive, _ = os.path.splitdrive(PROJECT_ROOT)
    if drive and not os.path.exists(drive + os.sep):
        logger.warning(f"config: projectRoot={PROJECT_ROOT!r} 所在盘 {drive} 不存在，"
                       f"回退默认 {DEFAULT_PROJECT_ROOT}")
        PROJECT_ROOT = DEFAULT_PROJECT_ROOT

CORS_ORIGINS = [o.strip() for o in os.environ.get("DC_CORS_ORIGINS", "http://localhost:8501,http://127.0.0.1:8501").split(",") if o.strip()]


def get_context_window() -> int:
    """实时读 contextWindow（Token 展示用，config.json 可编辑）；非法值回退默认 400000。"""
    raw = _load_config().get("contextWindow", DEFAULT_CONTEXT_WINDOW)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"config: 无效 contextWindow={raw!r}，回退默认 {DEFAULT_CONTEXT_WINDOW}")
        return DEFAULT_CONTEXT_WINDOW
    return value if value > 0 else DEFAULT_CONTEXT_WINDOW


def get_llm_config() -> dict:
    """实时读 LLM 配置（V-L7：每次调用实时读 config.json，env 优先）。

    返回 {base_url, api_key, light_model, power_model, light_base_url, light_api_key,
    power_base_url, power_api_key, 六项价格, channel_type}；未配置项为空串/0。
    api_key 做 strip（防粘贴带入的空格/换行——尾随空格会导致 openai SDK
    报 APIConnectionError，实测确认）。

    2026-08-19 兼容 newapi_channel_conn（New API 通道）：config.json 新增
    llmChannel 字段（通道导出的 JSON {"_type":"newapi_channel_conn",
    "key":...,"url":...}，dict 或 JSON 字符串均可）→ 自动提取 url/key；
    优先级 env > llmChannel > baseUrl/apiKey 旧字段；base_url 出口统一
    _normalize_base_url（无 /v1 自动补全，New API/One API/DeepSeek 等
    OpenAI 兼容网关 API 路径均为 /v1）。

    2026-08-23 双模型拆分：light/power 独立端点与 Key——新字段 env > config
    新字段（lightBaseUrl 等）> 回退共享旧字段（baseUrl/apiKey，旧配置无缝兼容）；
    六项价格（每 1M token 单价，缺省 0，非法值回退 0）。
    """
    cfg = _load_config()
    # 2026-08-23 data patch：旧版单组 baseUrl/apiKey 自动迁移到 light/power 新字段
    # （读取时一次性写回；幂等——新字段有值后不再触发）
    if _migrate_legacy_llm_config(cfg):
        write_config(cfg)
    ch_url, ch_key, ch_type = _resolve_channel(cfg)
    # 优先级：env > llmChannel（通道） > baseUrl/apiKey 旧字段（通道是显式新配置，覆盖旧字段残留）
    base_url = (_env("LLM_BASE_URL") or ch_url or str(cfg.get("baseUrl") or "")).strip()
    api_key = (_env("LLM_API_KEY") or ch_key or str(cfg.get("apiKey") or "")).strip()
    # 新字段 env > config 新字段 > 回退共享（旧配置无缝兼容；迁移后新字段已继承共享值）
    light_base_url = (_env("LLM_LIGHT_BASE_URL") or str(cfg.get("lightBaseUrl") or "") or base_url).strip()
    light_api_key = (_env("LLM_LIGHT_API_KEY") or str(cfg.get("lightApiKey") or "") or api_key).strip()
    power_base_url = (_env("LLM_POWER_BASE_URL") or str(cfg.get("powerBaseUrl") or "") or base_url).strip()
    power_api_key = (_env("LLM_POWER_API_KEY") or str(cfg.get("powerApiKey") or "") or api_key).strip()
    channel_type = ch_type if (ch_url or ch_key) else ""

    def _price(key: str) -> float:
        v = cfg.get(key, 0)
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {
        "base_url": _normalize_base_url(base_url),
        "api_key": api_key,
        "light_model": (_env("LLM_LIGHT_MODEL") or str(cfg.get("lightModel") or "")).strip(),
        "power_model": (_env("LLM_POWER_MODEL") or str(cfg.get("powerModel") or "")).strip(),
        "light_base_url": _normalize_base_url(light_base_url),
        "light_api_key": light_api_key,
        "power_base_url": _normalize_base_url(power_base_url),
        "power_api_key": power_api_key,
        "light_input_price": _price("lightInputPrice"),
        "light_cached_price": _price("lightCachedPrice"),
        "light_output_price": _price("lightOutputPrice"),
        "power_input_price": _price("powerInputPrice"),
        "power_cached_price": _price("powerCachedPrice"),
        "power_output_price": _price("powerOutputPrice"),
        "channel_type": channel_type,
    }


def _migrate_legacy_llm_config(cfg: dict) -> bool:
    """2026-08-23 data patch：旧版单组 baseUrl/apiKey 自动迁移到 light/power 新字段——
    lightBaseUrl 未配置而 baseUrl 存在 → lightBaseUrl=baseUrl（power 同理；apiKey 同理）。
    幂等（迁移后新字段有值不再触发）；env 不参与（只迁移文件字段）。返回是否发生变更。"""
    changed = False
    if not cfg.get("lightBaseUrl") and cfg.get("baseUrl"):
        cfg["lightBaseUrl"] = cfg["baseUrl"]
        changed = True
    if not cfg.get("powerBaseUrl") and cfg.get("baseUrl"):
        cfg["powerBaseUrl"] = cfg["baseUrl"]
        changed = True
    if not cfg.get("lightApiKey") and cfg.get("apiKey"):
        cfg["lightApiKey"] = cfg["apiKey"]
        changed = True
    if not cfg.get("powerApiKey") and cfg.get("apiKey"):
        cfg["powerApiKey"] = cfg["apiKey"]
        changed = True
    return changed


def _normalize_base_url(url: str) -> str:
    """规范化 LLM base_url：url 无路径（或仅 "/"）时自动补 /v1。

    openai SDK 对 base_url 直接拼接 /chat/completions——New API/One API/
    DeepSeek 等 OpenAI 兼容网关的 API 路径均在 /v1 下，裸域名（如
    newapi_channel_conn 的 url）不补 /v1 会打到网关前端页面/404（实测：
    GET https://host/chat/completions 返回 HTML 页面）。已有路径（含 /v1
    或自定义代理路径）保持原样不动。"""
    url = url.strip()
    if not url:
        return url
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.path in ("", "/"):
            return url.rstrip("/") + "/v1"
    except Exception:
        pass  # 非 URL 形态（如纯域名）也补 /v1
    return url


# New API/One API 通道导出的通道类型（_type 字段，大小写不敏感）
_CHANNEL_TYPES = ("newapi_channel_conn",)


def _resolve_channel(cfg: dict) -> tuple:
    """解析通道配置（config.json 新增 llmChannel 字段）。

    - llmChannel 为 dict 或 JSON 字符串：New API 通道导出格式
      {"_type": "newapi_channel_conn", "key": "sk-...", "url": "https://..."}
    - 返回 (base_url, api_key, channel_type)；无法解析返回 ("", "", "")。
    """
    raw = cfg.get("llmChannel")
    if raw is None or raw == "":
        return "", "", ""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    ctype = str(data.get("_type") or "").strip()
    if ctype.lower() not in _CHANNEL_TYPES:
        return "", "", ""
    return (str(data.get("url") or "").strip(),
            str(data.get("key") or "").strip(),
            ctype)


def get_memory_config() -> dict:
    """实时读记忆配置（2026-08-25 集成 Hindsight 记忆模块；V-L7 同理：每次
    调用实时读 config.json，env 优先）。返回 dict，所有字段都有默认值；
    enabled=False 时记忆模块全部跳过（retain/recall/consolidation/reflect
    都检查此 flag）。记忆库所有 LLM 调用统一走 light tier（用户要求）。"""
    cfg = _load_config()
    mem = cfg.get("memory", {})

    def _b(key: str, default: bool) -> bool:
        v = mem.get(key)
        if isinstance(v, bool):
            return v
        return default

    return {
        "enabled": _env("DC_MEMORY_ENABLED") == "true" or _b("enabled", False),
        "db_path": _env("DC_MEMORY_DB_PATH") or str(mem.get("dbPath") or ""),
        "embedding_model": _env("DC_MEMORY_EMBEDDING_MODEL") or str(mem.get("embeddingModel") or "text-embedding-3-small"),
        "embedding_base_url": _env("DC_MEMORY_EMBEDDING_BASE_URL") or str(mem.get("embeddingBaseUrl") or ""),
        "embedding_api_key": _env("DC_MEMORY_EMBEDDING_API_KEY") or str(mem.get("embeddingApiKey") or ""),
        "retain_mission": str(mem.get("retainMission") or ""),
        "retain_extraction_mode": str(mem.get("retainExtractionMode") or "concise"),
        "retain_chunk_size": int(mem.get("retainChunkSize") or 3000),
        "retain_structured_chunk_size": int(mem.get("retainStructuredChunkSize") or 8192),
        "retain_extract_causal_links": _b("retainExtractCausalLinks", True),
        "semantic_link_min_similarity": float(mem.get("semanticLinkMinSimilarity") or 0.3),
        "consolidation_dedup_threshold": float(mem.get("consolidationDedupThreshold") or 0.97),
        "consolidation_batch_size": int(mem.get("consolidationBatchSize") or 50),
        "consolidation_llm_batch_size": int(mem.get("consolidationLlmBatchSize") or 10),
        "consolidation_max_attempts": int(mem.get("consolidationMaxAttempts") or 3),
        "recall_max_tokens": int(mem.get("recallMaxTokens") or 4096),
        "recall_budget": str(mem.get("recallBudget") or "mid"),
        "reranker_enabled": _b("rerankerEnabled", False),
        "disposition_skepticism": int(mem.get("dispositionSkepticism") or 3),
        "disposition_literalism": int(mem.get("dispositionLiteralism") or 3),
        "disposition_empathy": int(mem.get("dispositionEmpathy") or 3),
        "enable_observations": _b("enableObservations", True),
        "enable_auto_consolidation": _b("enableAutoConsolidation", True),
        "observation_history_max_entries": int(mem.get("observationHistoryMaxEntries") or 10),
        "enable_observation_history": _b("enableObservationHistory", True),
        "llm_output_language": str(mem.get("llmOutputLanguage") or ""),
    }


def write_config(update: dict) -> dict:
    """写 config.json（§3.1 旧规则：apiKey 空串保留旧值；原子写）。

    update 中 apiKey 为 None/"" 时保留旧值；apiKey/baseUrl 等写入前 strip
    （防粘贴空格；与 get_llm_config 的 strip 对应）。返回合并后的完整配置。
    2026-08-23：lightApiKey/powerApiKey 与 apiKey 同规则（空串保留旧值）。
    """
    cfg = _load_config()
    for key, value in update.items():
        if key in ("apiKey", "lightApiKey", "powerApiKey") and value in (None, ""):
            continue  # 空串保留旧值
        if isinstance(value, str):
            value = value.strip()
        cfg[key] = value
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)
    return cfg
