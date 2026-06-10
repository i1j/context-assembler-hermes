#!/usr/bin/env python3
"""
CA Injection replace 模式验证脚本

CA 注入的三种模式中，只有 replace 改变 LLM 看到的 conversation_history：
  replace → 替换/移除 history 条目（CA 摘要替代原文）
  append  → 返回文本注 user message（history 原文透传）
  off     → 不返回任何内容       （history 原文透传）

本脚本只验证 replace 模式下 conversation_history 的变化。

运行：
  cd plugins/ca_assembler
  python -m pytest tests/debug_injection_demo.py -v -s
"""

import json
from copy import deepcopy

from ca import TurnPlanEntry


def test_replace_mode(ca_engine):
    """replace: 1:1 对齐注入，history 条目被 CA 摘要替换"""
    engine = _setup_engine(ca_engine)
    plan = _make_plan()
    history = _make_history()

    outcomes = engine._build_aligned_outcomes(plan, history)
    mutated = _apply_outcomes(outcomes, history)

    print("  ┌─ User 原始输入")
    for i, m in enumerate(history):
        if m.get("role") == "user":
            c = m.get("content", "")[:80]
            print(f"    [{i}] {c}")
    print("  └─")

    print("  ┌─ 注入前 conversation_history（Hermes 原始输出）")
    for m in history:
        slim = {"role": m["role"]}
        if m.get("tool_calls"):
            slim["tool_calls"] = [{"id": t.get("id")} for t in m["tool_calls"]]
        c = m.get("content", "")
        if len(c) > 120:
            slim["content"] = c[:117] + "..."
        elif c:
            slim["content"] = c
        if m.get("name"):
            slim["name"] = m["name"]
        print(f"  {json.dumps(slim, ensure_ascii=False)}")
    print("  └─")

    print("  ┌─ 注入后 conversation_history（CA replace）")
    for m in mutated:
        slim = {"role": m["role"]}
        if m.get("tool_calls"):
            slim["tool_calls"] = [{"id": t.get("id")} for t in m["tool_calls"]]
        c = m.get("content", "")
        if len(c) > 120:
            slim["content"] = c[:117] + "..."
        elif c:
            slim["content"] = c
        if m.get("name"):
            slim["name"] = m["name"]
        print(f"  {json.dumps(slim, ensure_ascii=False)}")
    print("  └─")

    print("  ┌─ 行级对比")
    print(f"  {'#':<4} {'role':<18} {'action':<12} {'原文':<36} {'注入后'}")
    print(f"  {'':-<4} {'':-<18} {'':-<12} {'':-<36} {'':-<36}")
    for i, (msg, outcome) in enumerate(zip(history, outcomes)):
        role = msg["role"]
        if msg.get("tool_calls"):
            role += " [tc]"
        action = (
            "\U0001f489 替换" if outcome and outcome != ""
            else ("- 移除" if outcome == "" else "\U0001f3af 保留")
        )
        c = msg.get("content", "")[:34]
        r = (outcome or "")[:34]
        print(f"  [{i:<2}] {role:<18} {action:<10} {c:<36} {r:<36}")
    print("  └─")


# ═══════════════════════════════════════════════════════════════
#  测试数据
# ═══════════════════════════════════════════════════════════════


def _setup_engine(ca_engine):
    """写入 v5 store + 填充 cache"""
    e = ca_engine
    e.store.write_turn("ref", 1, role="user", content="你好，请查一下文件")
    e.store.write_turn("ref", 2, role="user", content="读取 config.yaml")
    e.store.write_turn("ref", 2, role="assistant",
        content=json.dumps({"thought": "看看配置"}),
        api_call_count=1, seq_index=0,
        tool_calls_json=json.dumps([
            {"id": "c1", "function": {"name": "read_file",
                                       "arguments": json.dumps({"path": "config.yaml"})}}
        ]), finish_reason="tool_calls")
    e.store.write_turn("ref", 2, role="tool",
        content=json.dumps({"result": "port=8080"}),
        api_call_count=1, seq_index=1,
        tool_call_id="c1", tool_name="read_file", status="ok")
    e.store.write_turn("ref", 2, role="assistant",
        content="配置文件端口是8080", api_call_count=999999, seq_index=0)

    e.cache.l1_texts[1] = json.dumps(
        {"core_change": "打招呼问候", "ooda_observe": ["用户问好"],
         "ooda_act": ["回复"], "assess_state": "DONE"}, ensure_ascii=False)
    e.cache.l0_texts[1] = "问候"
    e.cache.l1_texts[2] = json.dumps(
        {"core_change": "收到读取请求", "ooda_observe": ["用户要读配置"],
         "assess_state": "PLANNED"}, ensure_ascii=False)
    e.cache.l0_texts[2] = "读取请求"
    e.cache.tool_group_l1_texts[(2, 1)] = json.dumps(
        {"group_intent": "读取配置文件", "group_result": "read_file: port=8080",
         "tool_count": 1, "state": "ok"}, ensure_ascii=False)
    e.cache.tool_group_l0_texts[(2, 1)] = "read_file: ok"
    e.cache.tool_l1_texts[(2, 1)] = json.dumps(
        {"tool_name": "read_file", "result_summary": "port=8080", "status": "ok"},
        ensure_ascii=False)
    e.cache.tool_l0_texts[(2, 1)] = "port=8080"
    return e


def _make_plan():
    return [
        TurnPlanEntry(turn_index=1, turn_type="dialogue", api_call_count=0,
                      seq_index=0, target_level="L1", decision_reason="tail"),
        TurnPlanEntry(turn_index=2, turn_type="dialogue", api_call_count=0,
                      seq_index=0, target_level="L1", decision_reason="tail"),
        TurnPlanEntry(turn_index=2, turn_type="tool_group", api_call_count=1,
                      seq_index=0, tool_sub_index=1, target_level="L1",
                      decision_reason="dialogue_downgrade"),
    ]


def _make_history():
    return [
        {"role": "user", "content": "你好，请查一下文件"},
        {"role": "assistant", "content": "好的，我查一下"},
        {"role": "user", "content": "读取 config.yaml"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}], "content": "要读配置"},
        {"role": "tool", "content": '{"result": "port=8080"}',
         "name": "read_file", "tool_call_id": "c1",
         "_api_call_count": 1, "_seq_index": 1},
        {"role": "assistant", "content": "配置文件端口是8080"},
    ]


def _apply_outcomes(outcomes, history):
    mutated = deepcopy(history)
    for i in range(len(outcomes) - 1, -1, -1):
        o = outcomes[i]
        if o is None:
            continue
        if o == "":
            mutated.pop(i)
            continue
        mutated[i]["content"] = o
    return mutated
