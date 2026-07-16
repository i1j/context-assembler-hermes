---
title: 已拒绝方案
slug: rejected-approaches
category: architecture
version_introduced: v5.0
status: 已拒绝
decisions: ["rejected-approaches"]
depends_on: []
updated: 2026-06-29
---

## 问题

CA 开发过程中评估并拒绝了多个技术方案。统一记录这些方案的拒绝理由，避免未来重新评估时重复劳动。

## 决策

### 已拒绝方案列表

| 方案 | 评估版本 | 拒绝理由 |
|------|----------|----------|
| **conv_encoding**（Jaccard + 三级替换 + conv_encoding blob 上下文编码） | v5.0 | 性能差、有损、不透明。替换为 (turn,seq) + 三列明文字段（Elm/Fct/Hdl） |
| **turn_cache 四主键** `(session_id, turn_index, api_call_count, seq_index)` | v5.0 | 主键复杂度过高，API 序号与对话轮序号耦合，改为简单的 `(turn, seq)` |
| **ToolBuffer 缓冲写入** | v5.0 | 增加复杂度，E-stage 写即落盘即可满足需求 |
| **水位压力话题分割**（累积分隔短语打分触发分割） | v5.7 | 边界不稳定，已物理删除 |
| **`_compute_topic_groups` 全量分割** | v5.7 | O(n²) 性能差，已物理删除 |
| **全量每轮重分割** | v5.5 | O(n²) 复杂度，话题变化通常不密集 |
| **sqlite-vec 向量检索** | v4.x | 外部依赖管理复杂、二进制兼容性问题 |
| **BM25 检索** | v4.x→v5.0 | v5.0 后不再需要 BM25 索引，检索功能从 CA 中移除 |
| **每轮全量重建增量缓存** | v5.8 | 性能差，全量替换为增量 + Fct-pending 防护 |
| **同步 LLM 摘要** | v5.2 | 阻塞用户路径，摘要生成应在后台进行 |
| **bg_review LLM 摘要** | v5.5 | 浪费时间，bg_review 内容已存在可直接填充 |
| **CE engine pass-through 空壳** | v5.0 | 空壳占用 pipes 资源，v5.0 移除 CE shell |
| **惰性缓存不过期** | v5.8 | 数据不一致，话题切换后缓存可能指向错误上下文 |
| **L2/L1/L0 术语保留** | v5.5 | 用户反复纠正，统一为 Elm/Fct/Hdl |
| **固定半径 grade (单阈值)** | v5.5 | 不灵活，不同话题需要不同替换粒度 |
| **按时间距离定级** | v5.5 | turn 序号差不反映语义距离 |

### 拒绝原则

1. **性能**：引入 O(n²) 或多轮 O(n) 的立即拒绝
2. **外部依赖**：需要额外数据看护或二进制兼容的慎重评估
3. **不可逆有损**：conv_encoding 一类的有损编码被拒绝
4. **用户路径阻塞**：在 pre_llm_call 路径上引入 LLM 调用的被拒绝
5. **增加复杂度无相应收益**：ToolBuffer、数据空洞后被拒绝
6. **术语混乱**：L2/L1/L0 被统一术语取代
