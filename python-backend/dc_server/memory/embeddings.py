"""
memory/embeddings.py — EmbeddingProvider：嵌入向量生成

复用 light model 的 API key 和 base_url。
支持 OpenAI 兼容 / Gemini 端点。
打包存储：array.array("f").tobytes() → SQLite BLOB。
降级：API 不可用返回 None，recall 降级为仅 FTS5+图谱+时间。
"""

from __future__ import annotations

import asyncio
import logging
import math
from array import array
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 嵌入向量维度（常见模型：text-embedding-3-small=1536, text-embedding-ada-002=1536）
# 实际维度由 API 返回决定，此处仅用于初始化默认值
DEFAULT_EMBEDDING_DIM = 1536


def pack_embedding(vec: list[float]) -> bytes:
    """打包 embedding 为 BLOB（float32）。比 list[float] 小 7.6x。"""
    return array("f", vec).tobytes()


def unpack_embedding(blob: bytes) -> list[float]:
    """解包 BLOB 为 float list。"""
    if not blob:
        return []
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


class EmbeddingProvider:
    """嵌入向量生成器。

    复用 DimensionCoder 的 light model 配置（base_url + api_key）。
    支持 OpenAI 兼容 /v1/embeddings 端点和 Gemini embedContent 端点。
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "text-embedding-3-small",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._cache: dict[str, list[float]] = {}
        self._dim = DEFAULT_EMBEDDING_DIM

    def _is_gemini(self) -> bool:
        return "generativelanguage.googleapis.com" in self.base_url or self.model.startswith("gemini")

    async def embed(self, text: str) -> Optional[list[float]]:
        """生成单条文本的嵌入向量。失败返回 None（降级）。"""
        if not text or not self.base_url or not self.api_key:
            return None

        # 缓存
        cache_key = hash(text)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            if self._is_gemini():
                vec = await self._embed_gemini(text)
            else:
                vec = await self._embed_openai(text)

            if vec:
                self._cache[cache_key] = vec
                self._dim = len(vec)
            return vec
        except Exception as e:
            logger.warning(f"Embedding failed, degrading to None: {e}")
            return None

    async def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """批量生成嵌入向量。"""
        if not texts:
            return []
        results: list[Optional[list[float]]] = []
        # OpenAI 支持批量，Gemini 需要逐条
        if self._is_gemini():
            for text in texts:
                results.append(await self.embed(text))
        else:
            # OpenAI 批量
            uncached = []
            uncached_indices = []
            for i, text in enumerate(texts):
                cache_key = hash(text)
                if cache_key in self._cache:
                    results.append(self._cache[cache_key])
                else:
                    results.append(None)
                    uncached.append(text)
                    uncached_indices.append(i)

            if uncached:
                try:
                    batch_vecs = await self._embed_openai_batch(uncached)
                    for idx, vec in zip(uncached_indices, batch_vecs):
                        if vec:
                            results[idx] = vec
                            self._cache[hash(uncached[uncached_indices.index(idx)])] = vec
                except Exception as e:
                    logger.warning(f"Batch embedding failed: {e}")

        if results:
            non_none = [v for v in results if v]
            if non_none:
                self._dim = len(non_none[0])
        return results

    async def _embed_openai(self, text: str) -> Optional[list[float]]:
        """OpenAI 兼容 /v1/embeddings 端点。"""
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": text}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]

    async def _embed_openai_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """OpenAI 批量嵌入。"""
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": texts}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            # 按 index 排序
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [d["embedding"] for d in sorted_data]

    async def _embed_gemini(self, text: str) -> Optional[list[float]]:
        """Gemini embedContent 端点。"""
        model_name = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"{self.base_url}/v1beta/{model_name}:embedContent"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            params = {"key": self.api_key}
        else:
            params = {}
        payload = {"content": {"parts": [{"text": text}]}}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, params=params, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding", {}).get("values")

    @property
    def dim(self) -> int:
        return self._dim

    def is_available(self) -> bool:
        """检查 embedding 是否可用。"""
        return bool(self.base_url and self.api_key and self.model)
