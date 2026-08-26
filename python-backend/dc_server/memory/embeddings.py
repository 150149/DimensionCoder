
from __future__ import annotations

import asyncio
import logging
import math
from array import array
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536

def pack_embedding(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()

def unpack_embedding(blob: bytes) -> list[float]:
    if not blob:
        return []
    a = array("f")
    a.frombytes(blob)
    return list(a)

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)

class EmbeddingProvider:

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
        if not text or not self.base_url or not self.api_key:
            return None

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
        if not texts:
            return []
        results: list[Optional[list[float]]] = []
        if self._is_gemini():
            for text in texts:
                results.append(await self.embed(text))
        else:
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
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": text}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]

    async def _embed_openai_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": texts}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [d["embedding"] for d in sorted_data]

    async def _embed_gemini(self, text: str) -> Optional[list[float]]:
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
        return bool(self.base_url and self.api_key and self.model)
