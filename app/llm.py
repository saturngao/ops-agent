"""大模型客户端：OpenAI 兼容接口（支持 Qwen/DashScope、DeepSeek、OpenAI 等）"""
import json
import re
import logging

import httpx

from .config import load_config

log = logging.getLogger("ops-agent.llm")


class LLMError(Exception):
    pass


def chat(messages: list, json_mode: bool = False, temperature: float | None = None,
         max_tokens: int = 2000, timeout: float = 90.0, node: str = "chat") -> str:
    """调用 OpenAI 兼容接口（流式收集完整回复），调用信息自动记录到 MySQL。"""
    cfg = load_config()["llm"]
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or "qwen-plus"
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if not base_url or not api_key:
        _log_call(node, model, False, 0, prompt_chars, "未配置大模型")
        raise LLMError("未配置大模型：请在「配置」页填写 base_url 与 api_key（如 DashScope qwen-plus）")

    url = base_url + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": cfg.get("temperature", 0.2) if temperature is None else temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    content = ""
    import time as _time
    t0 = _time.time()
    try:
        with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
            if resp.status_code != 200:
                text = resp.read().decode("utf-8", "ignore")[:500]
                raise LLMError(f"LLM HTTP {resp.status_code}: {text}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                content += delta.get("content") or ""
        _log_call(node, model, True, int((_time.time() - t0) * 1000), prompt_chars, "")
    except LLMError as e:
        _log_call(node, model, False, int((_time.time() - t0) * 1000), prompt_chars, str(e))
        raise
    except Exception as e:
        _log_call(node, model, False, int((_time.time() - t0) * 1000), prompt_chars, f"LLM 调用失败: {e}")
        raise LLMError(f"LLM 调用失败: {e}")

    if not content.strip():
        raise LLMError("LLM 返回为空")
    return content


def _log_call(node: str, model: str, ok: bool, latency_ms: int, prompt_chars: int, error: str) -> None:
    try:
        from . import db
        db.log_llm_call(node, model, ok, latency_ms, prompt_chars, error)
    except Exception:
        pass


def chat_json(messages: list, temperature: float | None = None, node: str = "chat_json") -> dict:
    """要求模型返回 JSON 并稳健解析。"""
    raw = chat(messages, json_mode=True, temperature=temperature, node=node)
    return extract_json(raw)


def extract_json(text: str) -> dict:
    text = text.strip()
    # 去掉 ```json ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 截取第一个 { 到最后一个 }
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            pass
    raise LLMError(f"LLM 返回内容无法解析为 JSON: {text[:200]}")
