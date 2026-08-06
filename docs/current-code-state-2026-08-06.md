# CA 插件当前代码状态技术文档（供 Codex 修复使用）

> 生成时间：2026-08-06
> 生成依据：`~/.hermes/profiles/tester/plugins/ca_assembler/`（git worktree `ca_530cf20`，分支 **v6.0**，HEAD **7df9743**）
> 测试基线：**787 passed, 1 skipped, 1 xfailed**（`/usr/bin/python3 -m pytest tests/ -q`，34.19s；注意 pytest 9.0.3 无 xdist，`run_ca_tests.py` 的 `-n 4` 会失败，需直接跑 pytest 或去 `-n`）

---

## 1. 部署快照

| 项 | 值 |
|---|---|
| 版本 | v6.0（HEAD 7df9743） |
| 插件路径 | `~/.hermes/profiles/tester/plugins/ca_assembler/` |
| 核心引擎 | `ca/` 子目录（入口 `ca/__init__.py` → `ContextAssembler`） |
| 插件适配 | `__init__.py`（入口 `CAContextAssemblerPlugin` + 模块级 hook 函数） |
| Hook 注册 | 8 个：`on_session_start/end/reset` + `pre_llm_call` + `post_llm_call` + `post_api_request` + `pre_tool_call` + `post_tool_call` |
| CE 壳 | `CAContextEngine`（context engine "ca_assembler"），`compress()` 走方向 B：从 turn_stream DB 重建 conv_history |
| 同步 | 改代码后执行 `~/.hermes/profiles/scripts/sync-ca.sh winker` / `sync-ca.sh sysadmin` |
| 热加载 | gateway 进程不热加载插件代码 → 改后需重启 gateway 才生效；独立脚本立即生效 |

**两个 git worktree 的关系**：
- `~/projects/context-assembler/`（530cf20 detached）→ 源项目历史
- `~/.hermes/profiles/tester/plugins/ca_assembler/`（v6.0, 7df9743）→ 开发副本（.git 指向 context-assembler/.git/worktrees/ca_530cf20）
- 未提交变更：`.graphify_pylib/` 全部删除（上个会话的 graphify 清理，非本任务范围，勿动）

---

## 2. 架构与数据流（修复前必读）

### 2.1 分层

```
Hermes gateway hooks
  └─ __init__.py（模块级分发函数）
       ├─ CAContextAssemblerPlugin（每 session 实例，话题检测/摘要调度/注入）
       └─ CAContextEngine（CE 壳，compress 入口）
            └─ ca/ContextAssembler（EStage/FStage/LStage/AStage mixin 聚合）
                 ├─ store.py      — turn_stream SQLite + ca_topics.db（strand/theme/reality/共现）
                 ├─ topic_manager.py — TopicGradeManager（话题分割+定级）★插件根目录
                 ├─ topic_summary.py — 话题块 → strand 4B 摘要（含 call_llm_raw 通用调用）
                 ├─ theme.py      — strand → theme 归并（S 匹配分/余弦候选 + 4B 决策 + 兜底）
                 ├─ reality.py    — reality 生成/归并 prompt + 防膨胀守卫
                 ├─ inject.py     — 注入侧 4B 拣选（负向排除 → 4B top-K → 空注入）
                 ├─ flash_reprocess.py — flash 全链路重跑 pilot（云端 LLM）
                 ├─ refinement.py — L4 空闲精炼守护线程（默认停用）
                 ├─ cloud_llm.py  — deepseek-chat 调用（flash 用，key 从 .env 读）
                 ├─ graphify_sync.py — theme → graph.json 增量同步
                 ├─ e_stage.py / f_stage.py / a_stage.py / lstage.py
                 └─ tool_summarizer.py / embedding.py / config.py / ...
```

### 2.2 数据流（方向 B）

