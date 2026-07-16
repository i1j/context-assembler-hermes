|---
|title: 多 OODA 分治摘要设计
|slug: multi-ooda-arch
|category: architecture
|version_introduced: v6.2
|status: 已设计
|decisions: ["multi-ooda-per-topic-summary"]
|depends_on: ["f-stage-async-summary", "fct-format-evolution", "topic-segmentation"]
|updated: 2026-07-04
|source_files: ["ca/f_stage.py", "topic_manager.py"]
|---

## 问题

F-stage 为每个 fin 行生成一份 OODA（`changes` 列表），将本轮增量 Elm 中所有事项混合。A-stage 消费 Fct 时只能以整行为粒度获取，无法区分其中不同话题的变更。

## 方案

### 数据流

```
post_llm_call_v5
  │
  ▼
process_turn_f_stage(turn, fin_seq)
  │
  ├─ read_incremental_elm() → elm_text
  ├─ read_prev_fct() → prev_fct (含 _topics)
  │
  ▼
_run_multi_ooda(elm_text, prev_fct)
  │
  ├─► Step A: 已知话题逐一提取
  │
  │   for each (topic_id, topic_data) in prev_fct._topics:
  │     │
  │     ├─ 构建 topic_prev = format_topic_prev(topic_data)
  │     ├─ prompt = "只提取与【{topic_title}】相关的变更"
  │     ├─ LLM(topic_prev, elm_text) → topic_ooda
  │     └─ Fct._topics[topic_id] = topic_ooda
  │
  ├─► Step B: 剩余新话题检测
  │
  │   ├─ prompt = "检查未覆盖的新内容"
  │   ├─ LLM(elm_text) → new_topic_ooda(s)
  │   ├─ 无新内容 → 跳过
  │   └─ 有新内容 → Fct._topics[new_ids] = new_ooda
  │
  └─► Step C: 合并 + 写入
      │
      ├─ Fct.changes = union of all _topics[*].changes
      ├─ Fct.core_change = 拼接所有 topic core_change
      ├─ Hdl = 拼接所有 topic hdl
      └─ _update_fct_v5(session_id, turn, fin_seq, fct_str, hdl_text)
```

### 已知话题识别

`prev_fct._topics` 中的话题来源：

```
┌─ 之前 fin 的多 OODA 输出 → _topics 中的已知话题
├─ TopicGradeManager._turn_to_topic 中的话题 ID 映射
└─ 首轮/重启 → _topics 为空 → 跳过 Step A，直接 Step B
```

### 线程模型

```
F-stage daemon thread (_run_f_stage)
  │
  ├─ spawn Step A: 已知话题提取线程池 (max_workers=N)
  │     ├─ Thread-1: LLM(topic_0)
  │     ├─ Thread-2: LLM(topic_1)
  │     └─ ...
  │
  ├─ join(所有 Step A 线程) → 收集结果到 _topics
  │
  ├─ spawn Step B: 剩余检测 (单线程)
  │
  └─ Step C: 合并 + 写入 (主线程)
```

**配置项**（新增）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CA_MULTI_OODA_ENABLED` | `false` | 多 OODA 开关（逐步上线） |
| `CA_MULTI_OODA_MAX_WORKERS` | `3` | 并行 LLM 最大线程数 |
| `CA_MULTI_OODA_MAX_TOPICS` | `5` | 单轮最大话题数上限 |
| `CA_MULTI_OODA_SEGMENT_TOKENS` | `600` | 每话题 OODA 输出 token 预算 |

### 回退策略

| 条件 | 行为 |
|------|------|
| `_topics` 为空（首轮/重启） | 跳过 Step A，直接 Step B → 单 OODA |
| 已知话题数 > `CA_MULTI_OODA_MAX_TOPICS` | 退化为单 OODA |
| Step B 输出含已知话题标题 | 去重合并到已有 _topics |
| LLM 所有调用均超时/失败 | 退化为现有 fallback Fct |
| `CA_MULTI_OODA_ENABLED=false` | 原有单 OODA 路径，无变化 |

## 与现有系统的关系

### 前向兼容：旧 DB 数据

旧 turn_stream 中 fin 行的 Fct 不含 `_topics` 键。`_run_multi_ooda` 检测到 `prev_fct._topics` 为空时，直接走 Step B（单 OODA + 剩余检测）。旧数据可被新代码正常读取，新写入的 Fct 自动带上 `_topics`。

### 后向兼容：旧消费者

| 消费者 | 读取路径 | 影响 |
|--------|---------|------|
| `_select_content` (a_stage.py) | `json.loads(Fct)` → 顶层 `changes` | 无感 |
| `_extract_hdl` (f_stage.py) | `fct_dict.get("changes", [])` | 无感 |
| `clean_increment` (post_process.py) | `data.get("changes", [])` | 无感 |
| `_json_to_v1_markdown` (post_process.py) | `data.get("changes", [])` | 无感 |
| `format_previous_summary_for_prompt` (store.py) | `data.get("changes", [])` | 无感 |
| `cache.add_turn` (cache.py) | 存整份 fct_text | 无感 |
| BM25 索引 | `fct_texts.values()` | 无感 |

### 未来：A-stage 话题感知消费

`_topics` 可被 `_select_content` 用于更精准的 Fct 选取：当 `target_grade=Grade.FCT` 时，不仅返回 `json.loads(Fct)` 整个对象，而是优先返回当前 turn 所属话题的独立 OODA。

## 性能评估

| 场景 | 当前（单 OODA） | 多 OODA（3 话题） |
|------|----------------|-------------------|
| LLM 调用次数 | 1 | 4（3 已知 + 1 剩余） |
| 总延迟（串行） | ~4s | ~16s |
| 总延迟（并行 3 线程） | ~4s | ~8s |
| 输出 token 总量 | ~150 | ~450 |
| 输入 token 增量 | 基准 | ~2x（每次带完整 Elm） |

F-stage 为 daemon 线程，不阻塞用户路径。延迟叠加在可接受范围内。

## 变更记录

- **v6.2** (2026-07-04)：初始设计
- 10 样本实测：qwen3-4b 话题识别 3.1/轮，真实内容率 100%，标签噪声 19%（PAIR_LOOSE 覆盖）
