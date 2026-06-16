# A-stage 角色队列匹配实现记录

## 日期
2026-06-17

## 背景

旧版 `_simple_mutation_mode_v5` 用「每轮依次 seq++」的方式匹配 conv_hist 行到 CA turn_stream 行。
当 state.db 和 CA 数据行数不一致时（某轮多几行或少几行），seq 对齐断裂，导致：
- 错位行注入错误的 Fct（角色不匹配，内容完全错位）
- 或者报"校验错误"/"数据错误"的中文标记进 LLM 上下文

## 方案演进

1. **校验错误防御**（初版）：每行读 CA role 比对，不对就填"校验错误" → 太保守，多余 token
2. **fin 锚定 + 前向扫描**（第二版）：用 fin 行做 CA 边界，逐行前向扫 → fin 不应做边界（turn 边界是 user），且扫描逻辑复杂
3. **角色队列匹配**（终版，当前实现）：按 turn 分组，从 CA 拉数据分 role 队列，conv_hist 行依角色从对应队列中取

## 终版逻辑

```
Phase 1 — 分组
  conv_hist 非 user 行按 turn 收集，每行标 type: thought | tool | fin

Phase 2 — 队列匹配
  每轮：
    get_turn_ca_rows(store, sid, turn) → 拉该轮全部 CA 行
    分两队列：
      ca_thoughts = [asst 行 Fct]（排除 finish_reason='stop' 的 fin 行）
      ca_tools    = [tool 行 Fct]
    逐行匹配 conv_hist 的 thought → ca_thoughts[N]，tool → ca_tools[M]
    队列用尽 → 保留 Elm，队列有余 → 自然孤行
    fin 行不注入
```

## 改动文件

| 文件 | 变动 |
|------|------|
| `__init__.py` | 重写 `_simple_mutation_mode_v5()`（~60 行，替换旧逐行 seq 逻辑） |
| `ca/store.py` | `get_turn_ca_rows()` 增加 `tool_calls_json` 输出列 |

## 已知风险（用户已确认接受）

R1 — 跨 API call 交叉注入：当 CA 和 state.db 各丢一行且互为不同行时，同 role 队列匹配可能跨 API call 配错。概率极低，影响不大。
