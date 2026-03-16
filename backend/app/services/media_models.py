"""
多模态模型客户端。

当前实现采用“远程调用优先，失败后回退到本地确定性特征”的策略，
保证媒体离线处理链路在没有外部模型时也可运行。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np
import requests
from PIL import Image

from app.core.config import settings


class MediaModelClient:
    def __init__(self) -> None:
        self.timeout = 20

    def _can_remote_embedding(self) -> bool:
        return bool(settings.MEDIA_ENABLE_REMOTE_MODELS and (settings.EMBEDDING_QWEN_API_URL or "").strip())

    def _can_remote_caption(self) -> bool:
        return bool(
            settings.MEDIA_ENABLE_REMOTE_MODELS
            and (settings.VL_BASE_URL or "").strip()
            and (settings.VL_API_KEY or "").strip()
            and (settings.VL_MODEL or "").strip()
        )

    def _can_remote_rerank(self) -> bool:
        return bool(settings.MEDIA_ENABLE_REMOTE_MODELS and (settings.RERANKER_API_URL or "").strip())

    def embed_text(self, text: str) -> list[float]:
        if self._can_remote_embedding():
            try:
                return self._remote_embedding({"input": text})
            except Exception:
                pass
        return self._fallback_text_embedding(text)

    def embed_image(self, image_path: str) -> list[float]:
        if self._can_remote_embedding():
            try:
                data_url = self._image_to_data_url(image_path)
                return self._remote_embedding({"image": data_url})
            except Exception:
                pass
        return self._fallback_image_embedding(image_path)

    def caption_image(self, image_path: str) -> Optional[str]:
        if self._can_remote_caption():
            try:
                return self._remote_caption(image_path)
            except Exception:
                pass
        return self._fallback_caption(image_path)

    def extract_tags(self, text: Optional[str]) -> list[str]:
        if not text:
            return []
        words = re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text.lower())
        tags: list[str] = []
        for word in words:
            if not word or len(word) <= 1:
                continue
            if word not in tags:
                tags.append(word)
        return tags[:12]

    def rerank_documents(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []

        if self._can_remote_rerank():
            try:
                return self._remote_rerank(query, documents, top_k=top_k)
            except Exception:
                pass
        return self._fallback_rerank(query, documents, top_k=top_k)

    def _remote_embedding(self, payload: dict[str, Any]) -> list[float]:
        url = (settings.EMBEDDING_QWEN_API_URL or "").rstrip("/")
        candidates = [url, f"{url}/embeddings", f"{url}/v1/embeddings"]
        last_error: Optional[Exception] = None

        for candidate in candidates:
            try:
                response = requests.post(candidate, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                vector = self._extract_embedding(data)
                if vector:
                    return vector
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        raise RuntimeError("embedding service returned empty vector")

    def _remote_caption(self, image_path: str) -> Optional[str]:
        url = (settings.VL_BASE_URL or "").rstrip("/") + "/v1/chat/completions"
        data_url = self._image_to_data_url(image_path)
        payload = {
            "model": settings.VL_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请简短描述图片内容，并给出 3-5 个标签，返回 JSON：{\"caption\":\"...\",\"tags\":[...]}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {settings.VL_API_KEY}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
            return str(parsed.get("caption") or "").strip() or None
        except Exception:
            return str(content).strip() or None

    def _remote_rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        url = (settings.RERANKER_API_URL or "").rstrip("/")
        candidates = [url, f"{url}/rerank", f"{url}/v1/rerank"]
        payloads = [
            {"query": query, "documents": documents, "top_n": top_k or len(documents)},
            {"query": query, "texts": documents, "top_k": top_k or len(documents)},
            {"query": query, "docs": documents, "top_k": top_k or len(documents)},
        ]

        last_error: Optional[Exception] = None
        for candidate in candidates:
            for payload in payloads:
                try:
                    response = requests.post(candidate, json=payload, timeout=self.timeout)
                    response.raise_for_status()
                    return self._extract_rerank_results(response.json(), documents, top_k=top_k)
                except Exception as exc:
                    last_error = exc

        if last_error:
            raise last_error
        raise RuntimeError("reranker service returned empty result")

    def _extract_embedding(self, data: Any) -> list[float]:
        if isinstance(data, dict):
            if isinstance(data.get("embedding"), list):
                return [float(item) for item in data["embedding"]]
            if isinstance(data.get("vector"), list):
                return [float(item) for item in data["vector"]]
            raw_data = data.get("data")
            if isinstance(raw_data, list) and raw_data:
                first = raw_data[0]
                if isinstance(first, dict):
                    return self._extract_embedding(first)
        raise RuntimeError("embedding payload not found")

    def _extract_rerank_results(
        self,
        data: Any,
        documents: list[str],
        *,
        top_k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        raw_items: Any = data
        if isinstance(data, dict):
            for key in ("results", "data", "items"):
                if isinstance(data.get(key), list):
                    raw_items = data[key]
                    break
            else:
                if isinstance(data.get("scores"), list):
                    raw_items = [
                        {"index": idx, "score": score}
                        for idx, score in enumerate(data["scores"])
                    ]

        if not isinstance(raw_items, list):
            raise RuntimeError("reranker payload not found")

        for idx, item in enumerate(raw_items):
            if isinstance(item, dict):
                doc_index = item.get("index", item.get("doc_id", idx))
                try:
                    doc_index = int(doc_index)
                except Exception:
                    doc_index = idx
                if doc_index < 0 or doc_index >= len(documents):
                    continue
                score = item.get("relevance_score", item.get("score", item.get("rerank_score", 0.0)))
                try:
                    score = float(score)
                except Exception:
                    score = 0.0
                items.append({"index": doc_index, "score": score, "document": documents[doc_index]})
            else:
                try:
                    score = float(item)
                except Exception:
                    score = 0.0
                items.append({"index": idx, "score": score, "document": documents[idx]})

        items.sort(key=lambda item: item["score"], reverse=True)
        return items[: top_k or len(items)]

    def _image_to_data_url(self, image_path: str) -> str:
        with open(image_path, "rb") as file_obj:
            encoded = base64.b64encode(file_obj.read()).decode("utf-8")
        return f"data:image/{Path(image_path).suffix.lstrip('.').lower() or 'png'};base64,{encoded}"

    def _fallback_text_embedding(self, text: str, dim: int = 32) -> list[float]:
        vec = np.zeros(dim, dtype=np.float32)
        tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
        if not tokens:
            tokens = [text.lower()]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for idx in range(dim):
                vec[idx] += digest[idx % len(digest)] / 255.0
        return self._normalize(vec)

    def _fallback_image_embedding(self, image_path: str, dim: int = 32) -> list[float]:
        image = Image.open(image_path).convert("RGB").resize((8, 8))
        array = np.asarray(image, dtype=np.float32) / 255.0
        pooled = array.mean(axis=2).flatten()
        if pooled.size < dim:
            pooled = np.pad(pooled, (0, dim - pooled.size))
        else:
            pooled = pooled[:dim]
        return self._normalize(pooled)

    def _fallback_caption(self, image_path: str) -> str:
        image = Image.open(image_path).convert("RGB").resize((1, 1))
        r, g, b = image.getpixel((0, 0))
        dominant = self._dominant_color_name(r, g, b)
        stem_words = [word for word in re.split(r"[_\-\s]+", Path(image_path).stem.lower()) if word]
        text = " ".join(stem_words[:6]).strip()
        if text:
            return f"{text} {dominant}".strip()
        return dominant

    def _dominant_color_name(self, r: int, g: int, b: int) -> str:
        if r > 200 and g > 200 and b > 200:
            return "white image"
        if r < 60 and g < 60 and b < 60:
            return "black image"
        if r >= g and r >= b:
            return "red image"
        if g >= r and g >= b:
            return "green image"
        return "blue image"

    def _normalize(self, vector: np.ndarray) -> list[float]:
        norm = float(np.linalg.norm(vector))
        if math.isclose(norm, 0.0):
            return vector.tolist()
        return (vector / norm).astype(np.float32).tolist()

    def _fallback_rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query_tokens = [token for token in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query.lower()) if token]
        ranked: list[dict[str, Any]] = []
        for idx, document in enumerate(documents):
            text = (document or "").lower()
            if not text:
                ranked.append({"index": idx, "score": 0.0, "document": document})
                continue
            hit_count = sum(1 for token in query_tokens if token in text)
            score = hit_count / max(len(query_tokens), 1)
            ranked.append({"index": idx, "score": float(score), "document": document})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[: top_k or len(ranked)]


media_model_client = MediaModelClient()