```
pre_llm_call（_on_pre_llm_call_v5）:
  ① 写 user Elm (turn, seq=0) 到 turn_stream
  ② 首轮：等 _cleanup_done（最长 120s）→ pick_injection_themes → <wiki_carryover> 注入
  ③ 话题检测：topic_mgr.detect() → 切换则 grade_on_switch() → 旧话题打包进 _pending_topic_summarize
  ④ FAR 切换：pick_injection_themes（4B 拣选）注入 recall_str（v7 决策 38，与首轮对齐）
  ⑤ 返回 recall_str（append 模式）或 None

compress（CE 壳）:
  _build_conv_history_v6 → 从 turn_stream 重建 conv_history
    尾部保护区（最后 2 个 user turn）全 Elm
    保护区外按话题等级：ACT→(user/fin=Elm, thought/tool=Fct)；REL→(Fct, Hdl)；FAR→(Hdl, 删除)

post_llm_call（_on_post_llm_call_v5）:
  写 assistant fin (turn, seq=N+1) → process_turn_f_stage → F-stage daemon 线程 LLM 摘要

post_api_request → thought 行 + tool 占位行；post_tool_call → tool 行回填 + per-tool Fct

话题摘要（_run_topic_summarize，后台线程）:
  话题块 → collect_turn_fcts → summarize_topic_chunk（4B）→ strand_summaries 写多条 strand
  → run_theme_merge（向量/S 候选 → 4B 决策 → 4B merge/create → themes 表）
  → record_block_cooccurrences + sync_cooccurrences_to_graph
```

### 2.3 核心表（ca_topics.db）

| 表 | 用途 | 关键列 |
|---|---|---|
| `strand_summaries` | 工作线摘要 | strand_id/session_id/topic_id/profile/hdl/turns/ooda_json/centroid_json/status |
| `themes` | 主题条目 | theme_id/title/overview/ooda_json/key_facts_json/open_items_json/timeline_json/centroid_json/source_strands/profile |
| `theme_strand_map` | strand→theme 归并 | (session_id, strand_id) PK + theme_id + method |
| `cooccurrence_events` | 共现边（S 模型图数据） | (session_id, topic_id, profile, reality_a, reality_b) UNIQUE |
| `wiki_associations` | theme↔图节点关联 | theme_id/graph_node_id（旧列 entry_id 已迁移） |
| `refinement_meta` | L4 精炼元数据 | refined_at/last_refined_turn/... |

per-session DB：`{hermes_home}/ca_cache/{session_id}.db`（turn_stream 表）
共享 DB：`{hermes_home}/ca_cache/ca_topics.db`

### 2.4 关键配置（ca/config.py，环境变量覆盖）

| 配置 | 默认 | 说明 |
|---|---|---|
| REFINEMENT_ENABLED | **False**（env "0"） | L4 精炼守护总开关 |
| REFINEMENT_INTERNAL_REFINE | **False**（env "0"） | 内部精炼（已被 theme merge 吸收） |
| REFINEMENT_CROSS_VALIDATE | True | Fct↔wiki 交叉验证 |
| REFINEMENT_HEALTH_SCORE | True | 健康评分 |
| REFINEMENT_GRAPHIFY_SYNC | True | graphify 同步 |
| TOPIC_SUMMARIZE_ENABLED | True | 话题摘要管线 |
| TOPIC_SUMMARY_MAX_TOKENS | 4096 | 摘要 num_predict（勿用 L1_MAX_TOKENS=2048） |
| TOPIC_SUMMARY_MAX_CHARS | 4000 | 摘要输出预算 |
| S_ALPHA / S_BETA / S_R | 0.4 / 0.8 / 0.5 | S 匹配分参数 |
| HERMES_PROFILE | CA_HERMES_PROFILE→HERMES_HOME basename→default | profile 隔离 |
| HISTORY_INJECTION | replace（CA_HISTORY_MUTATE=1） | 注入模式 |

⚠️ **Config.reload() 存在默认值翻转 bug（见 CR-1）**。

---

## 3. 测试命令

```bash
cd ~/.hermes/profiles/tester/plugins/ca_assembler
# 全量（推荐）
/usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider
# 收集检查（DOA 自防御）
/usr/bin/python3 -m pytest tests/ --collect-only -q | tail -3
# 单文件
/usr/bin/python3 -m pytest tests/unit/test_theme_merge.py -q --tb=short
```

