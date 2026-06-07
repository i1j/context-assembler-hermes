"""
L1 生成速度测试脚本 (v4.2‑final)

用法:
  python tests/test_l1_gen_speed.py
  python tests/test_l1_gen_speed.py --model qwen3.5:9b
  python tests/test_l1_gen_speed.py --url http://localhost:11435 --model qwen3.5:hermes-32k
  python tests/test_l1_gen_speed.py --no-think
  python tests/test_l1_gen_speed.py --num-ctx 16384

注意:
  - 使用 OODA 5 节 Prompt（与 ca/prompts.py 一致），真实反映 L1 生成速度。
  - stdout 输出 JSON，stderr 输出可读摘要和 L0/L1 样本。
  - 默认使用本地 Ollama，可通过 --url 指定其他地址。
  - --vlm 参数已弃用，只输出警告并退出。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11435")
DEFAULT_MODEL = os.environ.get("MODEL", "qwen3.5:hermes-24k")
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "24576"))

# 测试语料：6 轮真实中文对话（与 OV 无关）
SAMPLE_MESSAGES = """用户: 现在 ov 什么情况
助手: **OV 目前正常。** OV Server healthy v0.3.17, VikingFS 正常, MCP 可用。

用户: 当前对话的数据写进去了没有？有提交吗？
助手: 查了 OV 数据目录。最新 session 有 1 次 commit, 含 2 条消息。L1 overview 已生成。

用户: 为什么是英文呢？我们不是用中文对话的吗？
助手: 确认 L1 overview 全英文。查 OV prompt 模板发现 ov_wm_v2.yaml 的 language 字段是 "en"。

用户: 这跟 OVCA 没关系。OV 的 prompt 是开放可以改的吗？
助手: OV prompt 是开放的 YAML 模板，可通过 ov.conf 的 prompts.templates_dir 或环境变量覆盖。

用户: 弄
助手: 创建自定义模板目录，加中文指令，更新 start.sh，重启 OV，验证 L1 已中文输出。

用户: 等等。你说模板可以自定是吧？那我可以指定什么时候写 7 段，什么时候写 L1/L0 吗？
助手: 解释 L0 从 L1 第一行用正则提取。无法通过配置控制条件逻辑——写死在 session.py。"""

# 对齐 ca/prompts.py 的 OODA 5 节模板
OODA_TEMPLATE = """你是一个会议纪要摘要助手。请根据上一轮摘要和本轮对话，生成增量式的 OODA 摘要。

【输出格式 – 严格遵循】
核心摘要：[1句话]
资源与观察：
- [最多3项]
事实与约束：
- [最多3项]
决策与结论：
- [最多3项]
后续行动：
- [最多3项]

【规则】
- 每项≤15字，使用短横线列表。
- 禁用冒号、括号、解释性文字。
- 无空行，无额外标题。
- 若某节无新信息，写“无”。
- 若整个增量无新信息，核心摘要写“本轮无新内容”，其余节写“无”。

【上一轮摘要】
{previous_summary}

【本轮对话】
{current_dialog}

