# TP-009: 手段与目标 — 三方向决策

> 2026-06-22 定稿
> 前置讨论: 方向一(OV 话题摘要)→方向二(CE 历史列表替换干行)→方向三(多 OODA 链集群)
> **关键纠正**: 方向二不是复活旧 v4.x BM25 检索，而是在当前 v5.x 上实现 Hermes `ContextEngine` ABC，通过 `compress(messages) → new_messages` 获得消息列表的完全操控权（删行/插行/重组）
> 参见: 废弃旧线 [[CE-000]]（monkey-patch 接管 `_compress_context`，v5.10 移除）


---
**父节点:** [[R-000]]
**子节点:** [[TP-009a]] [[TP-009b]] [[TP-009c]]
**超驰:** [[CE-000]]（已废弃 — 旧 monkey-patch 方案被此 TP 取代）
---


## 1. 问题

CA 当前通过 `pre_llm_call` / `post_llm_call` 8 个插件钩子 ([[P-004]]) 操作对话历史。操作方式是**内容突变**（content mutation）：

1. `pre_llm_call` → `_assemble()` 在 `conv_hist` 上替换各行的 `content` 字段（Fct 替 Elm、Hdl 替 Fct、"略" 替工具行）
2. `post_llm_call` → C-stage 写 ca_cache DB

**限制：只能改内容，不能删行。**

`conv_hist` 是 Hermes 已构建好的消息列表，CA 通过 `@_original_messages` 保护区 bypass 最新轮后，逐个替换剩余行的 content。消息列表行数不变——即使所有旧话题都被精简为 Fct，仍然是 N 行占位。

### 为什么「不能删行」是瓶颈

| 场景 | 当前行为 | 理想行为 |
|------|---------|---------|
| 旧话题被新话题完全取代 | N 条 Fct 文本继续占用位置 | N 条→1 条「旧话题已归档」 |
| 工具调用记录臃肿 | 只能清空为 `"略"`，tool_call_id 等骨架字段仍占 ~32 tok/行 | 整个 assistant{tc}+tool 链删除 |
| 迭代深度 >5 轮 | 每轮 N 条，压缩后仍 N 条，最终触发 Hermes 内置 compress | 提前主动干行，永不触发内置压缩 |

> **Direction 1（OV 话题摘要）和 Direction 3（多 OODA 链）能改善内容质量，但如果不解决删行问题，改善效果受限于「内容变短但行数不变」的天花板。**


## 2. 方向详解

### 2.1 方向一：接入 OV 做话题摘要 (TP-009a)

**本质**：F-stage 的摘要生成后端从本地 Ollama 切换到 OV API，后续迭代升级为整话题级压缩。

**改动范围**：

```
改动前: pre_llm_call → _assemble()
                         ├─ A-stage: topic_grade → ACT/REL/FAR
                         ├─ replace_mode_injection: 按 grade 替换
                         └─ 每行各自替换 content

改动后: pre_llm_call → _assemble()
                         ├─ A-stage: topic_grade → ACT/REL/FAR
                         ├─ replace_mode_injection: 按 grade 替换
                         ├─ OV API 替换 Ollama（F-stage 后端）
                         └─ topic_switch 事件 → OV 话题级压缩
                             └─ 非活跃链全部 → 1 条话题摘要
```

| 子项 | 说明 |
|------|------|
| ✅ F-stage 后端替换 | `_call_llm_for_fct()` 从 Ollama → OV API，不改 schema，不改 A-stage |
| ✅ 话题级压缩 | topic_switch 时，用 OV 为旧话题生成一条话题摘要，替换该话题所有 turn 的 content |
| ❌ 不能删行 | 仍然是逐行 content 替换，行数不变 |

**独立价值**：即使没有方向二，OV 模型质量优于本地 Ollama，直接改善 Fct 质量。

### 2.2 方向二：在 v5.x 上利用 CE 接口实现历史列表替换干行 (TP-009b)