**注意**：`run_ca_tests.py` 硬编码 `-n 4`（xdist），当前环境 pytest 9.0.3 无 xdist → 直接跑会报 `unrecognized arguments: -n 4`。基线用直接 pytest。

---

## 4. 已发现漏洞清单（修复任务）

> 每条含：严重度 / 位置 / 证据 / 影响 / 建议修复。证据均经代码阅读 + 运行验证。

### CR-1 🔴 高 — Config.reload() 默认值翻转：REFINEMENT_ENABLED/INTERNAL_REFINE 被意外启用

- **位置**：`ca/config.py` — 定义 L188/L195 用 `os.getenv("CA_REFINEMENT_ENABLED", "0")`（默认关），但 `reload()` L451/L455 用 `os.getenv("CA_REFINEMENT_ENABLED", "1")`（默认开）
- **证据**（实测）：
  ```
  类定义默认: REFINEMENT_ENABLED = False
  reload 后:   REFINEMENT_ENABLED = True   ← 翻转！
  类定义默认: REFINEMENT_INTERNAL_REFINE = False
  reload 后:   REFINEMENT_INTERNAL_REFINE = True   ← 翻转！
  ```
- **影响**：运行时任何 `Config.reload()` 调用会静默启用 L4 精炼守护线程 + internal refine（本来默认停用）。refinement 有 daemon 线程、LLM 调用、DB 写——意外启用造成额外开销与后台写库。
- **修复**：`reload()` 中 REFINEMENT_ENABLED / REFINEMENT_INTERNAL_REFINE 的 getenv 默认值改为 `"0"`，与类定义一致。

### CR-2 🔴 高 — refinement `_refine_single_entry` 环境变量污染（CA_L1_MAX_TOKENS 永久置 100）

- **位置**：`ca/refinement.py` L331-348（`_refine_single_entry`）
- **证据**（实测）：finally 块只恢复 `_C.L1_MAX_TOKENS = _saved`，**没有恢复 `os.environ["CA_L1_MAX_TOKENS"]`** → 函数返回后 env 仍为 "100"（实测确认），后续所有读取该 env 的代码（Config.reload、新进程、其它线程）拿到错误值。
- **影响**：进程级环境变量污染。精炼轮虽然默认停用，但一旦启用，会把 `CA_L1_MAX_TOKENS` 永久钉在 100，影响 F-stage 及其它读该 env 的链路。
- **修复**：用 `with patch.dict(os.environ, ...)` 或 try/finally 里同时恢复 env（`os.environ["CA_L1_MAX_TOKENS"] = 原值` / `del`）。更优：不要改 env，直接把 Config 属性改了用完后恢复即可（call_llm_for_summary 读的是 `Config.TOPIC_SUMMARY_MAX_TOKENS`，env 修改根本无效——见 CR-2b）。

### CR-2b 🟠 中 — refinement num_predict 覆盖无效（改错配置项）

- **位置**：`ca/refinement.py` L330-348
- **证据**：注释称「精炼输出很小（~80 tok），用独立 num_predict」，但 `_refine_single_entry` 调 `call_llm_for_summary(prompt)`，该函数固定用 `Config.TOPIC_SUMMARY_MAX_TOKENS`（4096，见 topic_summary.py L285），**与 L1_MAX_TOKENS 无关**。L331-336 的 env/属性覆盖对 num_predict 无任何效果（改的是 L1_MAX_TOKENS），是无效代码 + 环境污染。
- **修复**：删除 L330-336 的覆盖逻辑（或改为传入 num_predict 参数的正确方式）。

### CR-3 🔴 高 — refinement `_run_graphify_sync` 脚本路径错误（graphify sync 永不触发）

