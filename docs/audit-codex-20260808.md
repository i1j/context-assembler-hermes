# CA 插件 第二轮审计报告（Codex 执行 + tester 独立复核）

- 审计时间: 2026-08-08
- 代码版本: `75b55d1`（第一轮修复提交，2026-08-07）
- 审计范围: `docs/wiki/`（14 architecture + 33 decisions）+ `__init__.py` + `topic_manager.py` + `ca/` 全部模块 + `plugin.yaml` + `ca/settings.yaml`
- Hermes 源码（U1/U2 证据）: `/home/i1j/.hermes/hermes-agent/`（agent/turn_context.py、hermes_cli/plugins.py、hermes_cli/cli.py、hermes_cli/cli_agent_setup_mixin.py、hermes_state.py）
- 测试基线: 794 collected（真实环境 **792 passed / 1 skipped / 1 xfailed，0 failed**；Codex 沙箱 3 failed 均为 urllib3 网络限制，非代码回归）
- 验证方式: Codex 静态对照 + 运行时探针复现；tester 对全部发现独立复核（grep/读代码/实测复现 BUG-10/11），**全部属实，零误报**

## 一、第一轮修复回归验证（11 项正确 / 4 项不彻底）

| 修复 ID | 结果 | 结论 |
|---|---|---|
| BUG-02/03/04/05/06、D5/D7/D8、D10-D17 | ✅ 正确 | 主链路修复到位 |
| **D1** | ⚠️ 已回退（两次用户裁定，最终结论） | **2026-08-08 用户裁定（两次纠正）：①「保护后 2 轮正常对话是正确设计」——不实施「预算未用尽保护全部」；②「+ 超预算时向前扩展」也是错误设计——动态扩展破坏前缀稳定性 → 云端 prompt 缓存命中率下降。** 第一轮 D1 修复（预算扫描+扩展，75b55d1）为错误方向，已恢复 75b55d1^ 原始硬编码实现（`git checkout 75b55d1^ -- ca/a_stage.py`，零 diff）。**语义澄清**：`protect_tail_tokens`（20000）真实语义 = 话题块 token 保护（强制切分过长主题块），由 `TOPIC_PEAK_TOKEN`（20000，`topic_manager._apply_water_pressure` 水位满压消费）承接——**不是 tail 扩展预算**，审计原先的「D1 不彻底」判定方向错误。文档已同步（decisions/13、config.py 注释、settings.yaml） |
| **BUG-09** | ⚠️ 不彻底 | 存在性检查只防 `(turn, seq=0, role=user, Elm 相同)` 重放；thought/tool/fin 行仍 `INSERT OR REPLACE`（store.py:168-191）。U1 已证实正常路径 turn 严格递增不复写 → 实际风险低，但 arch 02「行不可变」+ arch 03「幂等」文档与实现矛盾未解 |
| **BUG-01** | ⚠️ 不彻底 | `find_s_candidates` 返回候选只有 name/hdl/title/overview（theme.py:452-461），**无 `current_status`/`member_hdls`** → `format_decide_reality_input` 的 goals 判定锚与「家族一致性成员」恒空 → 4B merge 决策信息降级（新-U3） |
| **D9** | ⚠️ 部分 | EmbeddingClient 已统一读 Config；但 settings.yaml 25 键仅 4 键被消费（D-新1）、plugin.yaml config_schema 全无消费点（D-新5）、默认值三处不一致（BUG-13） |

## 二、Bug 清单（新发现 5 项，BUG-10/11 实测复现）

