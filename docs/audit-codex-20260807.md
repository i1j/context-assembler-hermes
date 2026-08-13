# CA 插件 设计文档 vs 代码 审计报告（Codex 执行 + tester 独立复核）

- 审计时间: 2026-08-07
- 代码版本: `4d5572b docs: 本地技术文档 reality 化（决策 41 同步）`（worktree ca_530cf20）
- 审计范围: `docs/wiki/`（14 architecture + 31 decisions）+ `__init__.py`(1396 行) + `topic_manager.py`(649 行) + `ca/` 26 模块（14207 行）
- 测试基线: 789 collected
- 验证方式: Codex 静态对照 + 2 个最小化运行时复现；tester 对全部发现逐项独立复核（grep/读代码/实测），**17 偏离 + 9 bug 全部属实，无误报**

## 严重度分布

| 严重度 | 数量 | 项目 |
|---|---|---|
| 🔴 高 | 2 | BUG-01/D2（reality 恒 create）、D1（tail-protection 设计未实装） |
| 🟠 中 | 10 | D3/D4/D5/D6/D7/D8/D9/D10 + BUG-02/03/04/08/09 |
| 🟡 低 | 10 | D11~D17 + BUG-05/06/07 + 文档内部问题 |

---

## 一、高危（2 项，功能级失效）

### 🔴 BUG-01 + D2: reality 归并恒 create（v7 核心功能失效）

- **位置**: `ca/reality.py:803-821,841` + `ca/theme.py:418-419,433,447`
- **现象**: `run_reality_merge`（v7 生产归并主流程）**任何情况下都不产生归并候选**，全部 strand 走 `new` 分支新建 reality。
- **证据**（实测复现）:
  - `find_s_candidates` 只读 `theme_id` 键：`anchors = [t.get("theme_id") ...]`（theme.py:418-419）
  - 但运行时传入的锚与 reality 均为 `reality_id` 键 dict（`load_all_realities` store.py:947-951、`pick_injection_realities` inject.py:469-471）
  - 最小复现：`find_s_candidates([{"reality_id":1}], [...], {(1,2):3})` → `[]`；改用 `theme_id` 键 → 正常返回 S=0/0.25 候选
  - 冷启动路径（reality.py:818-821）传空锚 → 恒 `[]`（注释声称的"余弦退化"实际不存在）
  - 合并消费点 `cand.get("reality_id")`（reality.py:841）同样恒 None
- **根因**: 决策 38 的 `find_s_candidates` 是 theme 类型（`theme_id`），决策 41 接入 reality 时未做键适配直接复用。
- **影响**: reality 层运行时零归并、全部 create new → reality 碎片化（正是决策 37 P1 要解决的问题）；决策 41 §4.3 规划的 `test_reality_merge.py`（M-1~M-5）**未创建**（已确认 `tests/unit/test_reality_merge.py` 不存在），测试面为空。

### 🔴 D1: tail-protection 实现与设计不符（配置项失效）

- **位置**: `ca/a_stage.py:102-110` vs `docs/wiki/decisions/13-tail-protection.md` + `architecture/05-a-stage.md`
- **设计声称**: 保护最后 `CA_PROTECT_TAIL_TOKENS`（默认 20000）token 对应的 user 轮，从 turn_stream 末端向前扫描
- **代码实际**: 硬编码保护**最后 2 个 user turn**（`tail_set = set(user_turns[-2:])`）；`Config.PROTECT_TAIL_TOKENS`（config.py:123-126）**全库无消费点**（仅定义与 reload）
- **影响**: 长对话尾部保护范围与设计不符；配置项静默失效

---

## 二、中危（10 项）

