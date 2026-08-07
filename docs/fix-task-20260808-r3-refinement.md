# CA 插件第三轮实现任务书（精炼轮 v2.5/v2.7 落地，Codex 执行，2026-08-08）

> 前置文档：`docs/wiki/decisions/34-idle-refinement.md`（**v2.7**）+ `docs/wiki/decisions/42-hindsight-borrowed-retrieval.md`（**v2**）+ `docs/wiki/architecture/14-idle-refinement.md` + `docs/wiki/architecture/11-l-stage.md`（本地已落盘，未提交——本轮任务基于这 4 份设计文档提炼）。
> 与第二轮任务书（`docs/fix-task-20260808.md`，BUG 修复）**独立**——本任务书为精炼轮 6 项**实现任务**（新功能落地），非缺陷修复。
> 验收标准：测试基线（**795 collected**，embed 服务正常时 **793 passed / 1 skipped / 1 xfailed**）+ 图一致性审计脚本 `/tmp/graph_consistency_audit.py`（僵尸/悬挂/theme 残留清零，tester 提供）。

## 0. 部署快照与当前代码状态（current-code-state）

- 插件路径：`/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/`（git worktree，branch `v6.0`，HEAD=`2e2dc71`「第二轮审计修复」）
- 核心 DB：`$HERMES_HOME/ca_cache/ca_topics.db`（WAL；`realities` 115 行 + `strand_to_reality` 574 条，决策 41 已迁移）
- 会话库：`~/.hermes/profiles/tester/state.db`（只读，禁止写）
- 主图：`graphify-out/graph.json`（3294 节点 / 6109 边）；wiki 子图 `graphify-out/wiki_subgraph.json`
- OV：`http://127.0.0.1:1933`（服务健康；`/api/v1/search/find` POST 端点 OpenAPI 确认存在，实测调用超时——见 §4）
- 测试命令：`/usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider`
- **测试基线实测（2026-08-08）**：**795 collected** = 791 passed / 2 failed / 1 skipped / 1 xfailed（791+2+1+1=795 自洽）。2 个失败均为 `tests/store/test_embedding.py::test_tc_e_001/002`——本机 `CA_EMBED_ENDPOINT=http://localhost:11439` 指向未启动服务（环境性失败，非代码回归）。**embed 服务正常时基线 = 793 passed / 1 skipped / 1 xfailed**（793+1+1=795，与用户给定基线一致）。验收时先起 embed 服务，或注明 2 failed 为环境性。注：第二轮审计报告写 794 collected 是当时收集数，当前实测 795。

### 0.1 图数据现状（2026-08-08 实测，`/tmp/audit_graph_now.py`）

| # | 项 | 实测值 | 对应任务 |
|---|----|--------|---------|
| ① | reality 节点 vs DB | 图 **116** 个 `reality_` 节点 vs DB **115** 行 → **僵尸 `reality_420`**（图有 DB 无） | T2 |
| ② | theme_ 残留 | **9** 个节点（theme_19/44/46/65/78/98/379/411/420）+ **17** 条 theme_ 相关边（14 merged_into + 3 cooc，均指向冻结表；14 条 merged_into 的 source 同为悬挂的 topic_ 节点） | T2 |
| ③ | 悬挂边 | **592/592** 条 merged_into 边 source（`topic_{sid}_S{strand_id}`）**无对应节点**——`sync_realities_to_graph` 只加边不建 topic 节点（592 = reality merged_into 578 + theme merged_into 14） | T1/T2 |
| ④ | 社区 | 知识节点 community 全 0（125 个 knowledge 节点，graphify 聚类未作用于知识层） | T2（重建后 `graphify cluster-only`） |
| ⑤ | cooc 边 | 6 条 `co_occurs_with`：**3 条 reality 真边**（reality_19→65、reality_44→78、reality_46→420——末条连**僵尸** reality_420，T2 删节点时连带删边）+ **3 条 theme 残留**（theme_19→65、theme_44→78、theme_46→420） | T3/T4 数据源 |
| ⑥ | 主图知识层来源 | 116 reality 节点**全部来自增量 sync**（`sync_realities_to_graph`）；`wiki_subgraph.json` 是 theme 时代旧版（915 节点 = 410 theme + 503 strand-topic + 2 ov_doc + **0 reality**，503 个 strand-topic 全不在主图）——`build_wiki_subgraph` reality 版**从未成功产出/合并**，T2 实现时须先验证该链路 | T2 |