| ID | 严重度 | 位置 | 现象 | 证据 | 影响 |
|---|---|---|---|---|---|
| **BUG-10** | 🟠 中 | `ca/health.py:28-29` | `check_store` 引用 v5.0 已删属性 `store._checkpoint_thread`/`_checkpoint_interval` → 每次调用 AttributeError → 恒 unhealthy | ✅ **实测复现**：`{'status':'unhealthy','error':"'SQLiteStore' object has no attribute '_checkpoint_thread'"}` | 健康检查与 Prometheus 指标 `ca_store_healthy` 恒 0 误报；WAL/db size 指标不可达；`tests/config/test_health.py` 只断言 dict 结构未拦截 |
| **BUG-11** | 🟠 中 | `ca/refinement.py:387-393,566-570` + `ca/store.py:390-393` | `update_reality(..., db_path=conn)` 传 sqlite3.Connection，`_get_topic_conn` 对 db_path 调 `p.resolve()` → AttributeError | ✅ **实测复现**：`AttributeError: 'sqlite3.Connection' object has no attribute 'resolve'` | `REFINEMENT_ENABLED` 默认 False 当前未触发；一旦开启 L4 每次精炼回写 reality 恒失败（循环 try 兜住则静默丢回写） |
| **BUG-12** | 🟠 中 | `ca/reality.py:724-751` `_update_query_centroid` | `embed_client.embed()`（:736）裸调用无 try/except——第一轮 BUG-02 修了 topic_manager + pre_llm_call 两处，**漏掉此调用点** | ✅ 代码确认 | 嵌入服务故障时 strand 已写 strand_summaries 但**该话题块所有 reality 归并整体失败**（merge/create 丢失，下次无法重放） |
| **BUG-13** | 🟡 低 | `plugin.yaml` / `ca/settings.yaml` / `ca/config.py:80-91` | 默认值三处不一致：embed_endpoint 11439 vs 11435 vs 11435；llm_endpoint 11440 vs 11435 vs 11435；embed_model `qwen3-embedding:0.6b` vs 同 vs `dengcao/Qwen3-Embedding-0.6B:Q8_0`；llm_model 缺 `:latest` | ✅ 代码确认 | 任何一处的「默认值」都不可信，用户改文件无效 |
| **BUG-14** | 🟡 低 | `ca/embedding.py:26` | `_EMBED_DIM = int(os.getenv("CA_EMBED_DIM", "1024"))` 仍绕过 Config（D9 修复后唯一残留 getenv 配置项） | ✅ 代码确认 | 无法热重载、与 config 体系不一致 |

## 三、设计-实现偏离（新发现 9 项）

| ID | 严重度 | 现象 |
|---|---|---|
| D-新1 | 🟠 中 | `ca/settings.yaml:3` 声明「优先级：环境变量 > settings.yaml > 代码硬编码」，实际仅 4 键（protect_tail_tokens/topic_jaccard_entry/topic_jaccard_chain/topic_peak_token）被 `_YAML_DEFAULTS` 读取；**其余 25 键全库无消费点** → 运维改文件静默无效 |
| D-新2 | 🟠 中 | arch 07/08 声称「双路检索+RRF 是 A-stage 上下文来源」，实际 A-stage 从 turn_stream 直接读全量（`read_turn_stream_all`），从不触碰 `self.cache`；`retrieval.py` 无生产调用方、`get_bm25_snapshot` 无消费方 → 整个 BM25/向量双路检索子系统为死代码，文档误导（第一轮仅修「构造不崩」） |
| D-新3 | 🟡 低 | arch 05 三区表声称 REL thought/tool→Fct，代码 `_THOUGHT_TOOL_MAP = {ACT: FCT, REL: HDL}`（a_stage.py:47-50）+ 模块 docstring 均为 Hdl → 文档自相矛盾 |
| D-新4 | 🟡 低 | plugin.yaml hooks 声明 5 个，实际 `register_hook` 注册 8 个（post_api_request/pre_tool_call/post_tool_call 未声明）→ `hermes plugin list` 元数据/人工排查漏 3 hook |
| D-新5 | 🟡 低 | plugin.yaml config_schema 全库无消费点 → 用户在 Hermes 配置里改 embed_endpoint 等无效 |
| D-新6 | 🟡 低 | arch 09「10 个结构化 Handler」vs 实际 11 个 `_summarize_*` |
| D-新7 | 🟡 低 | arch 13「789/787」vs 当前 794 collected |
| D-新8 | 🟡 低 | 术语残留：`tool_summarizer.py` 大量 l0/l1 变量、`post_process.py:99`「L1 v2 解析器」、`config.py:134/159` L0/L1 注释、arch 11/14 的 L2/L3、`L1_TEMPERATURE`/`L1_MAX_TOKENS` 类字段名 |
| D-新9 | 🟡 低 | `ca/cloud_llm.py:28` 硬编码 `Path.home()/".hermes"/"profiles"/"tester"/".env"`（仅 flash reprocess + scripts 用，非主链路） |

## 四、UNCERTAIN 补证据结果

