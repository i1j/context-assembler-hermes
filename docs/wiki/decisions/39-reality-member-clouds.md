# 决策 39：reality 成员云表征 + 注入/归并镜像匹配

> 状态：方案定稿（2026-08-03），待落地
> 血缘：决策 38（共现图 S 模型）的**表征层修订**——本决策推翻 38 的"4B 摘要 centroid 表征"，确立"成员云"为 reality 唯一正表征
> 触发：2026-08-03 会话（提问→reality 映射链验证）

## 一、核心命题（本次会话确立）

**reality 的表征 = 其成员 strand 的"云"（embed 均值），不是 4B 生成的摘要 centroid。**

```
提问云 = 成员 strand 块首提问的 embed 均值     ← 注入侧匹配用（用户怎么问）
工作云 = 成员 strand hdl+ooda 的 embed 均值    ← merge 侧匹配用（工作内容）
```

**匹配域规律（数据实证，决定性）**：输入与 reality 表征**同域**时匹配有效，**跨域**时失效：

| 侧 | 输入 | reality 表征 | 域 | top-3 覆盖率 |
|---|---|---|---|---|
| 注入 | 用户提问 | 提问云（成员提问均值） | 同域（用户语言） | **0.815** |
| 注入 | 用户提问 | 4B 摘要 centroid | 跨域 | 0.008（100 倍差距） |
| merge | strand | 工作云（成员 hdl+ooda 均值） | 同域（工作摘要语言） | **0.881** |
| merge | strand | 4B 摘要 centroid | 跨域 | 0.016（55 倍差距） |

**4B 摘要层是检索毒药**：摘要 centroid 是"系统归纳语言"，与任何输入（提问/strand）跨域；成员云是"输入同域语言"，匹配天然同域。之前注入命中 0/9、cos 直接匹配 0.008 的根因即在此——**不是度量问题（cos/jaccard/BM25），是表征域问题**。

## 二、镜像原则（用户确认）

注入侧（提问→reality 拣选）与 merge 侧（strand→reality 归并）是**同一映射的两个方向**，方法论/流程/参数**镜像**：

| 维度 | 注入侧 | merge 侧 |
|---|---|---|
| 输入 | 用户提问（原话） | strand（hdl+ooda） |
| reality 表征 | 提问云 | 工作云 |
| 候选生成 | 同域 top-K | 同域 top-K ∪ 共现 S |
| 判定 | 4B 工作关联复核 | 4B decide（承接判定） |
| 空结果 | 空注入（宁缺勿错） | new（宁分不并） |

**镜像陷阱（必须遵守）**：merge 侧**禁止**用块首提问匹配（同块 strand 共享提问，无 strand 区分度）——必须用 strand 自身的 hdl+ooda 工作云。

## 三、reality 必有提问（无兜底）

```
reality = strand 集合（归并产物）
strand 必有块首提问（tester 505/505 验证，state.db messages）
→ reality 必有提问云（创建它的 strand 的提问即首个样本）
```

实测：tester 50 reality 全部有有效提问（≥4 字）——**"摘要 centroid 兜底"删除**。单提问 reality（11 个）质心退化为单样本但可用，随归并自动变准。

## 四、注入侧设计（镜像 A）

```
离线：每 reality 提问云 = 成员 strand 块首提问 embed 均值（随归并增量更新）
运行时：
  新提问 embed → 与全库 reality 提问云比 cos → top-3
  → 4B 复核（工作关联判定，38-inject-prompt.md）→ 注入
  → 4B 全拒/无候选 → 空注入（宁缺勿错，全新话题）
数据：top-3 覆盖率 0.815（tester 498 提问，金标准 50 reality）
```

**候选来源可选扩展**：提问云 top-3 命中 reality 后，其**共现邻居**（cooccurrence_events）可并入候选（行为信号补充，镜像 merge 侧的 S 模型）。

## 五、merge 侧设计（镜像 B）

