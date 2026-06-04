"""
端到端部署测试: C-stage(process_turn_async) → 存储 → A-stage(pre_llm_call)
通过 plugins/context_engine/ca_assembler 插件包装层。
"""
import json, os, shutil, sys, time

# 加到 Hermes 的 Python path
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

# 覆盖超时 + fallback
os.environ["CA_LLM_TIMEOUT"] = "300"
os.environ["CA_EMBED_BACKEND"] = "fallback"
os.environ["CA_EMBED_ENDPOINT"] = "http://localhost:11435"
os.environ["CA_LLM_ENDPOINT"] = "http://localhost:11435"

from ca import session_manager
from plugins.context_engine.ca_assembler import CAContextAssemblerPlugin

plugin = CAContextAssemblerPlugin()
plugin.on_session_start(
    "deploy_e2e",
    context_length=200000,
    hermes_home="/tmp/ca_deploy_e2e",
)
engine = plugin._engine
store = engine.store

# ── C-stage ──
print("=== C-stage (process_turn_async, ~240s) ===", flush=True)
t0 = time.monotonic()
turn_idx = engine.process_turn_async(
    "How to configure database?",
    "Edit config.yaml and restart the service.",
    [],
)
engine.wait_for_pending(310)
l1_str = engine.cache.l1_texts.get(turn_idx, "{}")
elapsed = time.monotonic() - t0
print(f"  {elapsed:.0f}s  turn={turn_idx}", flush=True)

# ── 存储验证 ──
recs = store.read_session("deploy_e2e")
print(f"\n=== 存储 ===", flush=True)
print(f"  记录数: {len(recs)}", flush=True)
for r in recs:
    print(f"  轮{r['turn_index']}: L0={r['l0_text'][:60]}", flush=True)

# ── A-stage ──
msgs = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "How to configure DB?"},
    {"role": "assistant", "content": "Edit config.yaml."},
    {"role": "user", "content": "Tell me about DB config"},
]
result = plugin.pre_llm_call(msgs, user_input="Tell me about DB config", context_length=200000)
print(f"\n=== A-stage ===", flush=True)
print(f"  pre_llm_call: {len(msgs)}→{len(result)}", flush=True)
markers = sum(1 for m in result if "[~/" in str(m.get("content", "")))
print(f"  [~/N] markers: {markers}", flush=True)

# ── 清理 ──
plugin.on_session_end()
shutil.rmtree("/tmp/ca_deploy_e2e", ignore_errors=True)
print("\n✅ C-stage → 存储 → A-stage 端到端通过", flush=True)