| U-ID | 结论 | 证据 |
|---|---|---|
| U1 | **已证实（覆写风险小）** | Hermes 每轮 `messages=list(conversation_history)`（turn_context.py:514）→ append 当前 user（:569）→ pre_llm_call 收全量（:1064-1069，已含当前消息，turn 计数含本轮）；CLI 传 `conversation_history[:-1]`（cli.py:14047）；resume 全量加载 lineage（cli_agent_setup_mixin.py:618 + hermes_state.py:7670）。**正常轮次 turn 严格递增、resume 从历史续计，不会覆写**；覆写仅可能发生在同 turn 重放且内容不同（BUG-09 只防同文本重放，边缘残留） |
| U2 | **已证实（影响等级下调）** | `hermes_cli/plugins.py:1908-1928` `invoke_hook` 对每个回调独立 try/except 仅 warning → pre_llm_call 抛异常不中断 Hermes turn，只丢该回调后续逻辑。第一轮 BUG-02 原标「hook 抛未捕获异常」影响偏重；修复本身仍正确 |
| U3 | **仍缺实时采样** | 代码证据：4B 调用 `call_llm_raw(max_retries=1, timeout=120s)`；首轮最坏 ≈ 5s 清账 + embed（10s×2≈30s）+ 4B（120s）≈ **2.5~3 分钟级**。沙箱无 4B 服务无法实测 |
| U4 | **理论分析（低估 4~6×）** | `_estimate_conv_tokens` 用 chars//4：中文每字≈1 token，CJK 场景低估 4~6×。影响：① tail 预算 20000「token」实际只保护约 1/4~1/6 真实 token（与 D1 叠加）；② 水位压力延后触发；③ 英文/代码误差小。方向均为低估 |
| U5 | **已解决** | 已并入 BUG-01：冷启动余弦已真实现（`_cold_start_cosine_candidates` reality.py:761-794），M-5 测试覆盖 |

## 五、文档内部问题

- `architecture/01-overview.md:57` 声称 should_compress 固定返回 False，代码恒 `return True`（__init__.py:294-308），decisions/20 也写 True → arch 01 是过期方
- arch 01 核心术语表 Fct 示例 `{"role":"assistant","summary":"已搜索..."}` 为旧格式，现为 changes/core_change/OODA 四段 JSON
- arch 02「行不可变」+ arch 03「幂等」vs store.py:168 `INSERT OR REPLACE` 矛盾（需同步为「默认不可变 + 重放场景 REPLACE 覆盖」）
- arch 05 三区表 REL thought/tool 与模块 docstring 矛盾（见 D-新3）
- arch 13 测试统计过期（D-新7）
- arch 09 handler 计数过期（D-新6）
- arch 11/14 残留 L2/L3 术语（D-新8）
- decisions/13 未写明「预算未用尽保护全部」边界（D1 不彻底配套）
- 第一轮编号体系（BUG-01~09/D1~17）与任务书 P0/P1 无映射表（流程性建议）

## 六、新 UNCERTAIN（6 项）

- **新-U1（已排除）**：inject.py:368-373 SQL `WHERE profile=? AND (qc 有效 OR centroid 有效)` 疑似 AND 优先于 OR 绕过 profile 过滤——逐字核对为误报，括号语义正确。不构成 bug。
- **新-U2**：同 turn 不同内容重放实际触发概率（U1 已证正常路径不复现）
- **新-U3**：候选缺 current_status/member_hdls 对 4B merge 决策准确率影响（需真实 4B 采样）
- **新-U4**：REFINEMENT_ENABLED 开启后 BUG-11 影响面（未实跑 L4）
- **新-U5**：`collect_turn_fcts` 多 fin 行只取最后一条是否丢摘要（缺「一轮多 fin」真实样本）
- **新-U6**：settings.yaml 死配置是否已被运维按「改文件生效」误用（建议 config.py 启动日志列出未消费键）

## 七、审计结论

第一轮主链路修复正确（11 项），4 项不彻底需跟进：① D1「预算未用尽保护全部」未实现且无测试；② BUG-09 REPLACE 语义与文档「行不可变」冲突未解（U1 证实风险低，以文档同步为主）；③ BUG-01 候选缺 4B 决策输入；④ D9 配置体系（settings.yaml 死配置 + plugin.yaml 无消费点 + 默认值不一致）。

新发现 5 bug（health 恒 unhealthy、refinement 回写崩溃为**实测复现**）+ 9 偏离。建议修复优先级：**BUG-10/11/12（功能失效/崩溃类）→ BUG-13/14 + D1 补全 → D-新1/5 配置体系 → 文档批量（D-新2/3/4/6/7/8/9 + arch 01/02/03/05/09/11/13/14 + decisions/13）**。

*注：本报告为审计结论，未修改任何代码。修复需 Codex 执行 + 真实环境回归（794 collected 基线）+ 多 profile 同步。*