### 0.2 相关代码模块现状

| 模块 | 现状 | 本轮任务 |
|------|------|---------|
| `ca/graphify_sync.py:183` `sync_realities_to_graph` | 加 reality 节点 + merged_into 边；**topic source 节点不建**（229-239 只写边引用） | T1 |
| `scripts/wiki_to_graph.py:449` `merge_into_main_graph` | 只增不删（465-480），无一致性清理 | T2 |
| `scripts/wiki_to_graph.py:279` `build_wiki_subgraph` | 已读 realities；会建 topic 节点（420-432）+ trace 边（373-410）——全量重建骨架可用；⚠️ **reality 版从未成功产出/合并**（磁盘 wiki_subgraph.json 为 theme 时代旧版，见 0.1 ⑥） | T2 复用 |
| `ca/refinement.py` `IdleRefinementDaemon` | **Step 编号注意：代码编号 ≠ 设计编号**。代码 `_run_refinement_cycle`（151-）：Step 1 内精炼（默认关）/Step 2 **交叉验证**（=`_run_cross_validate`，设计文档 **Step 5**）/Step 3 僵尸清理/Step 4 健康评分/Step 5 graphify sync 已实现；**归并审查（设计 Step 2）、详情重生成（设计 Step 3）、设计 Step 8 全量重建、设计 Step 8.5 建边未实现** | T3/T5/T6 |
| `ca/refinement.py:817` `_CROSS_VALIDATE_PROMPT` | 仍是 theme 时代 entry 语义（entry_title/entry_facts）；candidates 已读 realities 表但键名残留 entry_id/title（247-296） | T6 |
| `ca/inject.py:345` `pick_injection_realities` | 提问云形心主路径（θ_max=0.5 → top-15 → 4B top-3）；**无任何图读取** | T4 |
| `ca/reality.py:550` `append_timeline_hdl` | append 纯字符串 hdl（防重：空/末条相同不追加）；**生产代码无调用点**（仅 exp_reality_winker.py 实验脚本调用） | T5 |
| `ca/store.py:971` `update_reality` | timeline_entry `{seq?, topic_id, turns, session_id, overview}`，seq 缺省自动 +1（1005-1006）；**无 ts** | T5 |
| `ca/reality.py:921/972` merge/create timeline_entry 构造 | `{topic_id, turns, session_id, overview}`——**overview 实测 8/8 空值**（F-4 数据缺陷，v2.6 归 Step 4 修复，非本轮范围） | T5 只加 ts |

## 1. 修复纪律（硬性）

1. **最小改动**：只实现本任务书列出的 6 项，禁止重构、禁止格式化无关代码。
2. **禁止改测试代码**：测试用例是基线，不许修改、删除、跳过。**新增测试可以**（每项任务附对应新增测试）。
3. **术语**：Elm/Fct/Hdl（禁用 L0/L1/L2 新出现）；环境变量名（CA_*）保留原名。
4. **硬约束**：不写 state.db；路径用 `get_hermes_home()` 或 `Path(__file__)` 相对，禁止硬编码 `/home/i1j`；SQL 用参数化；graph.json 读写加锁/容错（文件不存在/解析失败 → 跳过对应功能，不崩溃）。
5. **设计约束（34 v2.7，违反即返工）**：
   - 信号 A bigram **暂缓**：不得按「共享 ≥2 非泛词」建 shares_topic 边（完整泛词表重测仅 14 对、≥3 仅 1 对假象）——本轮不实现信号 A。
   - references_ov 相似度阈值**无先验**：先小样本验证（10 reality 检索看命中合理性）再定，不拍脑袋。
   - 关联层（Step 8.5）**不上 L3**：L1 代码/API + L2 4B 足够。
   - R-1 图路只做**候选扩展**（recall augmentation），排序仍由提问云形心 + 4B 决定；深度 1、上限 +5、无图时行为与现状一致。
   - R-2 生长序：timeline 追加顺序 = strand 归并顺序（代码维护，勿信 LLM）。
6. **改动范围核对**：完成后 `git status --short` + 列出每个改动文件 diff 摘要。
7. **完成输出格式**：逐条 `[T-ID] 实现摘要 + 改动文件:行 + 验证方式`；未实现项说明原因。

## 2. 实现清单（6 项，按依赖序：T1→T2→T3→T4→T5→T6）