| ID | 位置 | 现象 | 验证 |
|---|---|---|---|
| **BUG-02** | `__init__.py:1042-1043` | 话题切换时 `embed_client.embed(user_message)` 与 `grade_on_switch` 无 try 保护；`_compute_centroids` 的 `except (ValueError, TypeError, RuntimeError)` 覆盖不到 `ConnectionError`/`TimeoutError`/urllib3 `HTTPError`（embedding.py:142 明确 `raise`）→ 嵌入服务宕机瞬间用户轮 hook 抛未捕获异常 | ✅ 代码确认 |
| **BUG-03 / D6** | `__init__.py:527-536` | 新会话 turn 1 "上次会话末话题补缺摘要"读错 store：`old_store` 仅取 `max_turn` 后 `close()`，`_run_topic_summarize` 内部 `collect_turn_fcts(engine.store, ...)`（L654）用的是**当前会话** DB → 查询 `last_sid` 恒空 → 空摘要兜底 | ✅ 代码确认 |
| **BUG-04 / D3** | `ca/inject.py:426-427` | 提问云形心范围预筛 `in_range = [... d <= THETA_MAX]` 空集时回退 `budget = (in_range or scored)[:15]` → θ_max=0.5 安全网被绕过，远离所有 reality 的提问仍构造 4B 候选（决策 41 §2.4b ④ 要求"空注入宁缺勿错"） | ✅ 代码确认 |
| **BUG-08** | `__init__.py:1011-1014` + inject 4B | 用户消息路径同步阻塞：turn==1 `while + time.sleep(0.5)` 等清账 5s；`pick_injection_realities` 内同步 4B 调用超时 `LLM_TIMEOUT=120s` × `LLM_MAX_RETRIES=2` + 退避 → 首轮/FAR 切换用户消息可被阻塞分钟级 | ✅ 代码确认；**根治 (2026-08-08 v7.1)**: ① `Config.REALITY_INJECT_ENABLED` 总闸（`CA_REALITY_INJECT_ENABLED`）；② 代理优先级头 `X-Queue-Priority`：注入 4B + F-stage=high、话题摘要=normal、精炼轮=low（`call_llm_raw`/`call_llm_for_summary` 加 `priority` 参数）；③ **切换慢根因**：`_compute_centroids` 全量重算改为读 `AssemblyCache.semantic_fct_embeddings`（F-stage 后台补算语义文本 embedding，切换路径 0 embed）；④ 测试隔离 `REALITY_INJECT_ENABLED`（autouse fixture）。839 passed。恢复开关：env `CA_REALITY_INJECT_ENABLED=1` |
| **BUG-09** | `ca/store.py:169-191` | `write_turn_v5` 用 `INSERT OR REPLACE` 与 02-store"行不可变—写入即不可撤销"矛盾；`pre_llm_call` 以 `conversation_history` 的 user 数重算 turn（`__init__.py:978`）→ 会话恢复/重复触发时旧轮 Elm/Fct 可能被静默覆盖（触发条件依赖 Hermes 是否传全量历史，见 U1） | ✅ 代码确认 |
| **D4** | `ca/reality.py:909,961` + `ca/store.py:438-451` | 决策 39/41 要求运行时随归并增量维护提问云（summarize 快照 query_text），但 strand dict 来自 4B 输出无 `query_text` 键 → `_update_query_centroid` 恒空文本恒跳过；只有离线迁移脚本写 query_centroid → 运行时新建 reality 无提问云 → `inject.py:419-420` 排除 → **新 reality 永远无法被注入召回（冷启动死锁）** | ✅ 代码确认 |
| **D5** | `ca/store.py:1368-1458` + `__init__.py:170-191` | 决策 22（state.db 去重）未实装：无 `on_session_finalize` 注册；`install_state_dedup_trigger` 是死代码（无调用方）；若接线会对 state.db 执行 DELETE+CREATE TRIGGER，违反 AGENTS.md「不写 state.db」 | ✅ 代码确认 |
| **D7** | `ca/reality.py:805,819` | 决策 38 §五 锚集 A = I ∪ F（F 用 beta=0.8），但 `fused_ids=set()` 恒空 → beta 分支永不触发，块内延续场景 S 排序权重失真 | ✅ 代码确认 |
| **D8 / BUG-07** | `ca/retrieval.py:73-75` + `ca/cache.py:113-117` | `Retriever.__init__` 引用已删除的 `BM25Snapshot.tool_bm25/tool_turn_keys/tool_fct_embeddings` 属性 → **构造即 AttributeError（实测复现）**；全库无 `Retriever(` 调用方 → 方向 B 后检索模块为死代码且不可用，文档仍列为活动组件 | ✅ 实测复现 |
| **D9** | `ca/config.py:79-101` + `ca/embedding.py:23-26,38-42` | 配置键名错位：文档 `CA_EMBEDDING_BACKEND`/`CA_EMBEDDING_MODEL`/`CA_SUMMARY_MODEL` 不存在（实际 `CA_EMBED_BACKEND`/`CA_EMBED_MODEL`/`CA_LLM_MODEL`）；`EmbeddingClient` 直接 getenv 绕过 Config：`CA_EMBED_MAX_RETRIES` 默认 0 vs Config 2、`CA_EMBED_BATCH_TIMEOUT` vs `CA_EMBED_BATCH_PARALLEL_TIMEOUT` 键名错位、默认模型名不一致；settings.yaml 多数键无消费点 | ✅ 代码确认 |
| **D10** | `docs/wiki/architecture/10-embedding.md:27` vs `ca/embedding.py:24` | 文档声称"Hdl+Fct 均为 4096 维 BLOB"，实际 `_EMBED_DIM=1024`、`centroid_json TEXT` | ✅ 代码确认 |

---

