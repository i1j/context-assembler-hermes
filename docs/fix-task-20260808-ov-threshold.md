# 信号 C 阈值分层判断任务书（P0 泛文档排除 + P1 先代码后 4B，2026-08-08）

> 前置：OV 全量入图已完成（228 ov_doc 节点，references_ov 52 条已落图）。本任务优化信号 C 落图质量：
> - P0：泛文档（导航类）不做 references_ov target —— 零 token，直接清噪声
> - P1：分层判断「先代码后 4B」—— score 高自动落图，边界区先看词面 Jaccard，冲突才上 4B
> 执行人：Codex（实现）+ tester（验收）。**验证阶段禁止 apply 真实环境/权威源，落盘动作由 tester 执行。**

## 1. 背景与数据证据（52 条已落图边作真值）

### P0 泛文档排除（导航类命中 8/52 条）
实测 52 条 references_ov 边中 **8 条命中导航类节点**：

| target | 命中数 | 合理性 |
|---|---|---|
| ov_doc_index.md（根 INDEX.md） | 5（reality_40/75/106/191/193） | 3 噪声（40/75/106）+ 2 勉强 |
| ov_doc_overview.md（testing/overview） | 1（reality_199） | 噪声 |
| ov_doc_c-index.md（决策点目录） | 1（reality_15 C-stage 核查） | **合理，保留** |
| ov_doc_agents.md（AGENTS.md） | 1（reality_210 session_search 工具） | **合理，保留** |

**排除规则必须精确匹配 uri 尾段**（非前缀）：
- 排除：uri 尾段 ∈ {`INDEX.md`, `README.md`, `changelog.md`, `overview.md`}（导航/索引类）
- 保留：`01-overview.md`（architecture 实质文档）、`C-INDEX.md`/`H-INDEX.md`/`GAP-INDEX.md`（决策点目录，有实质关联）、`AGENTS.md`（技术参考）

### P1 分层判断（实验证据）
- score≥0.60 档：31/52 自动落图合理（60%）
- score 0.55-0.60 边界档：21 条中 17-18 合理（reality_13→hdl 设计 0.576、reality_16→mutation 0.595、reality_180→37-reality 0.589...），仅 3-4 噪声
- 词面 Jaccard（reality name vs 文档 title）：合理边可达 0.105-0.417（reality_10→工具摘要引擎 0.417、reality_18→TP-008 0.222、reality_55→P-002 0.105）；噪声边（index 系）≤0.067
- 结论：**纯 Jaccard 无判别力（多数 0）但可作为「低分放行」的正信号**；边界冲突区（score 0.55-0.60 且 jaccard<0.05）是 4B 复核的合适范围

## 2. 目标

- P0：references_ov 不再指向导航类文档（INDEX/README/changelog/overview）
- P1：信号 C 分层——score≥0.60 自动落图；0.55-0.60 看词面 Jaccard（≥0.05 放行，<0.05 上 4B 复核）；<0.55 不落图
- 4B 复核仅用于边界冲突区（实测 ~19 条 × 0.4s ≈ 8s/轮，成本可控）

## 3. 改动清单（全部在 ca/fact_linking.py + tests/unit/）

### 3.1 P0：导航类文档排除（新增 `_is_navigation_doc(nid, hit)`）

- 判定：从 hit 取 uri（或 title 回退），uri 尾段（最后一个 `/` 后的段，去 `.md`）∈ {`index`, `readme`, `changelog`, `overview`} → True
- 注意：**精确匹配尾段**——`01-overview` 不命中（尾段是 `01-overview`），`C-INDEX` 不命中（尾段是 `C-INDEX`）
- title 形态命中（无 uri）→ 用 title 段同样规则
- 在 `_run_ov_references` 落图循环内、known_ov 检查之后：`if _is_navigation_doc(hit): continue`

### 3.2 P1a：词面 Jaccard 辅助（新增 `_doc_title_jaccard(reality_text, hit)`）

- 从 hit 取 uri → 若 uri 存在则用 `_fetch_ov_raw(uri)`（已有，WebDAV）拉文档，提取 frontmatter title（复用 wiki_to_graph.py `_load_ov_doc_titles` 的 title 提取逻辑——frontmatter title → H1 → 文件名 fallback）
- 计算 `_bigrams(reality_text) & _bigrams(title)` 的 Jaccard；title 拉取失败/为空 → 返回 0.0（退化）
- **缓存**：dict uri→title，避免同轮重复拉取

### 3.3 P1b：分层逻辑（改 `_run_ov_references` 落图分支）

