# CA 插件当前代码状态技术文档（reality 化后，供 Codex/开发使用）

> 日期：2026-08-07 | 版本：v7（决策 37-41 已落地）
> 前版：`current-code-state-2026-08-06.md`（v6.5.4 CR 清单，历史归档保留）

## 1. 部署快照

- 插件路径：`~/.hermes/profiles/tester/plugins/ca_assembler/`（tester 开发库；winker/sysadmin 运行库已 sync-ca.sh 同步）
- 数据库：`~/.hermes/profiles/tester/ca_cache/ca_topics.db`（WAL 模式；备份 `ca_topics.db.bak_pre_reality_20260807`）
- 金标准库：`ca_cache/flash_pilot.db`（flash 重跑产物：111 reality + 837 s2r，导入生产后为基线）
- 会话库：`~/.hermes/profiles/tester/state.db`（块首提问回查源，turns[0] 定位）
- 测试基线：787 passed / 1 skipped / 1 xfailed
- 最近 commit：`ad463c8`（决策 41）/ `b51613e`（v7 注入统一）/ `656378f`（graphify 清理）

## 2. 架构与数据流（决策 41 后）

### 2.1 核心链路（reality 化）

```
用户提问 → pick_injection_realities（提问云形心 d=1−cos 主拣选）
           → θ_max=0.5 范围预筛 → 距离排序 top-15 → 4B 拣选 top-3
           → <wiki_carryover> 注入（reality 渲染）→ _candidate_themes 快照

话题切换（FAR）→ 旧话题打包 → summarize 生成 strand（写 strand_summaries）
           → run_reality_merge（S 匹配分候选[注入锚 S=0 必进] → 4B 决策 → merge/create）
           → realities upsert + strand_to_reality + query_centroid 增量
           → sync_realities_to_graph（reality_{id} 节点）+ record_block_cooccurrences + cooc 边
```

### 2.2 核心表（ca_topics.db）

| 表 | 状态 | 说明 |
|---|---|---|
| `realities` | ✅ 生产主表（111 行） | reality_id/name/hdl/current_status/timeline/source_strands/centroid_json/query_centroid_json/query_count/health 字段 |
| `strand_to_reality` | ✅（574 条） | strand_id → reality_id（PK strand_id） |
| `cooccurrence_events` | ✅（2 行） | reality_a/b 为**真 reality_id**（迁移完成） |
| `strand_summaries` | ✅ 活跃（~666 行） | 实时累积；92 个无归属 strand 待实时 merge |
| `themes` | 🧊 冻结归档（416 行） | 不再写入；`theme_strand_map` 停用 |
| `wiki_associations` | 遗留 | 仍读 themes（关联缓存，冻结后无害） |

### 2.3 关键配置

- `CA_TOPIC_SUMMARY_RECALL_LIMIT`（默认 3）：注入数量
- `TOPIC_SUMMARY_MAX_CHARS`（4000）/ `TOPIC_SUMMARY_MAX_TOKENS`（4096）
- 注入拣选常量（inject.py）：`THETA_MAX=0.5`、`QUERY_CLOUD_TOP_K=15`
- S 匹配分（config.py）：`CA_S_ALPHA/BETA/R` = 0.4/0.8/0.5

## 3. 测试命令

```bash
# 全量（推荐）
/usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider
# 注入/召回
/usr/bin/python3 -m pytest tests/plugin/test_plugin.py -q
# wiki 子图（realities）
/usr/bin/python3 -m pytest tests/store/test_wiki_graph_themes.py -q
```

## 4. 已知遗留项（非缺陷，过渡状态）

| 项 | 状态 | 处置 |
|---|---|---|
| 92 个无归属 strand（60 新块 + 32 低分） | 过渡 | 实时 merge 消化（run_reality_merge 自动归并） |
| 14 个 reality 无生产成员/形心 | 过渡 | 冷启动排除（注入跳过无形心 reality） |
| summarize 未快照 query_text | 缺口 | 提问云形心靠离线迁移 + merge 时 strand.query_text；生产 strand_summaries 无 query_text 列，后续快照改造 |
| `build_wiki_associations` 读 themes | 低风险 | wiki_associations 缓存表，冻结后无新数据 |
| winker/sysadmin 数据基线 | 待定 | 仅同步代码；数据迁移脚本 `migrate_reality_prod.py` 按 profile 参数化（winker 早期 flash_pilot.db 184KB / sysadmin 无） |

## 5. 迁移脚本（重跑入口）

```bash
# 幂等，可重跑（会 DELETE realities/s2r 重建 + cooc 转换 + 提问云形心重算）
/usr/bin/python3 scripts/migrate_reality_prod.py [--profile tester] [--flash <path>] [--no-embed]
```

## 6. 变更记录（决策 41 摘要）

| 变更 | 详情 |
|---|---|
| 数据迁移 | flash 111 reality 按块键映射导入；s2r 574（440 直连 + 134 centroid 语义匹配 cos≥0.55）；s2r↔source_strands 零不一致 |
| 注入 | `pick_injection_realities`（提问云形心 d=1−cos 主路径 + θ_max=0.5 + top-15 + 4B + jaccard 兜底）；实测 top-3 0.393（留一），KMeans 0.703 虚高推翻 |
| 归并 | `run_reality_merge`（S 匹配分保留 + reality prompt + 防膨胀守卫 + query_centroid 增量）；theme_ref 直连停用（v7 无直连） |
| graphify | `sync_realities_to_graph`（reality_{id} 节点 + merged_into 边）；cooc 边真 reality_id；graph.json 重建对齐 |
| L4/全量 | refinement.py 6 处 SQL + wiki_to_graph.py 全量子图：themes → realities |
| 测试 | 787 passed（recall mock 切 pick_injection_realities；wiki 子图 realities） |