### T1 [任务①] sync_realities_to_graph 补建 topic 节点（P0，悬挂边根治）

- **文件**: `ca/graphify_sync.py:183-263`（`sync_realities_to_graph`）
- **证据**: 实测 592/592 merged_into 边 source（`topic_{sid}_S{strand_id}`）在 graph.json 中无节点——增量 sync 只加边不建 topic 节点（229-239 仅构造边引用）。
- **建议修复**: 在构造 merged_into 边时，对每个 source 补建 topic 节点（对齐 `build_wiki_subgraph` 420-432 的节点格式：`label: "Strand {sid}/S{strand_id}"`、`file_type: knowledge`、`_origin: wiki`、`community: 0`、`source_file: strand_summaries/{sid}/S{strand_id}`）；幂等（existing_ids 去重逻辑复用 244-260）。
- **验证**: 构造含 source_strands 的 reality → `sync_realities_to_graph` → graph.json 中 topic 节点存在、悬挂边为 0。**新增测试** `tests/store/test_wiki_graph_themes.py` 或新文件：断言 sync 后 strand-topic 节点数 = source_strands 引用的 strand 数。⚠️ **计数必须按 strand 格式过滤**（正则 `^topic_[^_]+_S\d+$`）——主图另有 42 个 `topic_manager_*` 等**代码 AST 节点**（graphify 从代码文件提取，file_type=code），不可计入或误删（见 T2 ⑥）。

### T2 [任务②] merge_into_main_graph 一致性清理（P0，僵尸/残留/悬挂清零）

- **文件**: `scripts/wiki_to_graph.py:449-489`（`merge_into_main_graph`）+ `build_wiki_subgraph`（279-）
- **证据**: 实测僵尸 `reality_420`（图 116 vs DB 115）、9 个 `theme_` 残留节点 + 17 条 theme_ 相关边（14 merged_into + 3 cooc）、592 悬挂边。merge 当前只增不删（465-480）。⚠️ 另见 0.1 ⑥：`build_wiki_subgraph` reality 版从未成功产出/合并（磁盘 wiki_subgraph.json 是 theme 时代旧版）。
- **建议修复**（全量重建语义，realities 表为唯一权威）：
  0. **先验证 `build_wiki_subgraph` reality 版能产出**（直接运行 `python3 scripts/wiki_to_graph.py`，确认 wiki_subgraph.json 重建为 reality 节点版；当前磁盘文件是 theme 时代旧版——410 theme/503 topic/0 reality，覆盖属预期）
  1. merge 前对主图 **knowledge 域**（`file_type=="knowledge"` 的节点：`reality_*`/`theme_*`/strand-topic/`ov_doc_*`）做一致性清理。**⚠️ 清理范围必须限定 knowledge 节点——主图另有 42 个 `topic_manager_*` 代码 AST 节点（file_type=code，来自 graphify 代码提取），前缀 `topic_` 会误匹配，严禁删代码节点**：
     - `reality_{id}` 节点：id 不在 DB realities 表 → 删除（含其所有入/出边；注意 reality_46→420 cooc 边随 reality_420 删除）
     - `theme_*` 节点：**全删**（themes 冻结，设计 v2.3 清残留，含 17 条 theme_ 相关边）
     - strand-topic 节点（正则 `^topic_[^_]+_S\d+$` 匹配）：以 DB 全量 reality.source_strands 反推引用集合为准，不在集合内的删除（注意：先建后删或按 subgraph 全量替换，避免顺序问题；代码 AST 节点如 `topic_manager_*` 不在正则内，天然豁免）
     - 悬挂边（source 或 target 已删除/不存在于图）删除
  2. 然后 merge subgraph（复用 465-480 逻辑）
  3. 重建后跑社区发现：`graphify cluster-only <插件目录> --no-viz --no-label`（CLI 已确认存在；`--no-label` 跳过 LLM 命名防网络依赖）——供下一轮 Step 2e 归并候选源
- **验证**: `/tmp/graph_consistency_audit.py`（tester 提供）输出：僵尸=0、theme 残留=0、悬挂边=0、知识节点数与 DB 一致；`graphify cluster-only` 后知识节点 community 非全 0；**代码 AST 节点（topic_manager_* 等 42 个 + 其边）完好**。**新增测试**：构造含僵尸 reality_/theme_ 残留/悬挂边/代码节点的 graph.json fixture → merge 后知识域清零且代码节点保留。

