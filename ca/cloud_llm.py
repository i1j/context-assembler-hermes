"""ca/cloud_llm.py — 云端 LLM 调用层（任务书 40：flash 全链路重跑 pilot）。

deepseek flash（deepseek-chat，OpenAI 兼容 /v1/chat/completions）：
  - key 从 .env 读（不硬编码）
  - urllib 调用（对齐 refine_reality_cloud.py 已验证模式）
  - 失败 → None（不抛异常污染上层）；重试可配置
  - lenient_parse / parse_sid 宽松解析（容忍围栏/前缀/截断）

模型名注意：`deepseek-chat` 实测可用；`deepseek-v4-flash` 名 404，勿用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"

# 默认 .env 路径（D-新9: 不再硬编码 tester profile，随 HERMES_PROFILE 环境变量）
DEFAULT_ENV = (
    Path.home() / ".hermes" / "profiles"
    / (os.getenv("HERMES_PROFILE", "tester") or "tester") / ".env"
)


def load_api_key(env_path: Optional[Path] = None) -> str:
    """从 .env 读取 DEEPSEEK_API_KEY。

    支持 `KEY=value` / `KEY="value"` / `KEY='value'` 三种写法。
    找不到 → RuntimeError（调用方应尽早暴露配置错误，不静默降级）。
    """
    path = Path(env_path or DEFAULT_ENV)
    if not path.exists():
        raise RuntimeError(f"env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"DEEPSEEK_API_KEY not found in {path}")


def call_cloud_llm(
    prompt: str,
    api_key: str,
    max_tokens: int = 8192,
    temperature: float = 0.2,
    max_retries: int = 2,
    timeout: int = 300,
) -> Optional[str]:
    """调用 deepseek-chat，返回响应文本。

    失败（网络/HTTP/解析异常）→ None（重试 max_retries 次后放弃）。
    """
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                API_URL,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choices = data.get("choices") or []
            if not choices:
                last_err = ValueError("empty choices in response")
                raise last_err
            return choices[0].get("message", {}).get("content") or ""
        except Exception as exc:  # noqa: BLE001 — 统一转 None，上层不感知异常类型
            last_err = exc
            logger.warning("[CA_CLOUD_LLM] attempt %d failed: %s", attempt + 1, exc)
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
    return None


def lenient_parse(text: Optional[str]):
    """剥离 markdown 围栏/前后杂音，取首个 { 到末个 } 宽松 json.loads。

    解析失败 → None（不抛异常）。
    """
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except (json.JSONDecodeError, TypeError):
        return None


def parse_sid(k) -> int:
    """容忍云端返回 'S7' / 's7' / 'SS7' / 'R4' / 7 形式的 strand/reality id。

    剥掉任意前导非数字字符（S/R 前缀均可能，flash 对 reality_id 习惯用 R 前缀）。
    """
    s = str(k).strip()
    while s and not s[0].isdigit():
        s = s[1:]
    return int(s)
