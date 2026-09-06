from __future__ import annotations

import hashlib
import math
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Sequence

import numpy as np
from flask import current_app

from .security import SecretVault, safe_http_request, validate_outbound_url


HASH_DIMENSIONS = 384
LOCAL_DIMENSIONS = 512
MAX_TOKENS = 128
VALID_MODES = {"auto", "cloud", "local", "hash"}
_runtime_lock = threading.RLock()
_local_sessions: dict[str, tuple[object, object]] = {}
_cloud_status: dict[str, bool | None] = {}


def _database():
    return current_app.extensions["meridian_db"]


def _record_id(workspace_id: str) -> str:
    return f"{workspace_id}:embedding"


def _model_dir() -> Path:
    return current_app.config["SETTINGS"].storage_dir / "models" / "bge-small-zh-v1.5"


def local_installed() -> bool:
    path = _model_dir()
    return (path / "model.onnx").is_file() and (path / "tokenizer.json").is_file()


def get_config(workspace_id: str) -> dict:
    record = _database().get("embedding_settings", _record_id(workspace_id)) or {}
    public = dict(record.get("config") or {})
    token = SecretVault(current_app.config["VAULT_KEY"]).open(record.get("credential", ""), {}).get("token", "")
    return {
        "mode": str(public.get("mode") or os.getenv("MERIDIAN_EMBED_MODE") or "auto"),
        "url": str(public.get("url") or os.getenv("MERIDIAN_EMBED_URL") or ""),
        "model": str(public.get("model") or os.getenv("MERIDIAN_EMBED_MODEL") or "bge-large-zh"),
        "token": token or os.getenv("MERIDIAN_EMBED_TOKEN", ""),
        "token_configured": bool(token or os.getenv("MERIDIAN_EMBED_TOKEN")),
    }


def save_mode(workspace_id: str, mode: str) -> dict:
    normalized = str(mode or "").strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError("Invalid mode. Use: auto, cloud, local, hash")
    current = get_config(workspace_id)
    _save_config(workspace_id, {**current, "mode": normalized})
    _cloud_status.pop(workspace_id, None)
    return info(workspace_id)


def configure_cloud(
    workspace_id: str, *, url: str, model: str, token: str | None = None,
    clear_token: bool = False, verify: bool = False,
) -> dict:
    normalized_url = validate_outbound_url(str(url or "").strip().rstrip("/"))
    normalized_model = str(model or "").strip()
    if not normalized_model:
        raise ValueError("云端模型名不能为空")
    current = get_config(workspace_id)
    next_token = "" if clear_token else (str(token).strip() if token is not None and str(token).strip() else current["token"])
    _save_config(workspace_id, {
        "mode": current["mode"], "url": normalized_url, "model": normalized_model,
        "token": next_token,
    })
    _cloud_status[workspace_id] = None
    result = cloud_public_config(workspace_id)
    if verify:
        result["test"] = test_cloud(workspace_id)
    return result


def _save_config(workspace_id: str, config: dict) -> None:
    public = {key: config.get(key) for key in ("mode", "url", "model")}
    credential = SecretVault(current_app.config["VAULT_KEY"]).seal({"token": config.get("token", "")})
    _database().put(
        "embedding_settings",
        {
            "id": _record_id(workspace_id), "workspace_id": workspace_id,
            "config": public, "credential": credential,
        },
        workspace_id=workspace_id,
    )


def cloud_public_config(workspace_id: str) -> dict:
    config = get_config(workspace_id)
    return {"url": config["url"], "model": config["model"], "token_configured": config["token_configured"]}