### T3 [任务③] Step 8.5 事实关联建边：信号 B（承接判定 L2）+ 信号 C（references_ov L1）（P1；信号 A 暂缓）

- **文件**: `ca/refinement.py`（新增 Step 8.5 方法，如 `_run_fact_linking`，接入 `_run_refinement_cycle`）+ 必要时新增 `ca/fact_linking.py` 模块
- **设计依据**: 34 v2.4/v2.6/v2.7 Step 8.5 节 + 修订记录。四类边：`shares_topic`（弱）/`depends_on`（强）/`continues`（强）/`references_ov`。**本轮只做 B + C**。
- **信号 B（L2 4B 判定，承接/延续）**：
  - 候选：reality 内容（name/hdl/current_status）含承接动词「基于/承接/详见/延续/参照」（实测 11 条）→ 与该 reality 有文本关联的另一 reality 成对
  - 4B 判定（复用 `_pick_by_4b`/`call_llm` 模式；**解析失败重试一次，仍败跳过该候选并记日志**——对齐 34 v2.2 升级原则的 L2 部分；信号 B 不上 L3，无「升 L3」路径）：输出 `depends_on` / `continues` / 无关联
  - 落图：`graph.json` 边 relation=`depends_on`/`continues`，source/target 均为 `reality_{id}`，`_origin: fact_linking`
  - 强边用途：合并影响分析（邻居查询）+ R-1 图路（T4 消费）
- **信号 C（L1 OV 语义检索，references_ov）**：
  - 对每个 reality：name/hdl 作 query → `POST http://127.0.0.1:1933/api/v1/search/find`（OpenAPI 确认存在，FindRequest schema；**实测调用超时——实现时先用 1 个 reality 冒烟验证端点可用性，超时则加长 timeout/降级跳过并报告**）
  - top-1 命中且相似度 ≥ 阈值 → `references_ov` 边（target 节点 `ov_{resource_id}` 或复用 build_wiki_subgraph 的 `ov_doc_*` 命名——**实现时对齐现有命名**）
  - **阈值无先验**：先跑 10 reality 小样本，人工看 top-1 命中合理性，记录命中分布后再定阈值（写入日志/注释，勿写死未验证值）；若命中全部不合理 → 本项标记「待阈值验证」不落图
  - 替代 theme 时代 `wiki_associations` 关键词子串匹配（134K 条全 related，粗糙冻结）
- **约束**: 关联层不上 L3；每轮新增边数记 refinement_meta（`associations_added`，schema 已有该字段）
- **验证**: 构造含「基于/承接」动词的 reality 对 + mock 4B → depends_on/continues 边落图；mock OV 检索返回命中 → references_ov 边落图；10 reality 真实 OV 检索冒烟记录命中分布（tester 复核）。**新增测试**：`tests/unit/test_fact_linking.py`（信号 B 判定 + 信号 C 落图 + 无候选/API 失败降级不崩）。

### T4 [任务④] R-1 图路：pick_injection_realities 图邻居候选扩展（P1）

- **文件**: `ca/inject.py:345-455`（`pick_injection_realities`）+ 新增图读取辅助函数
- **设计依据**: 42 v2 R-1 节（落点修正：retrieval.py 不修改，reality 级检索只有注入拣选）。
- **建议实现**（在主路径 ③ top-15 截断之后、④ 4B 拣选之前插入）：
  ```
  ③ budget = in_range[:QUERY_CLOUD_TOP_K]  (432 行)
  → ③.5（新增）图路扩展：
      - 读 graphify-out/graph.json（容错：不存在/解析失败 → 跳过图路）
      - 对 budget 中每个 reality 的图邻居（reality 间边：co_occurs_with / shares_topic / depends_on / continues）
      - 深度 1，去重（不在 budget 的邻居），上限 +5（超过截断）
      - 邻居补入 budget（index 重新编号）
  → ④ _pick_by_4b(query, budget, limit)
  ```
  - 约束：只扩展候选池，排序/拣选逻辑不动（决策 38 行为信号优先不被破坏）；无图/无边时行为与现状完全一致（回归测试保证）
  - 图读取辅助函数建议放 `ca/inject.py` 内（`_load_graph_neighbors(graph_path, budget_ids) -> list[dict]`，复用 `_to_candidate` 结构或最小 dict：reality_id/name/hdl）
