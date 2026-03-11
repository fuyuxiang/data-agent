"""
系统监控 API
"""
from datetime import datetime, timezone
import os
import time
from typing import Any, Dict

import requests
from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends

from app.api.auth import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import User

logger = get_logger(__name__)
router = APIRouter()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_down_payload(message: str, detail: str = "", latency_ms: int = 0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": False,
        "status": "down",
        "message": message,
        "checked_at": _utc_now_iso(),
        "latency_ms": latency_ms,
    }
    if detail:
        payload["detail"] = detail
    return payload


def _build_up_payload(message: str, latency_ms: int, model: str = "", detail: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "status": "up",
        "message": message,
        "checked_at": _utc_now_iso(),
        "latency_ms": latency_ms,
    }
    if model:
        payload["model"] = model
    if detail:
        payload["detail"] = detail
    return payload


def _chat_completion_probe(base_url: str, api_key: str, model: str, timeout_sec: int) -> Dict[str, Any]:
    """对不支持 /v1/models 的兼容网关，回退用 chat/completions 做连通性探测。"""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    started = time.perf_counter()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            return _build_up_payload("环境启动正常", latency_ms, model=model, detail="通过 chat/completions 心跳检测")
        if resp.status_code in {401, 403}:
            return _build_down_payload("环境启动异常", f"LLM 鉴权失败（{resp.status_code}）", latency_ms)
        return _build_down_payload("环境启动异常", f"LLM 心跳探测失败（chat/completions 返回 {resp.status_code}）", latency_ms)
    except requests.RequestException as exc:
        logger.warning(f"LLM chat completion probe failed: {exc}")
        latency_ms = int((time.perf_counter() - started) * 1000)
        return _build_down_payload("环境启动异常", f"LLM 连接失败: {exc}", latency_ms)


@router.get("/llm-heartbeat")
async def llm_heartbeat(_: User = Depends(get_current_user)):
    """LLM 心跳检查：验证服务可达性与基础鉴权是否可用"""
    base_url = (settings.LLM_BASE_URL or os.getenv("LLM_BASE_URL") or "").strip()
    api_key = (settings.LLM_API_KEY or os.getenv("LLM_API_KEY") or "").strip()
    model = (settings.LLM_MODEL or os.getenv("LLM_MODEL") or "").strip()

    if not base_url:
        return _build_down_payload("环境启动异常", "LLM_BASE_URL 未配置")
    if not api_key:
        return _build_down_payload("环境启动异常", "LLM_API_KEY 未配置")

    return await run_in_threadpool(
        _probe_llm_sync,
        base_url,
        api_key,
        model or "deepseek-chat",
    )


def _probe_llm_sync(base_url: str, api_key: str, model: str) -> Dict[str, Any]:
    """在线程池中执行心跳探测，避免阻塞事件循环。"""
    models_url = base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout_sec = 8
    started = time.perf_counter()

    try:
        resp = requests.get(models_url, headers=headers, timeout=timeout_sec)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            model_count = len(data.get("data", [])) if isinstance(data, dict) else 0
            payload = _build_up_payload("环境启动正常", latency_ms, model=model)
            payload["model_count"] = model_count
            return payload

        detail = f"LLM 服务返回 HTTP {resp.status_code}"
        if resp.status_code == 401:
            detail = "LLM 鉴权失败（401）"
        elif resp.status_code == 403:
            detail = "LLM 鉴权失败（403）"
        elif resp.status_code in {404, 405}:
            # 兼容不提供 /v1/models 的服务商（例如部分 OpenAI 兼容网关）
            return _chat_completion_probe(base_url, api_key, model, timeout_sec)
        return _build_down_payload("环境启动异常", detail, latency_ms)
    except requests.RequestException as exc:
        logger.warning(f"LLM heartbeat failed: {exc}")
        # GET /v1/models 失败时，回退到 chat/completions 心跳，减少误报。
        return _chat_completion_probe(base_url, api_key, model, timeout_sec)