**本质**：CA 从插件钩子升级为 Hermes `ContextEngine` ABC 的实现，通过 `compress(messages) → new_messages` 获得**完全的消息列表操控权**。

#### 2.2.1 什么是 ContextEngine ABC

`agent/context_engine.py` 定义了 Hermes 的标准压缩引擎接口：

```python
class ContextEngine(ABC):
    def should_compress(self, prompt_tokens: int = None) -> bool: ...
    def compress(self, messages, current_tokens=None, focus_topic=None) -> List[Dict]: ...
    def on_session_start(self, session_id: str, **kwargs): ...
    def on_session_end(self, session_id, messages): ...
    def get_tool_schemas(self): ...
    def handle_tool_call(self, name, args, **kwargs): ...
```

目前只有 `ContextCompressor`（默认内置）实现了这个接口。配置文件中的 `context.engine: compressor` 控制选择哪个实现。

#### 2.2.2 与废弃的 CE-000 的区别

| 对比 | CE-000（废弃） | TP-009b（新方向） |
|------|:------------:|:---------------:|
| 接入方式 | monkey-patch `agent._compress_context` | 实现 `ContextEngine` ABC，`context.engine: ca_assembler` |
| 稳定性 | 对象属性覆盖，脆弱 | 标准插件接口，与 Hermes 核心解耦 |
| session 管理 | 需额外包装 `_persist_session` | `compress()` 返回新列表后 Hermes 自动处理 state.db |
| 保护末行 | 自行实现尾区检测 | `protect_last_n` 原生支持 |
| 轮转 session_id | 红线约束不许调 | `conversation_compression.py` 自动轮转 |

#### 2.2.3 怎么实现删行

`compress()` 接收全部消息列表，返回任意重组后的新列表：

```python
def compress(self, messages, current_tokens=None, focus_topic=None):
    # 1. 从 messages 重建已处理的历史
    #    （复用现有 _assemble 中的 topic_grade + turn_plan）

    # 2. 识别可合并的旧话题链
    #    话题切换点 → 旧话题全部内容 → 1 条 user 摘要消息

    # 3. 构建输出列表
    #    [system] + [head 保护区] + [旧话题摘要×1] + [tail 保护区]
    #    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    #    行数从 N 条降为 ~3-5 条

    # 4. 尾部保护：protect_last_n 控制最后 N 条 users 不碰
    return new_messages
```

关键能力：
- **删除整行**：不包含在 `new_messages` 中的旧行就是被删了，LLM 永远不会看到
- **合并多行为一条**：N 条旧话题消息 → 1 条 user 摘要消息
- **删除工具链**：assistant{tc} + tool 行组可以整组删除（信息已融入摘要）
- **控制尾部**：`protect_last_n = 2` 保护最后 2 轮完全不碰

#### 2.2.4 post_llm_call 如何处理

`compress()` 触发后 Hermes 做 session 轮转，新 session 的消息从返回列表开始累积。C-stage（写 ca_cache）在 `post_llm_call` 钩子中继续进行，但此时写的是**新 session 的** turn 数据。

改造方案：CA 同时注册为 CE 引擎（`compress()`）+ 插件钩子（`post_llm_call` → C-stage）。通过 `context.engine` 激活 CE 模式时，`pre_llm_call` 钩子静默跳过（已无传统 assemble 需求），`post_llm_call` 继续 C-stage 写 ca_cache。

### 2.3 方向三：多 OODA 链 + 事实摘要集群 (TP-009c)

**本质**：从「一轮一 OODA」→「会话多 OODA 链」，topic_manager 从按对话轮 Jaccard 聚类改为按 OODA 链 embedding 聚类。

**改造面**：

