---
title: 话题检测与定级
slug: topic-management
category: architecture
version_introduced: v5.5
status: 已实装
decisions: [topic-grade-manager, bg-review-sync]
depends_on: [store]
updated: 2026-07-31
source_files: ["topic_manager.py"]
---

## 问题

需要确定每轮对话所属的话题等级（ACT/REL/FAR），以决定 A-stage 对该轮采用何种摘要策略。旧方案使用 `_compute_topic_groups` O(n²) 全量分割，性能差且边界不稳定。

## TopicGradeManager

- **detect(turn, ca_rows, user_msg)**：pre_llm_call 中调用，检测当前话题
- **3 种等级**：ACT（当前活跃）、REL（历史相关）、FAR（无关）
- **切换检测**：新旧话题相似度 < 阈值 → `grade_on_switch()` → 旧话题块排队
  `_run_topic_summarize`（strand 生成 + reality 归并）
- **缓存**：增量缓存 topic→grade 映射，切换间冻结保障 prompt caching 稳定
- **bg_review 跳过**：检测到 bg_review 会话时完全跳过话题处理

## 话题摘要与 reality 归并

话题切换时旧话题块排队后台 `_run_topic_summarize`：读各轮 Fct → 4B 生成 strand →
同步 `run_reality_merge` 归并到现实工作对象。OV 话题提交（`_fire_ov_submit`）
已删除，本地 strand_summaries + realities 表替代。

## Jaccard 分割输入归一化（v6.3）

`_assign_topic` 用 `_jaccard_text` 对比当前轮 Fct 与话题累积文本（阈值
`TOPIC_JACCARD_ENTRY=0.04` / `CHAIN=0.08`）。Fct 以 JSON 存储，公共键名
（`core_change`/`changes`/`stage_tag`/`_assemble_status`）与元数据值
（stage_tag 状态、`_assemble_status` 数字）对**所有话题恒定**，直接 tokenize
会把无关话题的 Jaccard 抬过阈值——"数据库设计" vs "Python优化" 原始 JSON
j=0.0714 ≥ 0.04 → 虚假延续（同源缺陷族：Jaccard 反例铁则）。

修复：`_extract_turn_fct` 返回前经 `_extract_fct_semantic_text` 归一化——
递归提取 JSON 字符串 value（剥键名），跳过 `_FCT_METADATA_KEYS`
（`stage_tag`/`ooda`/`_assemble_status`/`_truncated`）与非字符串值。
仅剥离键名不够（`_assemble_status:0` 的数字 0 仍共享，j≈0.074 ≥ 0.04）。
非 JSON 文本（原始用户消息/旧格式/损坏 JSON）原样返回。

归一化必须发生在提取/累积点而非比较点：累积 profile 是 `{json} {json}`
拼接，非合法 JSON，比较时再解析必然失败。
