"""ca/meta_marker.py — EFLR MetaMarker 的 Hermes 移植（决策 44）。

职责：
- provider 推断（EFLR meta_marker.py:42-51 默认规则表；Hermes hook 已给
  provider 时直接采用）。
- usage 归一化：Hermes post_api_request.usage 是 CanonicalUsage summary，
  键为 input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/
  reasoning_tokens/request_count/prompt_tokens/total_tokens（无 completion_tokens）。
- reasoning/content/tool_calls 提取：assistant_message 是 NormalizedResponse，
  reasoning 优先 provider_data["reasoning_content"]，再取顶层 .reasoning。
- pre_api_request / post_api_request payload 的一级元数据提取。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PROVIDER_RULES: List[Tuple[str, str]] = [
    (r"deepseek", "deepseek"),
    (r"gpt-|o1|o3", "openai"),
    (r"openai", "openai"),
    (r"openrouter", "openrouter"),
    (r"localhost|127\.0\.0\.1", "local"),
    (r"ollama", "ollama"),
    (r"anthropic|claude", "anthropic"),
]


def infer_provider(provider: Any, model: Any, base_url: Any = "") -> str:
    """EFLR provider 规则表（顺序匹配 base_url + model；显式 provider 优先）。"""
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    base = (base_url or "").lower()
    model_lower = (model or "").lower()
    for pattern, name in DEFAULT_PROVIDER_RULES:
        if re.search(pattern, base) or re.search(pattern, model_lower):
            return name
    return "unknown"


def _get_field(obj: Any, names: Tuple[str, ...]) -> Optional[Any]:
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj.get(name)
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None


def extract_usage_tokens(usage: Any) -> Tuple[Optional[int], Optional[int]]:
    """返回 (prompt_tokens, completion/output_tokens)。

    - Hermes canonical summary：prompt_tokens 已为 input+cache 汇总，
      completion 列映射 output_tokens（Hermes summary 无 completion_tokens）。
    - 兼容旧测试/旧载荷：completion_tokens 键。
    - 兼容 SimpleNamespace。
    """
    if usage is None:
        return None, None
    prompt = _get_field(usage, ("prompt_tokens",))
    completion = _get_field(usage, ("completion_tokens", "output_tokens"))
    return (prompt if isinstance(prompt, int) else None,
            completion if isinstance(completion, int) else None)


def extract_reasoning_text(assistant_message: Any) -> str:
    """从 NormalizedResponse 提取全量 reasoning。

    优先 provider_data["reasoning_content"]（DeepSeek/Moonshot 原始字段，
    保持 E-stage v5 契约），再取顶层 `.reasoning`（Hermes transport 归一化）。
    绝不回退到 content（EFLR THINKING 块语义，决策 44 R2.3）。
    """
    if assistant_message is None:
        return ""
    pd = getattr(assistant_message, "provider_data", None) or {}
    if isinstance(pd, dict):
        raw = pd.get("reasoning_content")
        if isinstance(raw, str) and raw:
            return raw
    top = getattr(assistant_message, "reasoning", None)
    if isinstance(top, str) and top:
        return top
    return ""


def extract_content_text(assistant_message: Any) -> str:
    if assistant_message is None:
        return ""
    content = getattr(assistant_message, "content", None)
    return content if isinstance(content, str) else ""


def normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    """把 ToolCall 对象列表归一化为 OpenAI schema 的 dict 列表。"""
    result: List[Dict[str, Any]] = []
    for tc in tool_calls or []:
        if tc is None:
            continue
        tc_id = getattr(tc, "id", None) or ""
        tc_name = getattr(tc, "name", None) or ""
        raw_args = getattr(tc, "arguments", None)
        if raw_args is None or raw_args == "":
            arguments = "{}"
        elif isinstance(raw_args, str):
            try:
                import json as _json
                _json.loads(raw_args)
                arguments = raw_args
            except (TypeError, ValueError):
                arguments = "{}"
        else:
            import json as _json
            try:
                arguments = _json.dumps(raw_args, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                arguments = "{}"
        result.append({
            "id": tc_id,
            "type": getattr(tc, "type", "function") or "function",
            "function": {"name": tc_name, "arguments": arguments},
        })
    return result


def extract_request_meta(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """pre_api_request payload → llm_calls 一级元数据（fail-open）。"""
    kwargs = kwargs or {}
    provider = infer_provider(
        kwargs.get("provider"), kwargs.get("model"), kwargs.get("base_url"))
    return {
        "request_id": kwargs.get("api_request_id", ""),
        "turn_id": kwargs.get("turn_id", ""),
        "provider": provider,
        "model": kwargs.get("model", ""),
        "base_url": kwargs.get("base_url", ""),
        "api_mode": kwargs.get("api_mode", ""),
        "api_call_count": kwargs.get("api_call_count"),
        "retry_count": kwargs.get("retry_count"),
        "messages_count": kwargs.get("message_count"),
        "input_chars": kwargs.get("request_char_count") or (
            kwargs.get("approx_input_tokens") * 4
            if isinstance(kwargs.get("approx_input_tokens"), int) else 0),
        "max_tokens": kwargs.get("max_tokens"),
        "started_at": kwargs.get("started_at"),
    }


def extract_response_meta(kwargs: Dict[str, Any], assistant_message: Any) -> Dict[str, Any]:
    """post_api_request payload → 元数据 + 拆块输入（fail-open）。"""
    kwargs = kwargs or {}
    reasoning_text = extract_reasoning_text(assistant_message)
    content_text = extract_content_text(assistant_message)
    tool_defs = normalize_tool_calls(getattr(assistant_message, "tool_calls", None) or [])
    finish_kind = kwargs.get("finish_reason") or "stop"
    usage = kwargs.get("usage") or {}
    prompt_tokens, completion_tokens = extract_usage_tokens(usage)
    api_duration = kwargs.get("api_duration")
    return {
        "request_id": kwargs.get("api_request_id", ""),
        "provider": infer_provider(
            kwargs.get("provider"), kwargs.get("model"), kwargs.get("base_url")),
        "model": kwargs.get("model", ""),
        "base_url": kwargs.get("base_url", ""),
        "api_mode": kwargs.get("api_mode", ""),
        "api_call_count": kwargs.get("api_call_count"),
        "message_count": kwargs.get("message_count"),
        "finish_reason": finish_kind,
        "finish_kind": finish_kind,
        "usage": usage,
        "usage_prompt_tokens": prompt_tokens,
        "usage_completion_tokens": completion_tokens,
        "api_duration": api_duration,
        "duration_ms": int(api_duration * 1000) if isinstance(api_duration, (int, float)) else None,
        "reasoning_text": reasoning_text,
        "content_text": content_text,
        "reasoning_chars": len(reasoning_text),
        "text_chars": len(content_text),
        "tool_calls": tool_defs,
        "tool_calls_json": _safe_json(tool_defs),
    }


def _safe_json(value: Any) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return "[]"


def now_ts() -> float:
    return time.time()