| 组件 | 当前 | 改造后 |
|------|------|--------|
| F-stage | 每轮 1 条 OODA（单链） | `turn_stream` 增加 `chain_id` 列，多链输出 |
| F-stage prompt | `FCT_GENERATION_PROMPT` 单体式 | 多链聚合式，按 `chain_id` 分组输出 |
| topic_manager | `_assign_topic()` Jaccard 算对话轮相似度 | 按 OODA chain embedding 聚类 |
| compress() 删行 | 旧话题全量→1 条摘要 | 按 chain 级压缩，更精准 |

**与方向二的关系**：方向三依赖方向二的删行能力。方向三决定「按什么粒度聚」，方向二执行「怎么聚合删行」。方向三可等方向二稳定后再迭代。


## 3. 交叉比较

| 对比维度 | 方向一（OV 摘要） | 方向二（CE 接口） | 方向三（多链） |
|----------|:---------------:|:---------------:|:------------:|
| **干预域** | 摘要质量 | 消息数组形状 | 聚类粒度 |
| **能否删行** | ❌ | ✅ | ❌（依赖方向二） |
| **改造面** | F-stage 后端切换 | 入口从 pre_llm_call → compress()，保持 C-stage | F-stage + topic_manager + compress() |
| **架构侵入度** | 低（后端替换） | 中（接入标准 ABC） | 高（数据模型变更） |
| **重构风险** | 低 | 中（双模式：CE+插件，需防冲突） | 中高 |
| **独立价值** | 高 — 改善 Fct 质量 | 高 — 解决根本瓶颈 | 中 — 依赖前两者 |
| **与内置 ContextCompressor 关系** | 无关 | 替换为其实现 | 无关 |
| **与 OV 的耦合** | 强（F-stage OV API） | 无（仅使用 Hermes CE 接口） | 弱（topic embedding 可本地） |


## 4. 决策

### Phase 1 (P0 — 当前迭代): 方向二（CE 接口）

**理由**：删行是其他所有优化的前提。没有删行能力，方向一和方向三做出的内容改善仍然受限于 N 条消息数组。

**施工步骤**：

```
Step 1: CA 实现 ContextEngine ABC
  ├─ __init__() 注册为 context.engine: ca_assembler
  ├─ compress() 包装现有 assemble 逻辑 + 旧话题合并删行
  ├─ should_compress() 从恒 False → 「有旧话题且 token 超阈值」
  ├─ token 跟踪：update_from_response() 从 API usage 更新
  ├─ should_compress_preflight() 快速检查
  └─ 尾部保护区由 protect_last_n 配置

Step 2: 双模式注册
  ├─ CE 引擎模式（context.engine: ca_assembler）: compress() 驱动
  ├─ 插件钩子模式（pre/post_llm_call）: 保持 v5.10 原行为
  ├─ CE 模式下 pre_llm_call 静默跳过
  └─ 两模式共享 ca_cache 和 topic_manager

Step 3: 删行策略实现
  ├─ compress() 收尾阶段：topic_switch 检测 → 旧话题全部合并为 1 条
  ├─ 摘要格式: [话题 X 已结束，包含 N 轮对话，核心内容: {OV 摘要}]
  ├─ 旧 CE-000 的 _select_tail()/ _extract_tail() 逻辑可复用
  └─ 行数从 N→1，释放 token 预算

Step 4: 验证
  ├─ 全量 pytest 不因 CE 引擎新增而回归
  ├─ state.db 写入原始长表不变（CE compress 不影响 post_llm_call 的 C-stage）
  ├─ 旧话题合并后，LLM 不再执行旧话题请求
  └─ 尾部保护区正确保护最新 2 条 user 轮
```

### Phase 2 (P1 — 平行展开): 方向一（OV 话题摘要）

与 Phase 1 并行施工，不冲突：

```
Step 1: F-stage 后端替换
  ├─ _call_llm_for_fct() 从 Ollama → OV API
  ├─ OV API 路径: http://localhost:1933/api/v1/...
  └─ 格式兼容：仅切换模型，schema 不变

Step 2: topic_switch 事件 → 话题级摘要
  ├─ topic_manager.topic_switch 回调 → OV 为旧话题生成摘要
  ├─ compress() 中遇到 topic_switch = True 的旧链
  ├─ 摘要文本来自 OV summary endpoint
  └─ Phi-4 格式直接注入 compress() 的输出
```