```
score < OV_REFERENCE_THRESHOLD(0.55) → continue        # 阈值下不落
score >= 0.60 → 直接落图                                # 自动层（代码零 token）
0.55 <= score < 0.60 →
    j = _doc_title_jaccard(...)
    j >= 0.05 → 落图（词面正信号）
    j < 0.05 → _judge_ov_reference_4b(reality, hit)     # 4B 复核层
               判 references → 落图；判 none → 跳过
```

- 4B 复核新增 `_judge_ov_reference_4b(reality, hit)`：prompt 给 reality name/hdl/current_status + 文档 title/uri，判 `references`/`none`（复用 `call_llm_raw` 模式，解析失败重试一次，仍败 → 跳过候选不崩）
- 新增常量：`OV_AUTO_THRESHOLD = 0.60`（自动落图层阈值）、`OV_JACCARD_MIN = 0.05`（词面放行阈值）——均为模块级常量，允许 env 覆盖（`CA_OV_AUTO_THRESHOLD`/`CA_OV_JACCARD_MIN`）
- `OV_REFERENCE_THRESHOLD`（0.55）语义不变：仍是「低于此不落图」

### 3.4 P2：top-K 截断改进（新增 `_ov_top_hits` 遍历）

**实验证据**：8/10 样本 reality 有 ≥3 命中，top-2/3 与 top-1 gap 普遍 <0.03（reality_130: 0.594→0.588→0.580 三连有效；reality_10: 0.667→0.647→0.646 工具摘要三连）。当前只取 top[0] 导致每个 reality 最多 1 条 references_ov 边，漏掉大量有效关联。但需护栏防噪声：

- **`OV_TOP_K = 3`**（env `CA_OV_TOP_K` 可覆盖）：`_run_ov_references` 对每个 reality 遍历前 K 个命中，而非只取 top[0]
- **同 nid 去重**：同一 reality 的 K 个命中中，`_ov_doc_nid` 相同的只处理第一个（碎片与主文档同 nid 的重复命中实测出现，reality_150 top1/top2 同 nid）
- **护栏阈值**：top-1 走完整分层（≥0.60 自动 / 0.55-0.60 词面+4B）；**top-2/3 仅自动层**——score ≥ `OV_TOP_K_MIN_SCORE = 0.60` 才落图，0.55-0.60 的 top-2/3 直接跳过（不上 4B，控制成本；噪声多在低分 top-K）
- 每个候选（无论 top 位次）都过 `_is_navigation_doc` + known_ov 防悬挂 + `_add_edges` 幂等去重（跨 reality 的重复边由 _add_edges 的 (source,target,relation) 去重兜底）

### 3.5 不改的部分

- `_ov_doc_nid`、`_extract_ov_hits`、`_hit_score`、`_add_edges`、known_ov 防悬挂逻辑 —— 不动
- `scripts/wiki_to_graph.py`、`ca/inject.py` —— 不动

## 4. 验收标准（tester 执行）

1. **P0**：52 条旧边中 index.md/overview.md 命中在重跑后不再出现；01-overview/C-INDEX/AGENTS 仍可命中
2. **P1 分层**：mock 测试覆盖——score≥0.60 落图、0.55-0.60+jaccard≥0.05 落图、0.55-0.60+jaccard<0.05 且 4B 判 references 落图、4B 判 none 跳过、score<0.55 不落
3. **P2 top-K**：mock 测试覆盖——同 reality 多命中建多边（不同 target）、同 nid 去重（碎片/主文档只建 1 条）、top-2/3 score≥0.60 落图、top-2/3 score<0.60 跳过不上 4B（mock 断言 4B 调用次数 = 仅 top-1 边界区）
4. **导航排除精确性**：`_is_navigation_doc` 单测——index.md/readme.md/changelog.md/overview.md → True；01-overview.md/C-INDEX.md/AGENTS.md → False
5. **回归**：测试基线 871 passed 不降；旧测试断言不破（title 形态命中需兼容）
6. **真实环境**：tester 配阈值重跑信号 C → references_ov 边噪声减少（index/overview 清零）、每个 reality 最多 3 条边、无悬挂、图一致性 ALL PASS

## 5. 约束

- 只改 `ca/fact_linking.py` + `tests/unit/test_fact_linking.py`（或新增测试文件）；不触碰其它模块
- **验证阶段禁止 apply 真实环境**；落盘（写 graph.json / 配 env）由 tester 执行
- 4B 调用复用现有 `call_llm_raw` 模式（priority="low"，temperature 0.2，max_retries 1），解析失败降级跳过不崩
- title 拉取失败（网络/WebDAV）→ 返回 0.0 退化纯 score 判断，不阻塞主流程