- **位置**：`ca/refinement.py` L724-738
- **证据**（实测）：`script = Path(__file__).resolve().parent / "scripts" / "wiki_to_graph.py"` → `__file__` 是 `ca/refinement.py`，parent 是 `ca/` → 计算路径 `ca/scripts/wiki_to_graph.py` **不存在**（实际在插件根 `scripts/wiki_to_graph.py`）。`script.exists()` 恒 False → 静默跳过。
- **影响**：Step 5 graphify 同步永不执行（静默失效）。
- **修复**：`Path(__file__).resolve().parent.parent / "scripts" / "wiki_to_graph.py"`（上两级到插件根）。

### CR-4 🟠 中 — `_run_health_score` 日志统计列错位（centroid_json 误用 key_facts_json）

- **位置**：`ca/refinement.py` L669-679（日志统计的 `_compute_health_score` 调用）
- **证据**：row 顺序 = `(theme_id, title, overview, changes_json, key_facts_json, centroid_json, source_strands, updated_at, created_at)`；L673 `centroid_json=row[4]` 取的是 **key_facts_json**，应为 `row[5]`。
- **影响**：仅日志统计的 flagged 百分比失真（实际 score 写入走主循环解包，正确）。不修则日志误导。
- **修复**：L673 改 `row[5]`。

### CR-5 🟠 中 — `__init__.py` 死代码引用已清理方法（engine._full_mutation = None → TypeError）

- **位置**：`__init__.py` L836-850（`_simple_mutation_mode_v5`/`_incremental_mutation` 委托）+ L942-950（`pre_llm_call_v5` 调用）
- **证据**：`ca/a_stage.py` L266 `_full_mutation = None`（方向 A 已清理）；`__init__.py` L840/L848 委托 `self._engine._simple_mutation_mode_v5(...)` → AttributeError；L942 `self._engine._full_mutation(...)` → TypeError。grep 确认 **无调用者**（hook 注册的是模块级 `_on_pre_llm_call_v5`，插件类方法 `pre_llm_call_v5` 未被任何地方调用）。
- **影响**：死代码 + 潜在崩溃点（一旦被调用即 TypeError）。属方向 A 清理残留。
- **修复**：删除 `pre_llm_call_v5` / `_simple_mutation_mode_v5` / `_incremental_mutation` 三个死方法及 `_full_mutation` 别名（或保留 `pre_llm_call_v5` 但改为调用方向 B 逻辑——需确认无外部引用后删除更干净）。

### CR-6 🟠 中 — 首轮 pre_llm_call 阻塞等待 120s（_cleanup_done 轮询）

- **位置**：`__init__.py` L1088-1094
- **证据**：`while not plugin._cleanup_done and time.time() < deadline: time.sleep(0.5)`，deadline = now+120s。`_cleanup_done` 在 `_run_session_start_cleanup`（后台线程）的 finally 中置位。
- **影响**：若后台清账慢（补缺摘要含 LLM 调用），首条用户消息的 pre_llm_call 被阻塞最长 120s——用户消息路径上的同步等待，与「后台清理移出用户消息路径」的设计期望相悖。
- **修复**：改为非阻塞（不等待，直接跳过 recall 或设置短超时如 5s；recall 可延后到下次切换）。

### CR-7 🟠 中 — `_run_zombie_cleanup` 空 centroid 计数虚高（fixed 无实际修改）

- **位置**：`ca/refinement.py` L574-578
- **证据**：`if not cent_json or cent_json == "null" or cent_json == "[]": fixed += 1` —— 空 centroid 只计数不修改（注释「通过健康评分另行处理」），但 `fixed` 计入 entries_modified → 每次循环空 centroid 的 theme 都被计为 modified → Step 5 graphify_sync 误触发（`entries_modified > 0` 条件）。
- **修复**：空 centroid 分支不计数（或改为实际重 embed）。

### CR-8 🟡 低 — `_check_single_source` 空 strand_ids → `IN ()` SQL 语法错误

- **位置**：`ca/refinement.py` L494-497
- **证据**：`placeholders = ",".join("?" for _ in strand_ids)`，strand_ids 为空时生成 `IN ()` → sqlite3.OperationalError（有 except 捕获，不崩但浪费一轮）。
- **修复**：空列表提前 return False。