- **验证**: 构造含 2 个 connected reality 的测试图（co_occurs_with 边）→ 查询命中其一 → 候选池含邻居 reality（单测）；无 graph.json / 无边 → 行为与现状一致（回归）。**新增测试**：`tests/unit/test_inject_graph_route.py`。

### T5 [任务⑤] R-2 生长序 + R-4 timeline 结构化（P1）

- **文件**: `ca/reality.py:550-564`（`append_timeline_hdl`）+ `ca/store.py:971-1046`（`update_reality`）+ `ca/reality.py:921-926/972-977`（merge/create timeline_entry 构造）
- **设计依据**: 42 v2 R-2/R-4 节（v2 定稿：R-2 生长序建构约束，非检索信号；R-4 timeline 结构化支撑生长序）。
- **R-4 实现**：
  1. `append_timeline_hdl`：追加改为 `{"hdl": h, "ts": time.time()}` 结构化条目；**保留防重逻辑**（空 hdl 不追加；与末条相同不追加——注意末条可能是 dict 或 str，比较需兼容 `str(timeline[-1]).strip()` vs `h`，dict 时取 `timeline[-1].get("hdl")`）
  2. `update_reality`（store.py:1003-1007）：timeline_entry 处理时补 `entry["ts"] = entry.get("ts") or time.time()`（seq 逻辑不动）
  3. 存量兼容：读取 timeline 时 `str` 条目视为 hdl（ts 缺省用 reality.updated_at）——至少保证消费方（注入渲染、merge 恢复、refinement 读取）不崩
  4. 调用点核对：`append_timeline_hdl` **生产无调用点**（仅 exp_reality_winker.py 实验脚本）——R-4 的实际生产落点是 merge/create 的 timeline_entry 构造（921/972）+ update_reality。若 merge 链路应使用 append_timeline_hdl 统一 hdl 追加，实现时对齐（勿重复追加）
- **R-2 生长序实现**（本期范围）：
  - 约束 1：merge 时 group 内 strand 按 `turns` 升序处理（`run_reality_merge` 对 group 排序后再构造 timeline_entry/调用 4B；turns 是轮次列表，**排序键 = `min(turns)`**（起始轮次，实测 R3 S2[t1,2]→S5[t5,7]→S6[t7]）——timeline 追加顺序 = strand 归并顺序
  - 约束 2（Step 3 详情重生成按 turns 升序）：**Step 3 详情重生成当前未实现**（refinement.py 无详情重生成）——本期标注约束 2 的落点待 Step 3 实现时生效，不新增半成品
- **验证**: `append_timeline_hdl([], "h")` → `[{"hdl":"h","ts":...}]`（单测）；存量 `["旧hdl"]` + append → dict 追加且防重生效（回归）；`update_reality(timeline_entry=...)` → timeline 含 ts（单测）；merge group 乱序 strand → 输出按 turns 升序（单测）。**新增测试**：`tests/unit/test_reality_prompts.py` 扩展或新文件。

### T6 [任务⑥] 设计 Step 5 prompt Reality 化（P2）——交叉验证（= 代码 `_run_cross_validate`，`_run_refinement_cycle` 内注释「Step 2」）

- **文件**: `ca/refinement.py:817-851`（`_CROSS_VALIDATE_PROMPT`）+ 534-538（`.format` 调用点）+ 247-296（`_load_refinement_candidates` 键名）
- **证据**: `_CROSS_VALIDATE_PROMPT` 仍是 theme 时代 entry 语义（「知识条目（wiki entry）的标题和已有 key_facts」+ `{entry_title}`/`{entry_facts}` 占位符）；candidates 已读 realities 表但 dict 键为 `entry_id`/`title`（286-287），`.format` 传 `entry["title"]`/`entry["key_facts"]`（535-537）。
- **建议修复**:
  1. `_CROSS_VALIDATE_PROMPT` Reality 化：A) 输入改为「reality 的 name/hdl/current_status（key_facts/goals/current_state）」；B) 输出 `corrected_facts/corrected_changes/corrected_open_items` 与 reality current_status 四段（current_state/key_facts/goals/context）对齐
  2. `_load_refinement_candidates` 返回键补 `hdl`/`current_status`（realities 表 259 行 SELECT 已有 name/hdl/current_status——核对 286-287 只取了 name）
  3. `.format` 调用点改用 `{reality_name}`/`{reality_hdl}`/`{reality_cs}` 等新占位符