def _hash_tokens(text: str) -> list[str]:
    import re

    lowered = str(text or "").lower()
    words = re.findall(r"[a-z0-9_]+", lowered)
    cjk = re.findall(r"[\u4e00-\u9fff]+", lowered)
    grams = []
    for run in cjk:
        grams.extend(run)
        grams.extend(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return words + grams


def hash_embed(text: str) -> list[float]:
    vector = [0.0] * HASH_DIMENSIONS
    for token, count in Counter(_hash_tokens(text)).items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % HASH_DIMENSIONS
        vector[bucket] += (1.0 if digest[4] & 1 else -1.0) * (1.0 + math.log(count))
    length = math.sqrt(sum(value * value for value in vector))
    return [round(value / length, 8) for value in vector] if length else vector


def _local_runtime() -> tuple[object, object]:
    model_dir = _model_dir()
    cache_key = str(model_dir.resolve())
    with _runtime_lock:
        if cache_key in _local_sessions:
            return _local_sessions[cache_key]
        if not local_installed():
            raise FileNotFoundError("本地 BGE 模型未安装")
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("本地 BGE 需要 onnxruntime 和 tokenizers") from exc
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 2
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        model = ort.InferenceSession(str(model_dir / "model.onnx"), options, providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_padding(length=MAX_TOKENS)
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        _local_sessions[cache_key] = (model, tokenizer)
        return model, tokenizer


def local_embed_batch(texts: Sequence[str]) -> list[list[float]]:
    model, tokenizer = _local_runtime()
    encodings = tokenizer.encode_batch(list(texts))
    ids = np.array([item.ids for item in encodings], dtype=np.int64)
    mask = np.array([item.attention_mask for item in encodings], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in {item.name for item in model.get_inputs()}:
        feed["token_type_ids"] = np.zeros_like(ids)
    hidden = model.run(None, feed)[0]
    expanded = mask.astype(np.float32)[..., np.newaxis]
    pooled = (hidden * expanded).sum(axis=1) / np.maximum(expanded.sum(axis=1), 1.0)
    pooled = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
    return pooled.tolist()


def cloud_embed_batch(texts: Sequence[str], workspace_id: str, timeout: int = 30) -> list[list[float]]:
    config = get_config(workspace_id)
    if not config["url"] or not config["token"]:
        raise ValueError("云端向量服务 URL 或 Bearer Token 未配置")
    response = safe_http_request(
        "POST", config["url"].rstrip("/") + "/v1/embeddings",
        json={"input": list(texts), "model": config["model"]},
        headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    vectors = [item["embedding"] for item in payload.get("data", [])]
    if len(vectors) != len(texts) or any(not isinstance(vector, list) or not vector for vector in vectors):
        raise ValueError("云端向量响应数量或维度异常")
    _cloud_status[workspace_id] = True
    return vectors


def embed_batch(texts: Sequence[str], workspace_id: str = "default") -> list[list[float]]:
    if not texts:
        return []
    config = get_config(workspace_id)
    if config["mode"] in {"auto", "cloud"} and config["url"] and config["token"]:
        try:
            return cloud_embed_batch(texts, workspace_id)
        except Exception:
            _cloud_status[workspace_id] = False
            if config["mode"] == "cloud":
                raise
    if config["mode"] in {"auto", "local"} and local_installed():
        try:
            return local_embed_batch(texts)
        except Exception:
            if config["mode"] == "local":
                raise
    if config["mode"] in {"cloud", "local"}:
        raise RuntimeError(f"显式向量模式 {config['mode']} 不可用，已拒绝静默降级")
    return [hash_embed(text) for text in texts]


def embed(text: str, workspace_id: str = "default") -> list[float]:
    return embed_batch([text], workspace_id)[0]


def test_cloud(workspace_id: str) -> dict:
    vectors = cloud_embed_batch(["连接测试"], workspace_id, timeout=8)
    return {"available": True, "dim": len(vectors[0]), "model": get_config(workspace_id)["model"]}


def info(workspace_id: str, *, probe: bool = False) -> dict:
    config = get_config(workspace_id)
    if probe and config["url"] and config["token"]:
        try:
            test_cloud(workspace_id)
        except Exception:
            _cloud_status[workspace_id] = False
    cloud_available = _cloud_status.get(workspace_id)
    local_available = local_installed()
    if config["mode"] in {"auto", "cloud"} and config["token_configured"] and cloud_available is True:
        active, dimensions, model = "cloud", 0, config["model"]
    elif config["mode"] in {"auto", "local"} and local_available:
        active, dimensions, model = "local", LOCAL_DIMENSIONS, "BGE-small-zh-v1.5 (local)"
    elif config["mode"] in {"cloud", "local"}:
        active, dimensions, model = "unavailable", 0, config["model"]
    else:
        active, dimensions, model = "hash", HASH_DIMENSIONS, "基础关键词匹配（Hash 384维）"
    return {
        "mode": config["mode"], "active": active, "dim": dimensions, "model": model,
        "cloud_url": config["url"] if config["token_configured"] else "",
        "cloud_available": cloud_available is True, "cloud_configured": config["token_configured"] and bool(config["url"]),
        "cloud_status": "available" if cloud_available is True else "unavailable" if cloud_available is False else "configured",
        "local_available": local_available,
    }


MODEL_FILES = {
    "config.json": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/config.json",
    "tokenizer.json": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer.json",
    "tokenizer_config.json": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer_config.json",
    "special_tokens_map.json": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/special_tokens_map.json",
    "vocab.txt": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt",
    "model.onnx": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model.onnx",
}


def download_local_model() -> dict:
    target = _model_dir()
    target.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for filename, url in MODEL_FILES.items():
        path = target / filename
        if path.is_file() and path.stat().st_size:
            continue
        response = safe_http_request("GET", url, timeout=300, stream=True)
        response.raise_for_status()
        total = 0
        temporary = path.with_suffix(path.suffix + ".part")
        with temporary.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                total += len(chunk)
                if total > 220 * 1024 * 1024:
                    raise ValueError("BGE 模型文件超过安全大小上限")
                output.write(chunk)
        temporary.replace(path)
        downloaded.append(filename)
    _local_sessions.clear()
    return {"downloaded": downloaded, "installed": local_installed(), "model_dir": str(target)}


def rebuild(workspace_id: str) -> dict:
    from .knowledge import _build_chunk_index, _entry_text

    document_chunks = 0
    structured_records = 0
    for document in _database().list("knowledge_documents", workspace_id=workspace_id, limit=5000):
        chunks = document.get("chunks") or []
        _database().patch("knowledge_documents", document["id"], {"chunk_index": _build_chunk_index(chunks, workspace_id)})
        document_chunks += len(chunks)
    for entry in _database().list("knowledge_entries", workspace_id=workspace_id, limit=5000):
        _database().patch("knowledge_entries", entry["id"], {"embedding": embed(_entry_text(entry), workspace_id)})
        structured_records += 1
    skills = 0
    for skill in _database().list("skills", workspace_id=workspace_id, limit=5000):
        text = f"{skill.get('name', '')}\n{skill.get('description', '')}\n{skill.get('instruction', '')}"
        _database().patch("skills", skill["id"], {"embedding": embed(text, workspace_id)})
        skills += 1
    return {
        "document_chunks": document_chunks, "structured_records": structured_records,
        "skills": skills, "total_rebuilt": document_chunks + structured_records + skills,
    }
