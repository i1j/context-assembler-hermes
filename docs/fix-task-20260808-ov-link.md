# OV 资源全量入图 + 命名统一任务书（references_ov 打通，2026-08-08）

> 前置：本轮已做僵尸清理 + 知识层社区发现（networkx），图处于干净状态（reality 157=DB、悬挂 0、社区 794/794）。本任务打通 `references_ov` 边——它是 reality↔OV 资源语义关联的核心通道，当前恒空。
> 执行人：Codex（实现）+ tester（验收）。**验证阶段禁止 apply 真实环境/权威源，落盘动作由 tester 执行。**

## 1. 背景与根因（references_ov 恒空三因）

信号 C（fact_linking.py `_run_ov_references`）本应在配置 `CA_OV_REFERENCE_THRESHOLD` 后对全部 reality 检索 OV，命中 ≥ 阈值则建 `references_ov` 边。当前恒空，三因：

1. **阈值未配置**：`CA_OV_REFERENCE_THRESHOLD` 为空 → 探测模式不落图（已确认，本轮将由 tester 配置 0.55）
2. **OV 服务曾中断**：已恢复（实测 /api/v1/search/find 10/10 命中，score 0.510-0.683）
3. **命名不一致（本次要修的核心）**：入图侧与检索侧对同一文档生成不同节点 id →
   - 入图侧 `_load_ov_doc_titles`（wiki_to_graph.py:121）：`doc_nid = f"ov_doc_{doc_title.lower().replace(' ', '_')[:48]}"`（**frontmatter title 命名**）
   - 检索侧 `_ov_doc_nid`（fact_linking.py:370）：从 **uri 文件段**提取（如 `viking://.../decisions/15-fingerprint-dedup/15-fingerprint-dedup.md` → `ov_doc_15-fingerprint-dedup.md`）
   - 实测三组对照全 False：`ov_doc_指纹去重机制设计` ≠ `ov_doc_15-fingerprint-dedup.md`；`ov_doc_38-reality-graph-inject-merge` ≠ `ov_doc_38-reality-graph-inject-merge.md`（差 .md 后缀）
   - 且 `_run_ov_references` 有「宁缺勿错」防悬挂（fact_linking.py:467-469）：`references_ov` 只连**已入图**的 ov_doc 节点 → 图内只有 6 个 ov_doc 节点（5 个 TRACE_SOURCES bigram 命中），绝大多数命中被跳过

## 2. 目标

- OV 全量资源入图：`viking://resources/projects/context-assembler` 下 **231 个 md** 全部建为 ov_doc 节点
- 命名统一：所有 ov_doc 节点 id 采用 **URI 文件段规则**（与 `_ov_doc_nid` 完全一致），metadata 携带完整 viking:// URI
- 配置阈值后信号 C 可正常落图（references_ov 边非空、无悬挂）

## 3. 改动清单（全部在 scripts/wiki_to_graph.py）

### 3.1 新增 `_load_all_ov_docs()`（全量入图数据源）

- 调用 `GET {OV_API}/api/v1/fs/tree?uri=viking://resources/projects/context-assembler`（已验证返回 290 条目/231 md，每项含 uri + rel_path + isDir）
- 过滤：仅 `isDir == false` 且 `rel_path` 以 `.md` 结尾的条目
- 每文档生成 `{"uri", "rel_path", "nid"}`，其中 nid 用 URI 文件段规则：
  - 取 uri 中**第一个非隐藏 .md 段**（跳过 `.overview`/`.abstract` 等隐藏摘要与碎片路径）
  - `nid = f"ov_doc_{段.lower().replace(' ', '_')[:48]}"`（**保留 .md 后缀**，与 `_ov_doc_nid` 的 uri 分支逐字节一致）
- 网络失败/超时 → 返回空列表 + 打印警告，不抛异常（降级：保留原 TRACE_SOURCES 逻辑）

### 3.2 修改 `build_wiki_subgraph` 的 OV 节点构建（wiki_to_graph.py:376-413）

- **无条件建全量 ov_doc 节点**：遍历 `_load_all_ov_docs()` 结果，每个建节点（file_type=knowledge、`_origin=ov_import`、community=0、`metadata={"uri": 完整 viking:// URI}`、source_file=uri），不再只在 bigram 命中时建
- bigram 匹配逻辑**保留但只用于 trace 边**（reality ↔ OV 文档的追溯边），节点改为引用已建的全量节点（`_doc["nid"]`），重复命中不重复建节点
- 命名冲突处理：同一文档的 title 命名旧节点（如 `ov_doc_决策_38：...`）不再生成

### 3.3 修改 `_clean_knowledge_domain`（wiki_to_graph.py:491）清理旧 ov_doc 节点

- 知识域清理新增规则：`ov_doc_` 前缀节点**不在 `_load_all_ov_docs()` 清单** → 删除（含连带边）
- 目的：清掉 title 命名旧节点（如 `ov_doc_ca_↔_openviking_话题自动提交_—_技术方案`、`ov_doc_决策_37：...`、`ov_doc_决策_38：...`），避免同一文档双节点
- 注意：TRACE_SOURCES 中 34/37/38 的 URI 文件段命名节点（`ov_doc_34-idle-refinement.md` 等）在新规则下保留（在清单内）

### 3.4 不改的文件

- `ca/fact_linking.py`：`_ov_doc_nid` 已正确（uri → 文件段），**禁止改动**
- `ca/inject.py`、`ca/graphify_sync.py`：不动

## 4. 验收标准（tester 执行，Codex 不自行验收落盘）

1. **全量入图**：跑 `python3 scripts/wiki_to_graph.py` 后，graph.json 中 ov_doc 节点数 = 231（±1，若 fs/tree 有变动），每个节点 metadata.uri 为完整 viking:// URI
2. **无双节点**：同一 URI 仅一个 ov_doc 节点；旧 title 命名节点（`ov_doc_决策_`、`ov_doc_ca_↔`）已清理
3. **无悬挂边**：graph.json 悬挂边 = 0（/tmp/graph_consistency_audit.py ④）
4. **信号 C 落图**：tester 配置 `CA_OV_REFERENCE_THRESHOLD=0.55` 后跑 fact_linking → references_ov 边非 0，且所有 target 均在图中
5. **回归**：测试基线 795 collected 不降（embed 服务正常时 793 passed / 1 skipped / 1 xfailed）；新增/调整测试覆盖：全量入图节点数、命名统一（title 命名 vs URI 命名）、旧节点清理、`_ov_doc_nid` 与入图 nid 一致
6. **代码节点完好**：file_type=code 节点数不变（1818）

## 5. 约束

- 只改 `scripts/wiki_to_graph.py` + `tests/unit/`（新增测试）；不触碰 ca/ 业务模块
- **验证阶段禁止 apply 真实 OV / 修改权威源**；落盘（写 graph.json / 配 env）由 tester 执行
- 幂等：重复跑不产生重复节点/边
- 不硬编码 OV URI 清单到代码——全量来自 fs/tree 运行时拉取（TRACE_SOURCES 仅作 trace 边补充）