- **验证**: 构造 reality 候选 → `_CROSS_VALIDATE_PROMPT.format(...)` 输出含 name/hdl/current_status（单测）；现有交叉验证测试（tests 中 cross_validate 相关）通过。**新增测试**：prompt 内容断言（不含 entry_title/entry_facts 字样，含 reality 字段）。

## 3. 完成检查（Codex 自验 + tester 复核）

1. 语法检查：`python3 -c "import ast; [ast.parse(open(f).read()) for f in <改动文件>]"`
2. 全量测试：`/usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider`
   - 基线 795 collected；**embed 服务未启动时允许 test_embedding 2 个失败（环境性，791+2+1+1=795）**；embed 服务正常时应 793 passed / 1 skipped / 1 xfailed（793+1+1=795）
3. **图一致性验收**：`python3 /tmp/graph_consistency_audit.py`（tester 提供，断言僵尸=0 / theme 残留=0 / 悬挂边=0）
4. 逐项核对（tester 独立复核，不信 Codex 自报）：
   - T1：grep `sync_realities_to_graph` 的 topic 节点补建分支在位；graph.json 悬挂边计数归零
   - T2：grep merge 清理逻辑；跑一次 `python3 scripts/wiki_to_graph.py` 全链路后 graph.json 知识节点数 = DB realities 行数（+strand-topic），**代码 AST 节点（topic_manager_* 等 42 个）完好**；`graphify cluster-only` 后 community 非全 0
   - T3：信号 B/C 函数存在且接入 `_run_refinement_cycle`；OV 检索冒烟记录
   - T4：图路扩展代码在位且包在容错分支；无图时回归通过
   - T5：`append_timeline_hdl` 输出 {hdl,ts}；`update_reality` ts 在位；存量 str 兼容
   - T6：prompt 无 entry_ 字样；格式化调用键一致
5. `git status --short` 列出改动；确认只改任务书内文件（docs/wiki 的 34/42/14/11/INDEX 已有未提交修改属预期，勿动）
6. 输出每 ID 实现摘要 + 验证证据。

## 4. UNCERTAIN / 实现时需验证项（tester 已探明的环境事实）

| # | 项 | 探明结果 | 实现时动作 |
|---|----|---------|-----------|
| U-1 | OV `/api/v1/search/find` | OpenAPI 确认存在（POST，FindRequest）；实测调用 **8s 超时**（viking_search MCP 亦 Internal error）——检索后端当前慢/不稳 | 信号 C 先 1 reality 冒烟；超时加 timeout/降级跳过；完成后 tester 复核命中合理性 |
| U-2 | graphify 社区发现 | CLI `graphify cluster-only <dir> --no-viz --no-label` 已确认（重跑聚类写 community） | T2 重建后跑；`--no-label` 防 LLM 网络依赖 |
| U-3 | Step 3 详情重生成 | refinement.py **未实现**（归并审查/详情重生成均缺） | R-2 约束 2 本期仅标注落点；完整 Step 2/3 属后续轮（Phase 2），本任务书不新增 |
| U-4 | embed 服务 | `CA_EMBED_ENDPOINT=localhost:11439` 未监听（shell env 残留）；config.py 默认 11435 亦无监听 | 验收时确认 embed 服务状态再定基线口径：795 collected（embed 正常 793+1+1；未启动 791+2+1+1） |
| U-5 | append_timeline_hdl 生产调用点 | **生产零调用**（仅实验脚本 exp_reality_winker.py） | R-4 主落点 = merge/create timeline_entry + update_reality；append_timeline_hdl 作为统一工具函数改造 + 测试 |
| U-6 | `ov_doc_*` 节点命名 | build_wiki_subgraph 用 `ov_doc_{label}`（wiki_to_graph.py:118） | 信号 C references_ov 边 target 命名对齐此约定，勿发明新命名 |

## 5. 本轮不做（边界声明，防范围蔓延）

- 信号 A bigram shares_topic 建边（34 v2.7 暂缓：阈值待验证，提 ≥3 或降级候选线索不落图）
- 归并审查（Step 2 五源候选 + 承接判定执行 s2r 重映射）与详情重生成（Step 3）——Phase 2 后续轮
- 历史共现回填（39 块跨 reality ≥2 漏记，可选）——T3/T4 依赖现有 6 条 cooc 边即可
- F-4 timeline overview 空值根因修复（v2.6 归 Step 4 内精炼，内精炼默认关）
- 主题/术语清理、settings.yaml 死配置（第二轮任务书范围，不重复）