请生成本轮增量摘要："""


def build_prompt(messages: str, previous_summary: str = "") -> str:
    return OODA_TEMPLATE.format(
        previous_summary=previous_summary or "（无）",
        current_dialog=messages,
    )


def check_ollama(url: str, timeout: int = 5) -> None:
    """预检：尝试 GET /api/tags，若失败则给出友好提示。"""
    try:
        req = urllib.request.Request(f"{url}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except Exception as e:
        print(f"无法连接到 Ollama ({url}): {e}", file=sys.stderr)
        print("请确认 Ollama 服务已启动且地址正确。", file=sys.stderr)
        sys.exit(1)


def call_ollama(
    prompt: str,
    model: str,
    url: str,
    num_ctx: int = DEFAULT_NUM_CTX,
    no_think: bool = False,
    timeout: int = 300,
) -> Dict[str, Any]:
    """调用 Ollama generate API，失败时抛出异常。"""
    options: Dict[str, object] = {
        "num_ctx": num_ctx,
        "num_predict": 8192,
        "temperature": 0.0,
    }
    if no_think:
        options["think"] = False

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "options": options,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接失败: {e.reason}") from e


def extract_l0(response: str) -> str:
    """从 OODA 文本中提取核心摘要第一句作为 L0，支持跨行。"""
    # 匹配 "核心摘要：xxx" 直到遇到下一个字段标题或文本结束
    m = re.search(
        r"核心摘要[：:]\s*([\s\S]*?)(?=\n(?:资源与观察|事实与约束|决策与结论|后续行动|$))",
        response,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().replace("\n", " ")[:100]
    # 回退：取第一行非空行
    for line in response.splitlines():
        if line.strip():
            return line.strip()[:100]
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="OODA L1 生成速度测试 (Ollama)",
        epilog="示例: python tests/test_l1_gen_speed.py --model qwen3.5:9b --no-think",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama 模型名称")
    parser.add_argument("--url", default=DEFAULT_OLLAMA_URL, help="Ollama 服务地址")
    parser.add_argument(
        "--vlm",
        action="store_true",
        help="[已弃用] 请使用 --model 和 --url 明确指定。",
    )
    parser.add_argument(
        "--no-think",
        action="store_true",
        help="关闭 thinking/思考输出（适用于 qwen3.5 等模型，Ollama 0.2+ 支持）",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=DEFAULT_NUM_CTX,
        help=f"上下文窗口大小（默认 {DEFAULT_NUM_CTX}）",
    )
    args = parser.parse_args()

    if args.vlm:
        print(
            "警告: --vlm 已弃用，请改用 --model 和 --url 明确指定模型和地址。\n"
            "脚本将使用默认配置继续运行。",
            file=sys.stderr,
        )

    model = args.model
    url = args.url.rstrip("/")

    # 预检连通性
    check_ollama(url)

    prompt = build_prompt(SAMPLE_MESSAGES)
    print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)
    print(f"Model: {model}", file=sys.stderr)
    print(f"URL: {url}", file=sys.stderr)
    print(f"No-think: {args.no_think}, Num-ctx: {args.num_ctx}", file=sys.stderr)

    t0 = time.monotonic()
    try:
        result = call_ollama(
            prompt, model, url,
            num_ctx=args.num_ctx,
            no_think=args.no_think,
        )
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.monotonic() - t0

    response = result.get("response", "")
    gen_tok = result.get("eval_count", 0)
    gen_us = result.get("eval_duration", 0)
    prompt_tok = result.get("prompt_eval_count", 0)

    tok_s = gen_tok / (gen_us / 1e9) if gen_us > 0 else 0.0

    # 输出 JSON（机器可读）
    metrics = {
        "model": model,
        "no_think": args.no_think,
        "num_ctx": args.num_ctx,
        "prompt_tokens": prompt_tok,
        "gen_tokens": gen_tok,
        "total_duration_s": round(elapsed, 2),
        "eval_duration_s": round(gen_us / 1e9, 2) if gen_us else 0,
        "tokens_per_sec": round(tok_s, 1),
        "response_chars": len(response),
    }
    print(json.dumps(metrics, ensure_ascii=False))

    # 人类可读摘要
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Model: {model}", file=sys.stderr)
    print(f"Prompt tokens: {prompt_tok}, Gen tokens: {gen_tok}", file=sys.stderr)
    print(f"Total time: {elapsed:.1f}s, Eval time: {gen_us/1e9:.1f}s", file=sys.stderr)
    print(f"Speed: {tok_s:.1f} tok/s", file=sys.stderr)
    print(f"Response length: {len(response)} chars", file=sys.stderr)

    l0 = extract_l0(response)
    if l0:
        print(f"\n--- L0 (核心摘要) ---\n{l0}", file=sys.stderr)
    else:
        print("\n--- (无法提取 L0) ---", file=sys.stderr)
    print(f"\n--- L1 (前 300 字符) ---\n{response[:300]}", file=sys.stderr)


if __name__ == "__main__":
    main()