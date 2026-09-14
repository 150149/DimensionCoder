from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from . import config

logger = logging.getLogger("dc_server.auth")


def _now() -> float:
    return time.time()


class AuthManager:

    def __init__(self) -> None:
        self._challenges: dict[str, float] = {}
        self._sessions: dict[str, float] = {}

    def _cfg(self) -> dict:
        return config.get_auth_config()

    def _public_key_objects(self) -> list:
        keys = []
        for entry in self._cfg().get("publicKeys", []):
            key = self._load_public_key(entry)
            if key is not None:
                keys.append(key)
        return keys

    @staticmethod
    def _load_public_key(entry: str):
        try:
            if "BEGIN" in entry:
                return serialization.load_pem_public_key(entry.encode())
            return serialization.load_der_public_key(base64.b64decode(entry.strip()))
        except Exception:
            logger.warning("跳过无效的公钥配置项（格式无法解析，不打印其内容）")
            return None

    def issue_challenge(self) -> str:
        challenge = secrets.token_urlsafe(32)
        self._challenges[challenge] = _now() + int(self._cfg().get("challengeTtlSec", 300))
        return challenge

    @staticmethod
    def _try_p1363_to_der(signature: bytes, key_size_bits: int):
        half = (key_size_bits + 7) // 8
        if len(signature) != 2 * half:
            return None
        r = int.from_bytes(signature[:half], "big")
        s = int.from_bytes(signature[half:], "big")
        return encode_dss_signature(r, s)

    def verify_login(self, challenge: str, signature_b64: str) -> Optional[str]:
        rec = self._challenges.pop(challenge, None)
        if rec is None or rec <= _now():
            logger.info("登录失败：challenge 无效或已过期（不打印其内容）")
            return None
        try:
            signature = base64.b64decode(signature_b64)
        except (ValueError, TypeError):
            logger.info("登录失败：签名 base64 非法（不打印其内容）")
            return None
        data = challenge.encode("utf-8")
        public_keys = self._public_key_objects()
        logger.info("验签中（已配置公钥 %d 个）", len(public_keys))
        matched = False
        for public_key in public_keys:
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                continue
            try:
                public_key.verify(signature, data, ec.ECDSA(hashes.SHA512()))
                matched = True
                break
            except InvalidSignature:
                der = self._try_p1363_to_der(signature, public_key.curve.key_size)
                if der is not None:
                    try:
                        public_key.verify(der, data, ec.ECDSA(hashes.SHA512()))
                        matched = True
                        break
                    except InvalidSignature:
                        pass
        if not matched:
            logger.info("登录失败：签名与 %d 个公钥均不匹配（浏览器密钥与 config 公钥可能不是同一对）", len(public_keys))
            return None
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _now() + int(self._cfg().get("sessionHours", 8)) * 3600
        logger.info("登录成功（session 已签发，不打印 token）")
        return token

    def is_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        exp = self._sessions.get(token)
        if exp is None:
            return False
        if exp <= _now():
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)


_auth_manager = AuthManager()


class AuthMiddleware:

    def __init__(self, app: Any) -> None:
        self.app = app

    def _is_protected(self, path: str) -> bool:
        cfg = config.get_auth_config()
        if not cfg.get("enabled"):
            return False
        if path.startswith("/api/auth/"):
            return False
        return (path.startswith("/api/") or path == "/sse"
                or path.startswith("/docs") or path == "/openapi.json"
                or path == "/redoc")

    def _authenticated(self, token: Optional[str]) -> bool:
        return _auth_manager.is_valid(token)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not self._is_protected(path):
            await self.app(scope, receive, send)
            return
        cookies = self._parse_cookies(scope.get("headers", []))
        if not self._authenticated(cookies.get("dc_session")):
            logger.info("拦截未认证请求：%s %s", scope.get("method"), path)
            await self._send_json(send, 401, {"error": "unauthorized"})
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _parse_cookies(headers: list) -> dict[str, str]:
        out: dict[str, str] = {}
        cookie_header = ""
        for k, v in headers or []:
            if k == b"cookie":
                cookie_header = v.decode("latin-1")
                break
        for part in cookie_header.split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                out[name.strip()] = value.strip()
        return out

    @staticmethod
    async def _send_json(send, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        await send({"type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})
