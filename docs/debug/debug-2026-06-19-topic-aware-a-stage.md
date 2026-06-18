# 调试记录 2026-06-19 — Topic-Aware A-stage 降级规划

**日期**: 2026-06-19
**提交**: （未提交）

## 问题

当前 A-stage（`_simple_mutation_mode_v5`）对保护区外的所有对话轮一刀切替换为 Fct：
- 无话题感知，无等级区分，代码正确运行但很「笨」
- 整个 conversation_history 对老旧历史对话轮的 Elm/Fct 映射与当前提问无关
- 一切原始 Elm 直接丢弃

## 目标

引入话题分级降级：
- 保护区外对话轮，按话题**与当前提问的用户话题切换时的语义距离**，分 L0(Hdl)/L1(Fct)/L2(Elm) 三级
- 仅**话题切换时**触发一次形心定级，grades 冻结到下一次切换 → 每轮 LLM 调用 conversation_history 不变 → 云端 prompt caching 命中稳定

## 方案

### 新文件：`topic_manager.py`

| 组件 | 功能 |
|------|------|
| `TopicGradeManager` | 增量话题分割 + switch 检测 + grade 缓存 |
| `_jaccard_text()` | CJK 字 + 二元组 + 英文词 Jaccard 相似度 |
| `_scan_forced_split_phrases()` | 强制短语（换个话题/聊点别的/另一个等） |
| `_grade_topics_by_radius()` | 三级定级（半径公式复用旧代码） |
| `_compute_centroids_from_fct()` | 切换时 embed 旧话题成员 Fct → 形心 |

### 数据流

```
pre_llm_call_v5()
  ├─ 1. _topic_mgr.detect()         增量 Jaccard 匹配（累积文本）
  ├─ 2. 若 switch → _topic_mgr.grade_on_switch(q_emb)
  │       └─ 旧话题形心 → q_emb 距离 → _grade_topics_by_radius → L0/L1/L2
  └─ 3. _simple_mutation_mode_v5   按 topic_id→grade 分叉替换
```

### 增量话题分割设计

每个话题维护**累积 Fct 文本**（`_topic_text_profiles`）：

```
T1 accum:  "轮1 Fct ... 轮2 Fct ... 轮3 Fct"  ← 持续追加
新轮 Fct → Jaccard(curr, T1_accum)
  ≥ ENTRY(0.02) → T1 延续，Fct 追加到累积
  < ENTRY → T2 新话题
```

解决相邻轮 Fct 太不同但同话题的问题——累积文本越长，特征越丰富，同话题后续轮自然命中。

## 真实数据测试结果

### Session 1: 34 turns（Fct 链设计多话题讨论）

```
turn    topic     grades                user_msg
 1-4    T1 [L1]                         阅读CA插件的agents.md→考察todo/state
 5-8    T2 [L1]                         BJ/state + OODA结构讨论
 9-11   T3 [L1]                         链式结构提示词变更
12-20   T4 [L2]  ← 核心设计，9轮聚合    Fct 链式结构设计（多OODA+链ID）
21-23   T5 [L1]                         试验搁置+补充报告
24-31   T6 [L2]  ← "换个话题"强制执行   CA 三源输出质量检查（8轮）
32      T7 [?]                          "完成了吗"
33      T8 [?]                          "检查代码"
34      T9 [?]                          "写调试报告"
```

稳定性（grades 变化次数 = topic switch 次数 = 8）✅
> 评估：6 次真实语义切换（T4→T5 弱切换）+ 3 次结尾微话题。核心段落 Fct 链讨论 9 轮聚合正确。

### Session 2: 21 turns（CA 测试验证）

全量 1 话题。✅ — 全部同主题，正确。

### Session 3: 19 turns（Hermes 配置→CA 测试→BG 讨论）

```
T1 [L1]: turns=[1-18]  ← 漏切（配置→CA 测试在 turn 7）
T2 [L2]: turns=[19]     skill update
```

⚠️ 漏切点 turn 7：从「改回云端」到「三源检测CA插件」的语义变化未被 Jaccard 捕获（两者 Hermes/CA 词汇重叠高）。这是 Jaccard 纯字面匹配的固有缺陷，真实场景下用户自然语言的词汇差异通常比测试 session 更明显。

## 修改的文件

| 文件 | 改动 |
|------|------|
| `topic_manager.py` | **新增** ~260 行。完整话题管理模块 |
| `__init__.py` | **修改** -90 行。导入 TopicGradeManager + init + _simple_mutation_mode_v5 分叉替换 |

### topic_manager.py 结构

```
TopicGradeManager
  ├─ __init__(embed_client, session_id, store)
  ├─ detect(turn, ca_rows, user_msg)         → bool (switch?)
  ├─ grade_on_switch(turn, user_msg)          → dict[topic_id→grade]
  ├─ get_turn_topic(turn)                     → topic_id
  ├─ get_grades()                             → dict[topic_id→grade]
  ├─ reset()
  │
  ├─ _assign_topic(turn, ca_rows, user_msg)   → topic_id (Jaccard 累积匹配)
  ├─ _extract_turn_fct(ca_rows)               → str (代表性 Fct)
  ├─ _init_topic_data()
  ├─ _compute_centroids_from_fct()
  └─ ... 内部缓存: _turn_to_topic, _topic_data, _topic_grades, _topic_text_profiles
```

### __init__.py 改动点

| 行 | 内容 |
|----|------|
| ~27 | `from .topic_manager import TopicGradeManager` |
| ~80 | `self._topic_mgr = TopicGradeManager(...)` 初始化 |
| ~400 | 替换 `content = ca_tools[tj]` → 按 topic grade 分叉 L2/L1/L0 |
| 新增 | pre_llm_call 中 detect → switch → grade_on_switch 链路 |

## 对比测试（240 pass, 19 skip）

全部 ca_assembler 测试通过，无退化。

## 未决事项

1. **Session 3 漏切** — Jaccard 在词汇重叠高的相邻话题间偶有漏切。可考虑将强制短语列表扩展（"检测"、"分析"等新上下文暗示词），或接受当前精度——测试会话的词汇重叠度高于真实对话。
2. **Hdl 列填充** — 目前 turn_stream.Hdl 是否有值未实测验证。若 L0 降级时 Hdl 为空，会取 100 字截断 El。
3. **形心半径 r 的合理性** — 旧代码 `_grade_topics_by_radius` 的半径公式源自 batch 环境，在增量场景的切换定级中是否仍合适需线上验证。