### Phase 3 (P2 — 依赖前两者): 方向三（多 OODA 链）

等待方向一、二稳定后再切入：

```
Step 1: turn_stream chain_id 列
  ├─ F-stage 输出按链分组
  └─ 每个 chain_id 记录从第一条到最新一条的汇聚关系

Step 2: topic_manager 聚类切换
  ├─ _assign_topic() 从 Jaccard(对话轮内容) → embedding(OODA chain)
  └─ 聚类入口：OODA 的「变革」(change) +「起因」(cause) 字段

Step 3: compress() 按干链替换
  ├─ 方向二的旧话题合并 → 按 chain_id 聚合并替换
  └─ 数据驱动：每条非活跃链 → 1 条 chain 摘要
```


## 5. 风险评估

| 风险 | 等级 | 缓解 |
|------|:----:|------|
| CE 引擎模式下 pre_llm_call 和 compress() 冲突 | **高** | 双模式守卫：CE 激活 → pre_llm_call 跳过 assemble，仅 post_llm_call 继续 C-stage |
| session 轮转导致 ca_cache 地址变化 | **中** | compress() 返回后 Hermes 轮转 session_id → post_llm_call 在新 session 下写 ca_cache。ca_cache key 需从 session_id 改为 (profile, original_session_id, turn_index) 组合键 |
| protect_last_n 保护了「不该保护的中间话题」 | **低** | `protect_last_n` 只保护最后 N 条 user 消息。CA 也在 compress() 内做 topic-grade 二次筛选——protect_last_n 是保守保护（晚轮不动），topic-grade 是主动混合（旧轮压缩） |
| OV API 不可用 → F-stage 降级 | **中** | 保留 Ollama fallback（方向一 Phase 2 添加回落链） |
| 压缩后的 session 切换导致 OV memory provider 数据碎片 | **中** | 参考 `design/archive/kanban-review.md` 方案：compress 不切 OV session，仅切 Hermes session ID |


## 6. 状态

- [x] 架构分析完成
- [ ] Phase 1: CA 实现 ContextEngine ABC
- [ ] Phase 1: 删行策略（旧话题合并）
- [ ] Phase 1: 双模式守卫 + 验证
- [ ] Phase 2: F-stage OV API 后端
- [ ] Phase 2: topic_switch OV 话题摘要
- [ ] Phase 3: chain_id + 多链聚类
- [ ] Phase 3: 干链替换


## 相关代码

- `agent/context_engine.py` — ContextEngine ABC（待实现）
- `agent/context_compressor.py` — 内置压缩引擎（对标模板，~2500 行）
- `~/.hermes/profiles/tester/plugins/ca_assembler/__init__.py` — CA 当前实现（pre_llm_call 驱动）
- `agent/conversation_compression.py` — compress 编排器（`compress_context()` → session 轮转）
- `design/decision-points/CE-000.md` — 废弃旧方案（monkey-patch `_compress_context`）
- `design/decision-points/CE-001.md` — 废弃旧方案（retrieve agent 引用）
- `design/decision-points/CE-002.md` — 废弃旧方案（空壳架构）
- `design/decision-points/CE-003.md` — 废弃旧方案（restore-before-write）
- `design/ca-ce-shell-to-real.md` — 废弃旧方案（实装方案）

---

## 修订注记（2026-08-14）

> 方向二实现机制改为 **select_context 路线**：CA 不再以 `should_compress=True` 每轮触发
> `compress()`，而是实现 `CAContextEngine.select_context()` 每轮从 turn_stream DB 重建
> conv_history；`should_compress()` 恒 False，`compress()` 仅保留手动 /compress 回退路径。
> 上文正文保留历史决策内容。