```
候选 = 工作云 top-K（语义同域）∪ 共现 S 候选（行为信号，find_s_candidates）
  → 4B decide（承接判定）→ 归并 / new
数据：工作云 top-3 覆盖率 0.881；共现 S 漏选场景 top-3 0.82（互补：语义 vs 行为）
```

**S 模型（决策 38）定位调整**：共现图捕捉"词面远但工作相关"（行为），工作云捕捉"语义相关"（同域）——**互补融合**，不是替代关系。注入侧镜像加共现扩展。

## 六、数据来源与基建

1. **块首提问提取**：`state.db messages`（Hermes 会话存储，role='user'），strand.turns[0] → 该 session 第 N 条 user 消息（505/505 可连）。tester 的 `ca_topics.db.turn_stream` 为空（勿用）
2. **工程改进**：summarize 时把块首提问快照进 strand_summaries（新列 `query_text`）——后续提问云更新不再依赖 state.db
3. **双云存储**：reality 维护提问云/工作云（成员均值），随 strand 归并增量更新
4. **提问粒度与去重（2026-08-03 用户确认）**：
   - 提问 = **块首 turn 的整段 user 消息**（不拆单句；4B 生成 strand 时基于整段上下文，忠实记录）——**当前 v6.x 临时方案**
   - turn ↔ user 消息严格 1:1（Hermes 结构：1 user → assistant+tool 多轮 → 下条 user）
   - **同块去重**：同块多 strand 共享同一提问，提问云更新时同块同提问只计一次（防止块内 strand 数偏置云均值）
   - **演进路径（7.0）**：从 **Fct 开始区分事务（transaction）**——Fct 为 per-OODA 原子单位，事务粒度细于话题块 → 提问粒度**自然映射到单个问题**（事务级提问，无需整段）。提问云随事务粒度细化自动精化，整段方案届时废弃

## 七、与决策 38 的关系

| 决策 38 内容 | 状态 |
|---|---|
| 共现图模型（cooccurrence_events、S 匹配分、graphify 边） | **保留**（merge 侧行为信号） |
| 注入侧 cos 负向排除（NEG_EXCLUDE_COS） | **废弃**（成员云匹配替代） |
| 摘要 centroid 表征（query_themes_by_semantics 语义检索） | **废弃**（跨域毒药，0.008） |
| 4B 拣选 prompt（38-inject-prompt.md） | **保留**（判定环节，输入改为提问云 top-3） |

## 八、参数与待定

| 参数 | 先验 | 状态 |
|---|---|---|
| 注入 top-K | 3（TOPIC_SUMMARY_RECALL_LIMIT） | 定稿 |
| merge 工作云 top-K | 3~5 | 待定 |
| 共现扩展阈值（注入侧） | R=0.5（决策 38） | 待定 |
| 云更新策略 | 随归并增量 | 待定（原子替换 vs 计数均值） |
| 上下文增强（前块 theme 进匹配） | 用户提议 | 后置（基础版 0.815 已定稿） |

## 九、落地计划

1. **双云存储**：reality 云表（提问云/工作云 embed + 成员计数），归并时增量更新
2. **块首提问快照**：strand_summaries.query_text（数据源基建）
3. **注入侧改造**：`ca/inject.py` → 提问云匹配 top-3 + 4B 复核 + 空注入
4. **merge 侧扩展**：`find_s_candidates` → 工作云 top-K ∪ 共现 S
5. **验证**：端到端（真实切换注入 → merge 归并闭环）+ 冷启动 + 云更新正确性
6. **镜像参数统一**：两侧 top-K/阈值共用配置

## 十、风险与边界

- **提问云时效**：reality 演进后旧提问云过时 → 需时间衰减/定期重算（后置）
- **短提问噪声**（<4 字过滤）：50/50 reality 仍有有效提问，过滤安全
- **同域依赖**：匹配质量取决于 embedding 对"用户语言"的编码——Qwen3-Embedding-0.6B 已验证（0.815/0.881）
- **冷启动（新 profile）**：无历史提问 → 提问云空 → 空注入 → merge 建 reality → 提问云随首个 strand 建立（自举）