### CR-9 🟡 低 — `_update_entry_refinement_meta` 的 COALESCE 子查询逻辑怪异

- **位置**：`ca/refinement.py` L412-417
- **证据**：`last_reviewed_turn=COALESCE(?, (SELECT MAX(last_refined_turn) FROM refinement_meta ORDER BY id DESC LIMIT 1))` —— COALESCE 第一个参数 `_compute_global_turn_max()` 总非空，子查询永不执行；且 MAX+ORDER BY+LIMIT 混用语义混乱。
- **修复**：简化为直接 `?`（传 _compute_global_turn_max()），删子查询。

### CR-10 🟡 低 — `upsert_wiki_entry` INSERT 路径缺 profile 列

- **位置**：`ca/store.py` L1196-1204
- **证据**：INSERT 不写 `profile` 列 → profile='' 的 theme 无法被 `WHERE profile=?` 查询到。当前调用点（refinement L374/L546）都传 entry_id（UPDATE 路径），INSERT 路径实际死代码。
- **修复**：INSERT 加 profile 参数（或注明死代码）。

### CR-11 🟡 低 — `parse_reality_detail` re.sub 误删正文 ``` 

- **位置**：`ca/flash_reprocess.py` L417
- **证据**：`re.sub(r"```(?:json)?", "", text)` 会删除所有 ``` 出现（包括正文中的），与 lenient_parse 同款问题。
- **修复**：只剥离首尾围栏（strip 而非 re.sub 全局）。

### CR-12 🟡 低 — `_compute_health_score` 分支顺序 bug（>90 天折扣永不生效）

- **位置**：`ca/refinement.py` L705-710
- **证据**（实测）：
  ```
  days=60  (应×0.8): 0.8 ✓
  days=120 (应×0.6): 0.8 ✗  ← elif days > 90 永不达（>90 必 >30）
  ```
- **修复**：顺序改为 `if days > 90: ×0.6 elif days > 30: ×0.8`。

### CR-13 🟡 低 — inject `_exclude_session` 硬编码 row[7]

- **位置**：`ca/inject.py` L112-129
- **证据**：`ss_raw = row[7]` 依赖 SELECT 列顺序（theme_id, title, overview, ooda_json, key_facts_json, open_items_json, centroid_json, source_strands → 7=source_strands 当前正确）。加列即错位。
- **修复**：用命名列或常量索引 + 注释。

### CR-14 🟡 低 — `query_themes_by_semantics` 先截断后过滤负分

- **位置**：`ca/store.py` L1409
- **证据**：`return [item for _, item in scored[:limit] if _ > 0]` —— 先 `[:limit]` 再过滤 `>0`，若 top-limit 内混入 cos≤0 条目则返回数 < limit。
- **修复**：先过滤再截断。

---

## 5. 修复纪律（Codex 必读）

1. **只修上述清单内的漏洞**，禁止顺手重构/改接口。最小改动精准补缺。
2. **TDD**：每个修复先确认/补测试（tests/ 下按模块分布：unit/test_theme_*.py, unit/test_reality_*.py, config/test_config.py, store/test_theme_store.py 等；refinement 无独立测试文件，可考虑新建或并入现有）。
3. **术语铁律**：Elm/Fct/Hdl（禁用 L0/L1/L2）；TopicGrade=ACT/REL/FAR，Grade=ELM/FCT/HDL，两枚举严禁混用。
4. **不动**：`.graphify_pylib/` 删除（工作区既有变更）、graphify-out/、docs/ 重建任务。
5. **不写 state.db**；不硬编码 `~/.hermes`（用 get_hermes_home()）。
6. **修复后验证**：
   ```bash
   /usr/bin/python3 -m pytest tests/ -q --tb=short -p no:cacheprovider
   ```
   基线 787 passed / 1 skipped / 1 xfailed，修复后不得少于基线（新增测试可增加总数）。
7. **多 profile 同步**：改完 → `~/hermes/profiles/scripts/sync-ca.sh winker` + `sync-ca.sh sysadmin` → gateway 需重启才生效（独立脚本立即生效）。