## 三、低危（10 项）

| ID | 位置 | 现象 |
|---|---|---|
| **BUG-05** | `__init__.py:1022-1024,1077-1079` | reality 注入成功日志用 `e.get('theme_id')`/`e.get('title')` 读 reality dict（键为 reality_id/name）→ 恒输出 `id=None()`，日志失真 |
| **BUG-06** | `__init__.py:905-906` | `_pending_topic_summarize.clear()` 之后才 `len(...)` 打日志 → 计数恒 0 |
| **D11** | `docs/wiki/architecture/04-f-stage.md:32-34` vs `ca/f_stage.py` | 文档声称降级检测条件 `startswith('核心摘要：无有效增量')` + 保持上次 Fct/Hdl + 60s 超时；代码无该检测串（用 `MEANINGLESS_CORE` 清洗后仍覆写为"本轮无新内容"），超时实为 `LLM_TIMEOUT=120` |
| **D12** | `docs/wiki/architecture/08-cache.md` vs `ca/cache.py:30-38` | 文档六字典缓存表，实际仅 4 个对话轮字典（工具轮缓存已移除） |
| **D13** | `docs/wiki/decisions/34` vs `ca/config.py:188` | 文档/arch14 说 `REFINEMENT_ENABLED` 默认 True，实际默认 **False**（决策 36 停用后未回写 arch/34） |
| **D14** | `docs/wiki/architecture/11/14` vs `ca/refinement.py:48-93` | 文档标 L4 实现在 LStageMixin（lstage.py），实际在 `IdleRefinementDaemon`（plugin 级） |
| **D15** | `docs/wiki/decisions/20` vs `__init__.py:330-358` | 决策 20 声称 compress() 退化为仅 FAR 行删除，实际全量 `_build_conv_history_v6` 重建（方向 B 已替换，决策描述过期） |
| **D16** | `docs/wiki/architecture/03-e-stage.md` vs `ca/e_stage.py:77-79` | 文档声称 post_api_request 恒写 thought 行，实际纯对话轮（无 tool_calls）直接 return 不写 |
| **D17** | `docs/wiki/architecture/12-config.md` vs `ca/config.py:125` | 文档 `CA_PROTECT_TAIL_TOKENS` 内建默认 20000，实际无 settings.yaml 时兜底 **10000** |
| **文档内部** | 多处 | `decisions/28` 术语违规（L0/L1 小标题）+ `ca/reality.py:19` 注释残留 `OV Abstract (L0)`；`decisions/28` 阈值 `TOPIC_JACCARD_ENTRY=0.02` vs config `0.04`；`ca/f_stage.py:6` 引用不存在的 `architecture/15-multi-ooda-arch.md`；`architecture/13` 声称 `tests/integration/` 与 416 测试（实际无 integration 目录、787 测试）；`architecture/06` 仍描述已删除的 `_fire_ov_submit`；`40-flash-reprocess-pilot.md` 游离 wiki 根目录（其他决策均在 decisions/） |

---

## 四、UNCERTAIN（5 项，缺证据）

| ID | 内容 | 缺什么证据 |
|---|---|---|
| U1 | INSERT OR REPLACE 覆写是否真实发生（取决于 Hermes 是否每次传全量会话历史） | Hermes 调用方行为 |
| U2 | Hermes hook 对未捕获异常的处理（决定 BUG-02 最终影响） | Hermes 插件装载器源码 |
| U3 | `pick_injection_realities` 4B 实际耗时（决定 BUG-08 严重度） | 真实链路延迟采样 |
| U4 | `_estimate_conv_tokens`（chars//4）对中文的估算误差 | 真实 tokenizer 基准 |
| U5 | `run_reality_merge` 冷启动"余弦退化"是否曾被依赖 | 运行日志证据（已并入 BUG-01） |

---

## 五、审计结论

- **方向 B / E-stage / F-stage / topic 定级主体与文档一致**（hook 注册 8 项、写即落盘、tail 基本保护、schema 主键字段均符合）。
- **最严重问题集中在**：
  1. 🔴 **v7 reality 归并链路恒 create**（BUG-01/D2）——v7 核心功能实际未生效
  2. 🔴 **tail-protection 设计未实装**（D1）——token 预算保护被硬编码 2 轮替代
  3. 🟠 **注入/提问云/补缺三条静默失效链路**（D3/D4/D6）
- 建议修复优先级：BUG-01（含补 test_reality_merge）→ D1 → D6/BUG-03 → D4 → BUG-02/08（用户路径稳定性）→ 文档同步（D9~D17 批量）。

*注：本报告为审计结论，未修改任何代码。修复需 Codex 执行 + 真实环境回归（789 基线）+ 多 profile 同步。*
