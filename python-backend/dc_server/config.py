from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "config.json"))

DEFAULT_PORT = 8501
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PROJECT_ROOT = os.path.join(BASE_DIR, "workspace")

DEFAULT_CONTEXT_WINDOW = 1_048_576


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value is not None and value != "" else None


def _load_config() -> dict:
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
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(os.path.dirname(CONFIG_PATH), expanded))


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
    drive, _ = os.path.splitdrive(PROJECT_ROOT)
    if drive and not os.path.exists(drive + os.sep):
        logger.warning(f"config: projectRoot={PROJECT_ROOT!r} 所在盘 {drive} 不存在，"
                       f"回退默认 {DEFAULT_PROJECT_ROOT}")
        PROJECT_ROOT = DEFAULT_PROJECT_ROOT

CORS_ORIGINS = [o.strip() for o in os.environ.get("DC_CORS_ORIGINS", "http://localhost:8501,http://127.0.0.1:8501").split(",") if o.strip()]


def get_context_window() -> int:
    raw = _load_config().get("contextWindow", DEFAULT_CONTEXT_WINDOW)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"config: 无效 contextWindow={raw!r}，回退默认 {DEFAULT_CONTEXT_WINDOW}")
        return DEFAULT_CONTEXT_WINDOW
    return value if value > 0 else DEFAULT_CONTEXT_WINDOW


def get_llm_config() -> dict:
    cfg = _load_config()
    if _migrate_legacy_llm_config(cfg):
        write_config(cfg)
    ch_url, ch_key, ch_type = _resolve_channel(cfg)
    base_url = (_env("LLM_BASE_URL") or ch_url or str(cfg.get("baseUrl") or "")).strip()
    api_key = (_env("LLM_API_KEY") or ch_key or str(cfg.get("apiKey") or "")).strip()
    light_base_url = (_env("LLM_LIGHT_BASE_URL") or str(cfg.get("lightBaseUrl") or "") or base_url).strip()
    light_api_key = (_env("LLM_LIGHT_API_KEY") or str(cfg.get("lightApiKey") or "") or api_key).strip()
    power_base_url = (_env("LLM_POWER_BASE_URL") or str(cfg.get("powerBaseUrl") or "") or base_url).strip()
    power_api_key = (_env("LLM_POWER_API_KEY") or str(cfg.get("powerApiKey") or "") or api_key).strip()
    channel_type = ch_type if (ch_url or ch_key) else ""

    light_model2 = (_env("LLM_LIGHT_MODEL2") or str(cfg.get("lightModel2") or "")).strip()
    light_base_url2 = (_env("LLM_LIGHT_BASE_URL2") or str(cfg.get("lightBaseUrl2") or "")).strip()
    light_api_key2 = (_env("LLM_LIGHT_API_KEY2") or str(cfg.get("lightApiKey2") or "")).strip()

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
        "light_model2": light_model2,
        "light_base_url2": _normalize_base_url(light_base_url2),
        "light_api_key2": light_api_key2,
        "light_input_price": _price("lightInputPrice"),
        "light_cached_price": _price("lightCachedPrice"),
        "light_output_price": _price("lightOutputPrice"),
        "power_input_price": _price("powerInputPrice"),
        "power_cached_price": _price("powerCachedPrice"),
        "power_output_price": _price("powerOutputPrice"),
        "channel_type": channel_type,
    }


def light_route_enabled(llm: dict) -> bool:
    return bool(llm.get("light_model2") and llm.get("light_base_url2")
                and llm.get("light_api_key2"))


def _migrate_legacy_llm_config(cfg: dict) -> bool:
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
    url = url.strip()
    if not url:
        return url
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.path in ("", "/"):
            return url.rstrip("/") + "/v1"
    except Exception:
        pass
    return url


_CHANNEL_TYPES = ("newapi_channel_conn",)


def _resolve_channel(cfg: dict) -> tuple:
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


def get_auth_config() -> dict:
    cfg = _load_config()
    auth_cfg = cfg.get("auth", {})
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}

    def _b(key: str, default: bool) -> bool:
        v = auth_cfg.get(key)
        if isinstance(v, bool):
            return v
        return default

    keys = auth_cfg.get("publicKeys") or []
    if not isinstance(keys, list):
        keys = []
    public_keys = []
    for k in keys:
        if isinstance(k, str) and k.strip():
            public_keys.append(k.strip())
        elif isinstance(k, dict) and k.get("key"):
            public_keys.append(str(k.get("key") or "").strip())
    env_auth = _env("DC_AUTH_ENABLED")
    if env_auth is not None:
        enabled = env_auth.lower() == "true"
    else:
        enabled = _b("enabled", False)
    return {
        "enabled": enabled,
        "sessionHours": int(_env("DC_AUTH_SESSION_HOURS") or auth_cfg.get("sessionHours") or 8),
        "challengeTtlSec": int(auth_cfg.get("challengeTtlSec") or 300),
        "publicKeys": public_keys,
    }


def get_https_config() -> dict:
    cfg = _load_config()
    https_cfg = cfg.get("https", {})
    if not isinstance(https_cfg, dict):
        https_cfg = {}
    cert_file = str(https_cfg.get("certFile") or "certs/server.crt")
    key_file = str(https_cfg.get("keyFile") or "certs/server.key")
    env_https = _env("DC_HTTPS_ENABLED")
    if env_https is not None:
        enabled = env_https.lower() == "true"
    else:
        enabled = bool(https_cfg.get("enabled"))
    return {
        "enabled": enabled,
        "certFile": _resolve_project_root(cert_file),
        "keyFile": _resolve_project_root(key_file),
    }


def get_mcp_config() -> list:
    cfg = _load_config()
    raw = cfg.get("mcpServers")
    if not isinstance(raw, list):
        return []
    servers = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        stype = str(item.get("type") or "stdio").strip().lower()
        if not name or name in seen or stype not in ("stdio", "http"):
            logger.warning(f"config: mcpServers 条目非法/重名，忽略: {item.get('name')!r}")
            continue
        if stype == "stdio" and not str(item.get("command") or "").strip():
            logger.warning(f"config: mcpServers stdio 条目 {name} 缺 command，忽略")
            continue
        if stype == "http" and not str(item.get("url") or "").strip():
            logger.warning(f"config: mcpServers http 条目 {name} 缺 url，忽略")
            continue
        seen.add(name)
        args = item.get("args") or []
        if not isinstance(args, list):
            args = []
        env = item.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        servers.append({
            "name": name,
            "type": stype,
            "command": str(item.get("command") or "").strip(),
            "args": [str(a) for a in args],
            "env": {str(k): str(v) for k, v in env.items()},
            "url": str(item.get("url") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
        })
    return servers


def write_config(update: dict) -> dict:
    cfg = _load_config()
    for key, value in update.items():
        if key in ("apiKey", "lightApiKey", "powerApiKey", "lightApiKey2") and value in (None, ""):
            continue
        if isinstance(value, str):
            value = value.strip()
        cfg[key] = value
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)
    return cfg
