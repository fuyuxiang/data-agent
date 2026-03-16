"""
NL2* 服务共享的 LLM 工具。
"""

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests


def get_llm_config(config: Dict[str, Any]) -> Tuple[str, str, str, int]:
    """提取 LLM 连接配置。"""
    llm_cfg = config.get("llm", {})

    api_key = os.getenv("LLM_API_KEY") or llm_cfg.get("api_key")
    if not api_key:
        raise RuntimeError("LLM API key not configured")

    base_url = os.getenv("LLM_BASE_URL") or llm_cfg.get("base_url", "https://api.deepseek.com")
    model = os.getenv("LLM_MODEL") or llm_cfg.get("model", "deepseek-chat")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    timeout = llm_cfg.get("timeout", 30)
    return api_key, url, model, timeout


def call_llm_chat(
    api_key: str,
    url: str,
    model: str,
    timeout: int,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """通用 LLM 调用。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {400, 422}:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = requests.post(url, headers=headers, json=fallback_payload, timeout=timeout)
            response.raise_for_status()
        else:
            raise

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _strip_think_blocks(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\s*/?\s*think\s*>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        start = text.find("{", start + 1)
    return None


def parse_llm_json(content: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象。"""
    text = _strip_think_blocks(content.strip())
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    candidate = _extract_first_json_object(text) or text
    try:
        return json.loads(candidate)
    except Exception as exc:
        raise RuntimeError(f"LLM 输出不是有效 JSON: {content}") from exc


def has_llm_config(config: Dict[str, Any]) -> bool:
    """是否存在可用 LLM 配置。"""
    llm_cfg = config.get("llm", {})
    api_key = os.getenv("LLM_API_KEY") or llm_cfg.get("api_key")
    return bool(api_key)
