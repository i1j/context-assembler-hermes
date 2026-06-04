#!/usr/bin/env python3
"""L1 inference benchmark — directly measures Ollama eval_duration.

The key metric is Ollama's eval_duration field which represents actual
inference time (excluding queue wait and model loading).
"""
import json, time, os, sys, urllib.request, statistics

ENDPOINT = os.getenv("CA_LLM_ENDPOINT", "http://127.0.0.1:11437")

L1_PROMPT = """你是一个会议纪要摘要助手。请根据上一轮摘要和本轮对话，生成增量式的 OODA 摘要。

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
- 若某节无新信息，写"无"。
- 若整个增量无新信息，核心摘要写"无有效增量"，其余节写"无"。

【上一轮摘要】
{"core_change": "讨论了数据管道优化方案", "new_materials": ["A/B测试框架设计", "数据清洗流程"], "objective_facts": ["系统延迟约200ms", "日处理量10万条"], "consensus": ["采用Kafka作为中间件"], "todo": ["编写接口文档", "部署测试环境"]}

【本轮对话】
用户：我们讨论一下数据管道的性能优化方案。
助手：好的，目前数据管道的主要瓶颈在于序列化环节。建议采用Protocol Buffers替代JSON序列化，预计可以减少40%的延迟。
用户：这个改动对现有系统的影响如何？
助手：需要修改数据生产者和消费者的序列化代码，但接口层保持不变。建议在测试环境验证后分阶段上线。
用户：监控方面有什么建议？
助手：建议在管道关键节点添加Prometheus指标采集，包括吞吐量、延迟P99和错误率。可以设置告警阈值在P99超过500ms时触发通知。
用户：好，那就按这个方案推进。

请生成本轮增量摘要："""


def call_and_measure(model, prompt, timeout=300, warmup=False):
    """Call Ollama and return timing data."""
    num_predict = 10 if warmup else 2048
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.3},
        "keep_alive": -1,
    }).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    wall_start = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    wall_elapsed = time.monotonic() - wall_start
    return {
        "wall_elapsed": wall_elapsed,
        "eval_duration_ns": data.get("eval_duration", 0),
        "load_duration_ns": data.get("load_duration", 0),
        "prompt_eval_duration_ns": data.get("prompt_eval_duration", 0),
        "eval_count": data.get("eval_count", 0),
        "prompt_eval_count": data.get("prompt_eval_count", 0),
        "response_len": len(data.get("response", "")),
        "response_preview": data.get("response", "")[:80],
    }


def format_dur(ns):
    if ns >= 1_000_000_000:
        return f"{ns/1_000_000_000:.2f}s"
    elif ns >= 1_000_000:
        return f"{ns/1_000_000:.0f}ms"
    else:
        return f"{ns/1_000:.0f}µs"


models = ["qwen3.5:hermes", "qwen3:4b", "bigatuna/sushi-coder:q4_k_m"]

print(f"L1 Generation Benchmark")
print(f"  Endpoint: {ENDPOINT}")
print(f"  Prompt: {len(L1_PROMPT)} chars")
print()

results = []
for model in models:
    print(f"[{model}]", flush=True)

    # Warmup: ensure model is loaded in memory
    print(f"  Warming up...", end=" ", flush=True)
    try:
        w = call_and_measure(model, L1_PROMPT, warmup=True)
        print(f"OK (wall={w['wall_elapsed']:.1f}s, load={format_dur(w['load_duration_ns'])}), cooldown 5s")
        # Small cooldown after warmup
        time.sleep(5)
    except Exception as e:
        print(f"WARMUP FAILED: {type(e).__name__}: {e}")
        results.append({"model": model, "status": "FAILED", "error": str(e)})
        continue

    # Benchmark runs
    runs = []
    for i in range(3):
        print(f"  Run {i+1}/3...", end=" ", flush=True)
        try:
            r = call_and_measure(model, L1_PROMPT, warmup=False)
            runs.append(r)
            ed = format_dur(r["eval_duration_ns"])
            ped = format_dur(r["prompt_eval_duration_ns"])
            print(f"OK wall={r['wall_elapsed']:.1f}s eval={ed} prompt_eval={ped} tokens={r['eval_count']}/{r['prompt_eval_count']}")
        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
        if i < 2:
            time.sleep(1)

    if runs:
        eval_times = [r["eval_duration_ns"] for r in runs]
        prompt_eval_times = [r["prompt_eval_duration_ns"] for r in runs]
        wall_times = [r["wall_elapsed"] for r in runs]
        avg_eval_ns = statistics.mean(eval_times)
        avg_prompt_ns = statistics.mean(prompt_eval_times)

        results.append({
            "model": model,
            "status": "OK",
            "runs": len(runs),
            "avg_eval_duration": f"{avg_eval_ns/1_000_000:.0f}ms",
            "avg_wall_elapsed": f"{statistics.mean(wall_times):.1f}s",
            "avg_prompt_eval_duration": f"{avg_prompt_ns/1_000_000:.0f}ms",
            "per_run_eval_ms": [f"{t/1_000_000:.0f}ms" for t in eval_times],
            "per_run_wall_s": [f"{t:.1f}s" for t in wall_times],
            "avg_eval_count": int(statistics.mean([r["eval_count"] for r in runs])),
            "avg_prompt_eval_count": int(statistics.mean([r["prompt_eval_count"] for r in runs])),
        })

# Summary
print()
print("=" * 100)
print(f"{'Model':<35} {'Avg Eval':<12} {'Avg Prompt':<14} {'Avg Wall':<10} {'Tokens Out':<10} {'Tokens In':<10} {'Runs':<6}")
print("-" * 100)
for r in results:
    if r["status"] == "OK":
        print(f"{r['model']:<35} {r['avg_eval_duration']:<12} {r['avg_prompt_eval_duration']:<14} {r['avg_wall_elapsed']:<10} {r['avg_eval_count']:<10} {r['avg_prompt_eval_count']:<10} {r['runs']:<6}")
    else:
        print(f"{r['model']:<35} {'FAILED':<12} {r.get('error', '?'):<50}")
print("=" * 100)

# JSON output
json.dump(results, sys.stdout, indent=2, ensure_ascii=False)
print()
