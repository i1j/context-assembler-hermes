|---
|title: 多话题 OODA 分治摘要（Multi-OODA Per-Topic Summary）
|slug: multi-ooda-per-topic-summary
|category: decision
|date: "2026-07-04"
|version_introduced: v6.2
|alternatives: ["单次 LLM 输出多话题 OODA", "Elm 物理分割后分别摘要", "保持现状（单 OODA 混合输出）"]
|chosen: "已知话题逐一提取 + 剩余新话题检测，完整 Elm 不分割"
|affects: ["15-multi-ooda-arch", "03-f-stage-async-summary", "11-fct-format-evolution", "24-fct-changes-format"]
|status: 已设计
|---

## 触发条件

F-stage 当前为每个 fin 行生成一份 OODA（`changes` 列表），将本轮增量 Elm 中涉及的所有事项混在一起输出。当单个 fin 行覆盖多个独立话题时（如同轮讨论「连接池」和「日志模块」），OODA 的 `changes` 列表将多事项混合，无法按话题隔离消费。

## 备选方案

1. **单次 LLM 输出多话题 OODA** — prompt 要求 LLM 一次输出以 `## 话题：` 分割的多份 OODA。实测可用，但有局限：
   - 标签噪声（`<stage及_tag>`）在 10 样本中发生率 19%
   - LLM 内部处理多话题间的内容重叠，不可控
   - 单次输出的 token 预算需要更大（已实测部分 samples 倒 `num_predict=800` 上限触发 `length`）

2. **Elm 物理分割后分别摘要** — 对 Elm 文本做字符串级话题分割（`str.split` / 正则），每段送 LLM 摘要。**已拒绝**：
   - 同一段落可能属于多个话题（交叉引用、因果依赖）
   - 「日志用了连接池参数」→ 删则丢上下文，不删则重叠
   - 工具输出一行结果被两个 topic 需要
   - 决策耦合：共同结论（「都改完再部署」）被撕裂

3. **保持现状（单 OODA 混合输出）** — 不做多话题摘要。缺点是 A-stage 无法按话题隔离消费 Fct。

4. **已知话题逐一提取 + 剩余新话题检测（选定）**

## 选定

### 核心原则

- **Elm 不分割** — 每次 LLM 调用都看到完整增量 Elm，LLM 自主决定提取范围
- **已知话题循环** — 按 `prev_fct._topics` 中的话题逐一提取
- **剩余检测** — 全部已知话题提取后，检查 Elm 中是否还有未覆盖的新内容
- **零 schema 变更** — 多 OODA 存储在现有 `Fct` 列的 `_topics` 键中

### 流程

```
_on_post_llm_call_v5 → process_turn_f_stage(turn, fin_seq)
  → read_incremental_elm() → elm_text
  → read_prev_fct() → 含 _topics 的 prev Fct
  ↓
  _run_multi_ooda(elm_text, prev_fct)
  │
  ├─ for each topic_id, topic_prev in prev_fct._topics.items():
  │     prompt = "从对话中只提取与【{topic_title}】相关的变更"
  │     LLM(topic_prev, elm_text) → topic_OODA
  │     topic_OODA 写入 Fct._topics[topic_id]
  │
  ├─ prompt = "检查上述话题未覆盖的剩余新内容"
  │     LLM(elm_text) → new_topic_OODA(s)
  │     new_topic_OODA 写入 Fct._topics[new_id]
  │
  └─ 合并 Fct.changes = union of all _topics[*].changes
  ↓
  _update_fct_v5(session_id, turn, fin_seq, fct_str, hdl_text)
```

### 数据格式（零 schema 变更）

```python
# Fct 列（当前 JSON 格式，后向兼容）
Fct = {
    "changes": [                          # ← 合并版（所有话题的 changes 合集）
        {"stage_tag": "已实施", "core_change": "..."},
    ],
    "core_change": "...",
    "_topics": {                           # ← 新增键，旧消费者忽略
        "0": {
            "title": "连接池",
            "changes": [{"stage_tag": "已实施", "core_change": "超时调至30s"}],
            "hdl": "超时从60s调至30s"
        },
        "1": {
            "title": "日志模块",
            "changes": [{"stage_tag": "评估中", "core_change": "重构异步写入"}],
            "hdl": "需消息队列缓冲，先评估兼容性"
        }
    }
}

# Hdl 列
Hdl = "连接池: 超时调至30s；日志: 重构异步写入"
```

**后向兼容**：现有读取 `json.loads(Fct).changes` 的消费者（`_select_content`、`_extract_hdl`、`clean_increment`、`_json_to_v1_markdown`）全部在顶层工作，`_topics` 为可选新增键。

### Prompt 设计

**已知话题提取 prompt**：

```
从以下对话中只提取与【{topic_title}】相关的变更。
忽略其他话题的内容。引用对话中的具体事实。

【历史摘要（本话题）】
{topic_prev_summary}

【本轮对话】
{full_elm_text}

输出格式：
<stage_tag>【状态】</stage_tag>
<core_change>具体变更内容</core_change>
```

**剩余检测 prompt**：

```
以下对话已提取了以下话题：
{topic_list}

请检查对话中是否有上述话题未覆盖的新内容。
如果有新内容，以 <stage_tag>/<core_change> 格式输出。
如果没有，输出：无剩余新内容

【本轮对话】
{full_elm_text}
```

### 宽松正则

因 qwen3-4b 有时输出 `<stage及_tag>`、`<stage的标签>` 等变体，增加 fallback 正则（优先级：严格 > 宽松 > 无）：

```python
# 严格（原 PAIR_PATTERN）
PAIR_STRICT = re.compile(
    r'<stage_tag>\s*【([^】]+)】\s*</stage_tag>\s*'
    r'<core_change>\s*(.*?)\s*</core_change>',
    re.DOTALL | re.IGNORECASE
)

# 宽松（容忍 tag 名中含中文）
PAIR_LOOSE = re.compile(
    r'<stage[^>]*>\s*【?([^】\n]+)】?\s*</stage[^>]*>\s*'
    r'<core_change>\s*(.*?)\s*</core_change>',
    re.DOTALL | re.IGNORECASE
)
```

## 实测验证

用 10 个真实历史样本（从 423 个 ca_cache DB 提取）对 qwen3-4b 测试：

| 指标 | 值 |
|------|-----|
| 真实内容提取 | ✅ 10/10（无模板文字） |
| 平均话题数 | 3.1 /轮 |
| 平均耗时 | 3.9s /次 |
| 标签噪声率 | 19%（PAIR_LOOSE 可覆盖） |
| 失败（error/timeout） | 0 |

## 优点

- ✅ 不分割 Elm → 无语义撕裂，交叉引用保留
- ✅ 零 schema 变更 → 当前 turn_stream 直接承载
- ✅ 后向兼容 → 旧消费者读顶层 `changes` 无感
- ✅ 话题级历史摘要 → 每 topic 专用前文，减少 LLM 上下文噪声
- ✅ 逐步升级 → 可先部署单 OODA 模式，再开启多 OODA

## 约束 / 已知问题

- N 次 LLM 调用（N = 已知话题数 + 1）→ 延迟叠加（当前实测 3.9s/话题，3 话题 = ~12s）。F-stage 为 daemon 线程不阻塞主路径，可接受。
- 已知话题提取时 LLM 可能引用其他话题的内容（交叉引用自然）→ 不影响，最终存储时按话题隔离。
- 新话题检测可能产生误报（将已知话题的细节判定为新话题）→ 通过 `topic_title` 匹配过滤。
- 无话题历史（首轮/重启）时退化为单 OODA + 剩余检测（退化安全）。
