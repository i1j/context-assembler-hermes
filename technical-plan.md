# ContextAssembler 详细设计文档 (v4.3.4-hotfix3)

## 文档信息

| 项目         | 内容                             |
| ------------ | -------------------------------- |
| 文档版本     | v4.3.4-hotfix3（与代码一致）     |
| 对应需求文档 | Software\_REQUIREMENTS.md v4.3.3 |
| 编制日期     | 2025-06-27                       |

---

## 1. 设计概述

### 1.1 设计目标

为 Hermes Agent 构建高可靠性、可观测的上下文记忆系统，核心策略为 **增量 L0/L1 摘要 + 本地 BM25/向量双路检索 + Token 预算闸门 + 全指纹去重**。

### 1.2 关键设计原则

| 原则       | 实现方式                                                          |
| ---------- | ----------------------------------------------------------------- |
| 两阶段解耦 | C‑stage 异步生成摘要，A‑stage 同步组装上下文                    |
| 防御性工程 | 别名匹配、容错解析、截断清洗、深度规范化指纹                      |
| 轻量化     | SQLite 持久化，自实现 BM25、余弦相似度、向量序列化                |
| 可观测性   | 健康检查、Prometheus 指标、阶段统计、调试日志                     |
| 集中配置   | 所有可调参数通过 `config.py` 统一暴露，支持环境变量覆盖和热重载 |
| 线程安全   | 关键数据结构加锁，异步任务可追踪，资源泄漏已消除                  |
| 职责分离   | Plugin 层管理生命周期与断路器，引擎层专注上下文处理               |
| 弹性容错   | 失败冷却、连接池过期重试、WAL 模式校验、主动容量驱逐              |

---

## 2. 系统架构

### 2.1 逻辑架构

```
┌────────────────────────────────────────────────────────────┐
│                      Hermes Agent                          │
│                                                            │
│  ┌─────────────────────────┐  ┌──────────────────────────┐ │
│  │ C‑stage (post_llm_call) │  │ A‑stage (pre_llm_call)   │ │
│  │       异步              │  │       同步               │ │
│  │                         │  │                          │ │
│  │ L2 → LLM 生成 OODA 文本 │  │ 用户输入                 │ │
│  │     → OODAParser 解析   │  │  → 获取快照+数据副本     │ │
│  │     → 清洗+提取 L0      │  │  → BM25/向量检索 (RRF)   │ │
│  │     → 嵌入(L1,L0)       │  │  → 动态预算闸门         │ │
│  │     → 写入 SQLite       │  │  → Head/Middle/Tail 组装 │ │
│  │     → 更新 AssemblyCache│  │  → 全指纹去重           │ │
│  │                         │  │  → 阶段统计（CompressStats）│
│  └───────────┬─────────────┘  └──────────┬───────────────┘ │
│              │                           │                 │
│              └───────────┬───────────────┘                 │
│                          ▼                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ 基础设施：config / store / cache / retrieval /       │  │
│  │           embedding / ooda_parser / post_process     │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

### 2.2 模块依赖与职责划分

```
plugins/context_engine/ca_assembler/__init__.py
│   职责：生命周期管理、断路器状态读写、异常拦截短路
│
└── ca/__init__.py (ContextAssembler)
    │   职责：上下文组装核心逻辑，不感知断路器
    │
    ├── config.py            ← 集中配置 + 校验 + 热重载
    ├── health.py            ← 健康检查 + Prometheus 指标
    ├── store.py (SQLiteStore) ← 持久化，WAL 模式，线程安全
    ├── cache.py (AssemblyCache) ← 内存缓存 + BM25 索引 + 异步快照 + 冷却重试
    ├── retrieval.py (Retriever) ← 双路检索 + RRF 融合
    ├── embedding.py (EmbeddingClient) ← 嵌入服务，多后端，LRU 缓存，连接池过期重试
    ├── ooda_parser.py       ← OODA 解析 + 向量语义去重 (numpy/纯Python)
    ├── post_process.py      ← JSON 容错解析 + 清洗
    ├── prompts.py           ← L1 生成 Prompt
    └── stats.py             ← 阶段性能统计 (CompressStats)
```

**关键边界**：

- Plugin 层全权负责断路器的读写与短路判断，引擎层**不感知**。
- `AssemblyCache` 对外提供不可变快照 `BM25Snapshot` 及线程安全数据副本 `get_snapshot_data()`，避免外部直接依赖内部锁。
- `CompressStats` 嵌入 `assemble` 流程，对每个阶段计时并汇总。

---

## 3. C‑stage 详细设计

### 3.1 异步流程

1. `process_turn_async` 在锁内计算 `turn_index = max(内部计数器+1, 历史用户消息数)`，检查重复任务后启动后台线程。
2. 后台线程执行 `_run_c_stage`：
   - 调用 `_call_llm_for_l1` 生成 OODA 文本（带重试，降级时返回完整五节“无有效增量”）。
   - `OODAParser.parse` 解析 OODA 文本，并与上一轮 L1 做向量语义去重。
   - `robust_json_parse` + `clean_increment` 容错清洗。
   - 提取 L0（`core_change` 前 100 字符）。
   - 并行嵌入 L1、L0（失败时置 None）。
   - `SQLiteStore.write_turn` 持久化。
   - `AssemblyCache.add_turn` 更新内存缓存，异步触发快照重建。
3. 异常时写入“无有效增量”降级记录，更新缓存。

### 3.2 LLM 参数注入

参数三级回退：实例属性 → Config 类属性 → 硬编码默认值。`num_predict` 使用 `Config.LLM_NUM_PREDICT`（默认 24768），支持环境变量覆盖。

---

## 4. A‑stage 详细设计

### 4.1 同步流程

A‑stage 由 `ContextAssembler.assemble` 实现，包含完整的阶段统计（满足 REQ-OBS-002）。

1. 初始化 `CompressStats` 实例，记录原始消息总 token 数（用于后续节比例计算）。
2. 检查消息列表，若缓存快照为空且 Token 溢出则进行硬截断，同时完成统计并返回。
3. 获取不可变快照 `BM25Snapshot`。
4. 通过 `cache.get_snapshot_data()` 获取 `l1_texts` 和 `l0_texts` 的**线程安全副本**，后续组装仅使用副本。
5. 计算 Head（固定数量的有效 L1 摘要）、Middle（其余 L1 对应的 turn）、Tail（尾部保护区域）。
6. 建立 `idx_to_turn` 映射（数组下标 → 实际 turn_index）。
7. 嵌入用户查询（失败时记录错误并降级为 None）。
8. 计算可用升级预算，若预算 > 0 且快照非空，调用 `Retriever.retrieve` 获取升级候选，并记录升级数量。
9. `_build_final_messages` 根据索引和升级标记组装消息，注入 `[~/N]` 标记。
10. 若启用去重，执行 `_deduplicate_messages`。
11. 计算最终消息总 token 数，调用 `stats.finalize`。
12. 当 `CA_DEBUG` 开启时，输出阶段统计日志（格式：`t=12ms, saved=15%, upgrades=(retrieved=3)`）。

**阶段计时覆盖范围**：

- `get_snapshot`：快照与数据副本获取
- `layers`：Head/Middle/Tail 分层计算
- `embed_query`：用户输入嵌入
- `retrieval`：双路检索（含 RRF 融合）
- `assemble`：消息组装
- `dedup`：全指纹去重

### 4.2 消息组装规则

- **系统消息**和**工具调用链**原样保留。
- **Head 区**：若摘要有效，替换为 `[~/{turn_index}] {L1 摘要}`，否则保留原始消息。
- **Middle 区**：若在升级列表中且有有效 L1 摘要，注入 L1；否则展示 L0 文本；若无 L0 则保留原始消息。
- **Tail 区**：保留原始消息。
- 硬截断时，工具组整体保留或移除，被移除时在原位插入 `role: assistant` 提示消息（参与后续去重）。

### 4.3 Token 估算与预算闸门

- 优先使用 CJK 字符比例估算（中文为主文本按 1.5 倍长度），保守处理。
- 预算 = `context_length * 安全系数(0.95) - (Head 占用 + Tail 保护)`，结果 ≤0 时跳过检索。

### 4.4 硬截断设计

- 系统消息无条件保留。
- 工具组（`tool_calls` + 紧随的 `tool` 消息）整体保留或移除。
- 从尾部反向填充，超出预算时停止。
- 插入提示消息 `[工具调用结果因上下文截断已被省略]`。

### 4.5 阶段统计（CompressStats）

- 使用 `CompressStats` 实例记录 A‑stage 各阶段耗时。
- 记录嵌入失败、检索升级数量等事件。
- 组装结束后计算最终 token 数，生成节比例及汇总字符串。
- 调试模式下通过 `logger.debug` 输出，满足可观测性需求。

---

## 5. 全指纹去重设计

- **深度规范化**：递归排序 dict 键、列表保持顺序、解析字符串化 JSON、`None` → `""`。
- **包含 role 字段**：避免跨角色相同内容误杀。
- **系统消息豁免**：无条件保留。
- 去重逻辑在 `assemble` 末尾执行，处理 `_build_final_messages` 输出的消息列表。

---

## 6. 存储层与缓存设计

### 6.1 表结构 (SQLiteStore)

```sql
turn_cache(session_id, turn_index, l0_text, l1_text,
           l0_embedding BLOB, l1_embedding BLOB,
           bm25_tokens TEXT, token_offset, created_at)
```

- WAL 模式（启动时校验是否生效）、NORMAL 同步、busy_timeout。
- 后台 checkpoint 守护线程（使用 `time.sleep(1)` 轮询，避免 Event.wait 竞态）。
- 线程本地连接，空闲超时自动关闭。
- 写入重试（指数退避），批量写入单事务。
- 嵌入 BLOB 安全解包（损坏返回 None）。
- 内存缓存 session_id 列表（24h TTL），写操作后失效。

### 6.2 内存缓存 AssemblyCache

- 维护 L0/L1 文本和嵌入的字典。
- `add_turn` 后提交异步重建任务；连续失败时启用**冷却期**（5s），冷却期内不重建但安排延迟重试（守护 `Timer`）。
- `rebuild_bm25_snapshot()` 构建不可变 `BM25Snapshot`（含 BM25 索引、turn_indices、L1 嵌入字典），通过原子引用替换发布。
- `get_snapshot_data()` 返回当前 L1/L0 文本的线程安全副本。

### 6.3 缓存构建器 CacheBuilder

- 从 SQLite 加载历史数据填充 `AssemblyCache`，失败时重置内部字典并构建空快照，确保返回可用缓存。

---

## 7. 嵌入服务设计

- 多后端支持：Ollama / sentence-transformers / fallback。
- 线程安全 LRU 缓存（手动实现），**仅缓存真实后端结果**，fallback 伪向量不缓存（防止永久退化）。
- 并行编码（`ThreadPoolExecutor`），可配置超时。
- Ollama 连接池使用 `urllib3.PoolManager`；网络相关异常时自动 `pool.clear()` 再重试。

---

## 8. 配置管理设计

- 所有可调参数集中在 `Config` 类，通过环境变量覆盖。
- 支持 `reload()` 热重载 + `validate()` 校验（上下限检查），reload 失败时保留旧值并记录严重错误。
- 关键新增配置：`LLM_NUM_PREDICT`、`CONTEXT_LENGTH`、`SESSION_TTL`。

---

## 9. 健康检查与监控

- `HealthCheck.check_store`：检查连通性、WAL 大小、数据库文件大小（不主动执行 checkpoint）。
- `HealthCheck.check_embedding_client`：缓存命中率、失败次数、降级使用量。
- `get_prometheus_metrics` 导出指标，标签值转义，故障/降级计数器使用 gauge 类型。
- A‑stage 阶段统计通过 `CompressStats` 提供内部性能计时，配合调试日志使用。

---

## 10. 错误处理与降级

| 组件            | 错误                | 处理                              |
| --------------- | ------------------- | --------------------------------- |
| SQLiteStore     | 锁竞争              | 指数退避重试                      |
| EmbeddingClient | 超时 / 网络错误     | 清除连接池重试，最终降级 fallback |
| A‑stage        | 嵌入失败            | 记录错误，降级为纯 BM25 检索      |
| C‑stage        | LLM 超时 / 解析失败 | 降级写入“无有效增量”            |
| AssemblyCache   | 重建异常            | 进入冷却期，延迟重试，防止雪崩    |
| Plugin          | 连续初始化失败 3 次 | 断路器短路 1 小时                 |

---

## 11. 线程安全设计

- `SQLiteStore`：`threading.local()` 连接 + 空闲超时。
- `BM25Okapi`：`RLock` 保护内部数据结构。
- `AssemblyCache`：`RLock` 保护数据字典；快照原子替换；快照数据获取方法加锁返回副本。
- `EmbeddingClient`：`OrderedDict` + `RLock` 实现线程安全 LRU。
- `SessionManager`：`RLock` + 锁外异步销毁，清理循环主动容量驱逐。

---

## 12. 断路器设计

- Plugin 层维护状态文件 `~/.hermes/.ca_assembler_state_<PID>.json`。
- 连续失败 3 次后设置 `retry_after`（1 小时后），`is_available()` 返回 `False`。
- `on_session_start` 首先检查断路器，若不可用则直接短路。
- 成功初始化或 reset 时调用 `_record_success()` 清除状态。

---

## 13. 关键设计决策 (ADR)

| 决策                                        | 理由                                                 |
| ------------------------------------------- | ---------------------------------------------------- |
| C‑stage 异步化                             | 避免 LLM 调用阻塞用户响应                            |
| `turn_index = max(counter+1, user_count)` | 消除引擎重启场景下的主键覆盖                         |
| 全指纹去重 + 深度规范化                     | 解决嵌套 JSON 键序陷阱，杜绝跨角色误杀               |
| fallback 嵌入不缓存                         | 防止后端恢复后仍使用伪向量，确保检索质量             |
| AssemblyCache 冷却重试                      | 避免连续重建失败耗尽线程池，实现弹性容错             |
| SessionManager 异步销毁 + 主动容量驱逐      | 消除死锁风险，严格控制内存上限                       |
| Checkpoint 守护用轮询代替 Event.wait        | 避免 close 后仍操作数据库，防止资源泄漏              |
| WAL 模式主动校验                            | 防止只读文件系统导致回退到 DELETE 模式，保障并发性能 |
| Embedding 连接池过期自动清除                | 解决长时间空闲后 Nginx/LB 断开导致的首次请求失败     |
| A‑stage 集成 CompressStats                 | 提供阶段级性能计时，满足可观测性要求 (REQ-OBS-002)   |

---

附录：

**

# ContextAssembler v4.3.4-hotfix3 需求追溯表

## 正向追溯：需求 → 实现

| 需求 ID                     | 需求描述                                                                                  | 实现位置                                                                                                         | 状态          | 备注                                                                                               |
| --------------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------------------------------- |
| **C‑stage 异步压缩** |                                                                                           |                                                                                                                  |               |                                                                                                    |
| REQ-FUNC-CSTAGE-001         | 异步非阻塞执行，主线程调用后50ms内返回                                                    | `ca/__init__.py` `ContextAssembler.process_turn_async`                                                       | ✅ 满足       | 直接启动 daemon 线程后立即返回，无阻塞操作                                                         |
| REQ-FUNC-CSTAGE-002         | 增量 L1 摘要生成，LLM 超时/失败时降级写入“无有效增量”                                   | `ca/__init__.py` `_run_c_stage` → `_call_llm_for_l1`                                                      | ✅ 满足       | 重试失败后返回完整五节“无有效增量”格式；异常时写入降级记录                                       |
| REQ-FUNC-CSTAGE-003         | L0 原文提取，从清洗后的 L1 JSON 中提取核心事实文本                                        | `ca/__init__.py` `_extract_l0`                                                                               | ✅ 满足       | 提取 `core_change` 前 100 字符                                                                   |
| REQ-FUNC-CSTAGE-004         | 按需计算优化，C‑stage 不执行全量 Cosine top‑3 计算                                      | `ca/retrieval.py` `Retriever.retrieve`                                                                       | ✅ 满足       | 检索升级在 A‑stage 中按需触发，C‑stage 仅存储嵌入，不做 top‑3                                   |
| REQ-FUNC-CSTAGE-005         | 防覆盖索引计算：turn\_index = max(内部计数器+1, 历史用户消息数)                           | `ca/__init__.py` `process_turn_async`                                                                        | ✅ 满足       | 在锁内计算 `target = max(self._turn_counter + 1, expected_next)`                                 |
| **A‑stage 同步组装** |                                                                                           |                                                                                                                  |               |                                                                                                    |
| REQ-FUNC-ASTAGE-001         | Head/Middle/Tail 分层，Tail 受 `CA_PROTECT_TAIL_TOKENS` 控制                            | `ca/__init__.py` `assemble` → `_compute_tail_start`, `_compute_head_indices`, `_build_final_messages` | ✅ 满足       | 分层清晰，Tail 通过反向累积 token 计算                                                             |
| REQ-FUNC-ASTAGE-002         | 双路检索与 RRF 融合，嵌入服务不可用时降级纯 BM25                                          | `ca/retrieval.py` `Retriever.retrieve`                                                                       | ✅ 满足       | 若 `query_embedding` 为 None，仅 BM25 路径；RRF 融合见 `_rrf_fuse`                             |
| REQ-FUNC-ASTAGE-003         | 动态预算闸门，根据 Head 和 Tail 的实际 Token 消耗计算可用升级预算                         | `ca/__init__.py` `_available_budget`                                                                         | ✅ 满足       | 使用 `context_length * 0.95 - (Head+Tail)` 计算                                                  |
| REQ-FUNC-ASTAGE-004         | 预算耗尽快速短路，预算 <=0 时跳过检索                                                     | `ca/__init__.py` `assemble`                                                                                  | ✅ 满足       | `if budget > 0 and snapshot.turn_indices:` 时才执行检索                                          |
| REQ-FUNC-ASTAGE-005         | 无效摘要过滤，core\_change 为“无有效增量”的摘要回退显示原始消息或 L0                    | `ca/__init__.py` `_is_valid_summary` 及组装逻辑                                                              | ✅ 满足       | 无效摘要时 `_is_valid_summary` 返回 `False`，组装时回退到原始消息或 L0                         |
| REQ-FUNC-ASTAGE-006         | 统一 Token 估算，优先 tiktoken，不可用时基于广义 CJK 字符保守估算                         | `ca/__init__.py` `_token_estimate`                                                                           | ✅ 满足       | 目前代码未使用 tiktoken，但实现了 CJK 检测（`\u4e00`-`\u9fff`）及 `len*1.5` 逻辑；空文本短路 |
| REQ-FUNC-ASTAGE-007         | 硬截断兜底，优先保留 System 消息和 Tail 层                                                | `ca/__init__.py` `_hard_truncation`                                                                          | ✅ 满足       | 系统消息优先，从尾部反向保留，工具组整体保留                                                       |
| REQ-FUNC-ASTAGE-008         | 工具组完整性保护，含 `tool_calls` 的 Assistant 消息及其紧随的 Tool 消息视为不可分割整体 | `ca/__init__.py` `_hard_truncation`                                                                          | ✅ 满足       | 分组逻辑识别工具组，整体保留或移除                                                                 |
| REQ-FUNC-ASTAGE-009         | 截断提示消息规范化，插入 `role: assistant` 提示消息以参与去重                           | `ca/__init__.py` `_hard_truncation`                                                                          | ✅ 满足       | 插入 `{"role": "assistant", "content": "[工具调用结果因上下文截断已被省略]"}`                    |
| **全指纹去重**        |                                                                                           |                                                                                                                  |               |                                                                                                    |
| REQ-FUNC-DEDUP-001          | 系统消息豁免，`role: system` 的消息无条件保留                                           | `ca/__init__.py` `_deduplicate_messages`                                                                     | ✅ 满足       | 系统消息直接 `deduped.append(msg)` 不计算指纹                                                    |
| REQ-FUNC-DEDUP-002          | 深度规范化指纹，dict 键排序、list 保序、解析字符串化 JSON、None→空字符串                 | `ca/__init__.py` `_deep_normalize`                                                                           | ✅ 满足       | 递归处理所有规范化要求                                                                             |
| REQ-FUNC-DEDUP-003          | 跨角色防误杀，指纹计算必须包含 `role` 字段                                              | `ca/__init__.py` `_deduplicate_messages`                                                                     | ✅ 满足       | 规范化整个消息对象（含 role），再计算 SHA256                                                       |
| REQ-FUNC-DEDUP-004          | 时序保持，保留原始相对顺序                                                                | `ca/__init__.py` `_deduplicate_messages`                                                                     | ✅ 满足       | 顺序遍历追加，使用 `seen` 集合过滤                                                               |
| REQ-FUNC-DEDUP-005          | 配置与 Fail‑Safe 降级，非法值强制回退为禁用 (False) 并告警                               | `ca/config.py` `_parse_bool_env`                                                                             | ✅ 满足       | 白名单检测，非法值回退 `default`（False）并 WARNING                                              |
| REQ-FUNC-DEDUP-006          | 调试可观测性，`CA_DEBUG` 开启时输出前后消息数量                                         | `ca/__init__.py` `_deduplicate_messages`                                                                     | ✅ 满足       | 在 `if Config.DEBUG:` 块中输出 before/after 数量                                                 |
| **非功能需求**        |                                                                                           |                                                                                                                  |               |                                                                                                    |
| REQ-PERF-001                | A‑stage 同步组装延迟 p95 < 200ms                                                         | 架构设计保证                                                                                                     | ⚠️ 设计满足 | 快速短路、快照复用、LRU 缓存等均已实现，实际性能需压测验证                                         |
| REQ-PERF-002                | C‑stage 异步提交阻塞时间 < 50ms                                                          | `process_turn_async`                                                                                           | ✅ 满足       | 仅做锁竞争和线程启动，耗时极短                                                                     |
| REQ-PERF-003                | 嵌入服务 LRU 缓存命中率 > 80%                                                             | `EmbeddingClient` LRU 设计                                                                                     | ⚠️ 设计满足 | 缓存大小 256，命中率取决于业务，需监控                                                             |
| REQ-REL-001                 | 内存缓存 LRU 驱逐，限制会话数                                                             | `SessionManager` + `AssemblyCache`                                                                           | ✅ 满足       | `max_sessions` 控制，超限主动驱逐                                                                |
| REQ-REL-002                 | 会话列表 TTL 清理，僵尸 ID 超过 24h 清理                                                  | `SessionManager._cleanup_loop` + `SQLiteStore.list_session_ids` 缓存                                         | ✅ 满足       | 清理线程每 5 分钟扫描，`list_session_ids` 缓存有 24h TTL                                         |
| REQ-REL-003                 | SQLite 并发安全，WAL 模式，指数退避重试                                                   | `SQLiteStore`                                                                                                  | ✅ 满足       | WAL 并校验，写入重试指数退避，线程本地连接                                                         |
| REQ-REL-004                 | 断路器隔离，Plugin 层实现，连续 3 次失败后短路 1 小时                                     | `plugins/.../ca_assembler/__init__.py`                                                                         | ✅ 满足       | `_record_failure`/`_record_success`/`is_available` 完整实现                                  |
| REQ-OBS-001                 | 健康检查接口，支持 Prometheus 指标导出                                                    | `ca/health.py`                                                                                                 | ✅ 满足       | 提供 `check_store`、`check_embedding_client`、`get_prometheus_metrics`                       |
| REQ-OBS-002                 | 阶段统计，记录 C/A 阶段执行耗时与成功率                                                   | `ca/stats.py`                                                                                                  | ✅ 满足       | 提供了 `CompressStats` 模块，并在引擎中集成调用（在 `assemble` 中使用）                        |
| **接口与配置**        |                                                                                           |                                                                                                                  |               |                                                                                                    |
| 环境变量配置矩阵            | 所有环境变量均提供默认值                                                                  | `ca/config.py`                                                                                                 | ✅ 满足       | 所有变量均列出，支持 `reload`                                                                    |
| 断路器状态文件接口          | 路径 `~/.hermes/.ca_assembler_state_.json`                                              | 插件层                                                                                                           | ✅ 满足       | `_state_file_path` 返回正确路径，内容结构符合                                                    |

*状态说明：* ✅ 满足 – 完全实现；⚠️ 部分满足/设计满足 – 架构上已支持但需要运行时验证或集成度不够；❌ 未实现

---

## 反向追溯：实现 → 需求

| 模块 / 类 / 方法                                                  | 对应需求 ID                                                                    |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **ca/**init**.py**                                    |                                                                                |
| `ContextAssembler.process_turn_async`                           | REQ-FUNC-CSTAGE-001, REQ-FUNC-CSTAGE-005                                       |
| `ContextAssembler._run_c_stage`                                 | REQ-FUNC-CSTAGE-002, REQ-FUNC-CSTAGE-003                                       |
| `ContextAssembler._call_llm_for_l1`                             | REQ-FUNC-CSTAGE-002                                                            |
| `ContextAssembler._extract_l0`                                  | REQ-FUNC-CSTAGE-003                                                            |
| `ContextAssembler.assemble`                                     | REQ-FUNC-ASTAGE-001, REQ-FUNC-ASTAGE-004                                       |
| `ContextAssembler._build_final_messages`                        | REQ-FUNC-ASTAGE-001, REQ-FUNC-ASTAGE-005                                       |
| `ContextAssembler._compute_head_indices`                        | REQ-FUNC-ASTAGE-001                                                            |
| `ContextAssembler._compute_tail_start`                          | REQ-FUNC-ASTAGE-001                                                            |
| `ContextAssembler._token_estimate`                              | REQ-FUNC-ASTAGE-006                                                            |
| `ContextAssembler._available_budget`                            | REQ-FUNC-ASTAGE-003                                                            |
| `ContextAssembler._hard_truncation`                             | REQ-FUNC-ASTAGE-007, REQ-FUNC-ASTAGE-008, REQ-FUNC-ASTAGE-009                  |
| `ContextAssembler._deduplicate_messages`                        | REQ-FUNC-DEDUP-001, REQ-FUNC-DEDUP-003, REQ-FUNC-DEDUP-004, REQ-FUNC-DEDUP-006 |
| `ContextAssembler._deep_normalize`                              | REQ-FUNC-DEDUP-002                                                             |
| `SessionManager`                                                | REQ-REL-001, REQ-REL-002                                                       |
| **ca/config.py**                                            |                                                                                |
| `Config._parse_bool_env`                                        | REQ-FUNC-DEDUP-005                                                             |
| 所有环境变量默认值                                                | 接口需求 – 环境变量配置矩阵                                                   |
| **ca/store.py**                                             |                                                                                |
| `SQLiteStore` WAL 模式、重试、线程安全                          | REQ-REL-003                                                                    |
| **ca/retrieval.py**                                         |                                                                                |
| `Retriever.retrieve`                                            | REQ-FUNC-ASTAGE-002, REQ-FUNC-CSTAGE-004                                       |
| **ca/cache.py**                                             |                                                                                |
| `AssemblyCache` LRU 与快照                                      | REQ-REL-001 (辅助)                                                             |
| **ca/embedding.py**                                         |                                                                                |
| `EmbeddingClient` LRU 缓存                                      | REQ-PERF-003 (设计支持)                                                        |
| **ca/health.py**                                            |                                                                                |
| `HealthCheck` 及 Prometheus 方法                                | REQ-OBS-001                                                                    |
| **ca/stats.py**                                             |                                                                                |
| `CompressStats`                                                 | REQ-OBS-002                                                                    |
| **plugins/context\_engine/ca\_assembler/**init**.py** |                                                                                |
| 断路器 `is_available` 及状态文件                                | REQ-REL-004                                                                    |

以下是修正后的完整补充设计文档：

# ContextAssembler v4.4.0 详细补充设计文档（最终修正版）

## 文档信息

| 项目     | 内容                               |
| -------- | ---------------------------------- |
| 文档版本 | v3.3（对齐 SRS v4.4.0 最终修正版） |
| 对应需求 | Software\_REQUIREMENTS.md v4.4.0   |
| 编制日期 | 2025-06-29                         |

### 修正说明

- **A‑stage 预算计算**：明确 `_available_budget` 必须使用 `idx_to_turn` 映射和工具组识别，精确扣除系统消息、Head、Tail 的 Token 占用。
- **去重 Fail‑Safe 策略**：非法配置值时强制回退为**启用 (True)**，记录 WARNING，遵循保守策略。
- **配置项表**：补充核心基准配置 `CA_CONTEXT_LENGTH`。
- **L0 生成规则**：明确 `thought_process` 为提取后的思维链。
- **性能基准**：增加延迟指标的测试环境约束。

---

## 1. 设计概述

v4.4.0 在对话轮摘要、双路检索、硬截断等现有能力基础上，新增两大特性：

- **工具轮摘要**：规则引擎生成工具调用轮次的 L1/L0，不经 LLM。
- **L‑stage 异步补全**：独立后台阶段修复缺失的摘要（对话轮 + 工具轮）。

设计严格遵循三原则：不跨层访问内部状态、不引入环境依赖或隐式可调参数、关键业务字段绝不因压缩而丢失。

### 1.1 逻辑架构

```
C‑stage (异步)                L‑stage (独立后台线程)
          ┌───────────┐                ┌───────────────┐
          │ 对话L1生成 │                │ 补全缺失摘要  │
          │ 工具L1/L0  │                │ (对话+工具)   │
          │ 预选工具轮 │                └──────┬────────┘
          └─────┬─────┘                       │
                │                             │
                ▼                             ▼
           ┌──────────────────────────────────┐
           │  turn_cache (SQLite) + AssemblyCache │
           └─────────────────┬────────────────┘
                             │
                             ▼
                      A‑stage (同步)
                      ┌───────────────┐
                      │ 分层+拣选+组装 │
                      └───────────────┘
```

---

## 2. 数据结构变更

### 2.1 turn_cache 表

主键变更为 `(session_id, turn_index, turn_type, tool_sub_index)`，新增五列：

| 列名                  | 类型    | 默认值     | 说明                                                                 |
| --------------------- | ------- | ---------- | -------------------------------------------------------------------- |
| `turn_type`         | TEXT    | 'dialogue' | 区分对话轮（dialogue）与工具轮（tool）                               |
| `tool_sub_index`    | INTEGER | 0          | 同一轮次内工具组的序号，对话轮恒为 0，工具轮从 1 递增                |
| `backfill_attempts` | INTEGER | 0          | L‑stage 补全重试次数                                                |
| `l2_text`           | TEXT    | NULL       | 原始 L2 对话文本，供 L‑stage 补全使用                               |
| `_assemble_status`  | INTEGER | 0          | 摘要生成状态：0=正常生成，1=C‑stage 失败待补全，2=L‑stage 永久失败 |

### 2.2 AssemblyCache 扩展

增加工具轮相关字典，键为 `(turn_index, sub_index)` 复合键：

- `tool_l0_texts`, `tool_l1_texts`：工具轮 L0/L1 文本。
- `tool_l0_embeddings`, `tool_l1_embeddings`：对应的嵌入向量。

提供方法：

- `add_tool_turn(turn_index, sub_index, l0_text, l1_text, l0_emb, l1_emb)`：添加工具轮摘要。
- `get_tool_snapshot_data()`：返回工具轮 L1/L0 文本的线程安全副本。

快照 `BM25Snapshot` 新增属性：

- `tool_bm25`：工具轮 BM25 索引。
- `tool_turn_keys`：工具轮复合键列表。
- `tool_l1_embeddings`：工具轮 L1 嵌入字典。

快照重建时分别为对话轮和工具轮构建独立索引，互不干扰。

### 2.3 新增配置项

以下配置项为 v4.4.0 新增或需特别说明的核心配置：

| 配置项                               | 默认值 | 说明                                                                        |
| ------------------------------------ | ------ | --------------------------------------------------------------------------- |
| `CA_CONTEXT_LENGTH`                | 32000  | **核心基准**：上下文总 Token 窗口上限，所有预算计算和硬截断的绝对基准 |
| `CA_TOOL_PRE_UPGRADE_COUNT`        | 3      | C‑stage 预选工具轮数量                                                     |
| `CA_TOOL_MAX_UPGRADE_K`            | 3      | A‑stage 最大升级工具轮数                                                   |
| `CA_TOOL_PRE_UPGRADE_WINDOW`       | 50     | 预选检索窗口（最近 N 轮）                                                   |
| `CA_BACKFILL_DIALOGUE_RATE`        | 2      | 对话轮补全速率（条/秒）                                                     |
| `CA_BACKFILL_TOOL_RATE`            | 5      | 工具轮补全速率（条/秒）                                                     |
| `CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT` | 30     | 等待补全超时（秒）                                                          |
| `CA_TOOL_FIELD_PRIORITY_PROFILE`   | ""     | 字段优先级 Profile 名                                                       |
| `CA_SHUTDOWN_TIMEOUT`              | 5      | 资源清理等待超时（秒）                                                      |
| `CA_BM25_HIT_THRESHOLD`            | 5      | BM25 检索命中阈值，用于动态分配 BM25 与向量候选数量                         |

去重开关 `CA_DEDUP_ENABLED` 沿用原有配置，但改进了 Fail‑Safe 策略（见 4.4 节）。

---

## 3. C‑stage 扩展

### 3.1 整体流程

C‑stage 在生成对话轮 L1 后，按以下步骤处理工具轮：

1. 检测本轮消息中是否包含工具调用。
2. 使用 `_extract_tool_groups` 将工具调用与返回按组划分。
3. 调用 `ToolSummarizer.summarize_group` 生成 L1/L0 摘要。
4. 将摘要及嵌入存入 `turn_cache`（`turn_type='tool'`）并更新 `AssemblyCache`。
5. 以对话 L1 为 Query，在工具轮索引中执行 BM25 检索（预选）。
6. 判断 LLM 是否返回降级文本，设置 `_assemble_status`。

### 3.2 工具组提取

`_extract_tool_groups` 方法将消息列表中的 `tool_calls` 消息及其后续 `tool` 响应消息划分为独立的工具调用组。

- 一个 `assistant` 消息（含 `tool_calls`）及紧随其后的所有 `role: tool` 消息视为一个组。
- 每个组分配一个 `sub_index`，从 1 开始递增（对话轮恒为 0）。
- 同时记录 `start_index`（该组在原始消息列表中的起始位置），用于 A‑stage 匹配。

**关键设计**：C‑stage 和 A‑stage 均通过遍历消息列表中的工具组顺序分配 `sub_index`（从 1 开始），确保跨阶段复合键 `(turn_index, sub_index)` 绝对匹配，防止摘要覆盖或丢失。

### 3.3 摘要生成

`ToolSummarizer` 提供两种摘要方法：

- **`summarize(tool_call_msg, tool_responses)`**：为单个工具调用生成 L1/L0，用于处理独立的一次调用-返回闭环。
- **`summarize_group(tool_calls, tool_responses)`**：为整个工具调用组生成 L1/L0。取第一个 `tool_call` 的主要信息填充 L1 主字段，其余调用的关键信息合并到 `implicit_knowledge` 字段中（如 `"tool_b: 成功获取数据"`）。

**L1 模板字段**：

| 字段                   | 说明                                     | 缺失默认值                                                                          |
| ---------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------- |
| `tool_name`          | 工具名（工具组取第一个）                 | `"unknown_tool"`                                                                  |
| `tool_args`          | 关键参数                                 | `{}`                                                                              |
| `thought_process`    | 思维链（来自 assistant 消息的 content）  | `""`                                                                              |
| `result_summary`     | 结果摘要                                 | 优先提取 result/summary/message/conclusion/output 等字段；若无则为 `"无返回数据"` |
| `error`              | 错误信息                                 | `null`；若工具返回 error 字段则填充，且 result_summary 标记为 `"失败"`          |
| `implicit_knowledge` | 隐含知识（工具组中多调用的结果合并在此） | `[]`                                                                              |
| `next_action_hint`   | 后续建议                                 | `""`                                                                              |

**L0 生成规则**：

- 主体：`[tool_name]：[result_summary 或 error]`，截断至 100 字符。
- 若提取的思维链（`thought_process`）非空且 ≤ 30 字符，且追加后总长 ≤ 100，可追加 `(thinking: [thought_process])`。
- 否则 `thought_process` 仅存入 L1。

**字段优先级配置**：

- 采用 YAML/JSON 配置文件，分为 VIP（全文保留）、P0（头尾截断）、P1（仅名称）、P2（丢弃）四级。
- 支持 Profile 机制，通过环境变量指定不同的优先级文件。

### 3.4 预选工具轮

`_pre_upgrade_tools` 方法以对话 L1 的 `core_change` 文本作为 Query，在工具轮索引中执行 BM25 检索，获取 Top-K 候选（数量由 `CA_TOOL_PRE_UPGRADE_COUNT` 控制），标记为“预选”，存入 `_pre_upgraded_tool_turns` 集合。

### 3.5 LLM 失败标记

当 LLM 返回的文本以 `"核心摘要：无有效增量"` 开头且包含降级特征时，判定为 LLM 失败，对话轮 L1 中 `_assemble_status` 置为 1（待补全）；否则为 0（正常）。

---

## 4. A‑stage 扩展

### 4.1 分层

`_compute_layers_v2` 分别计算对话轮和工具轮的 Head、Middle 和 Tail：

- **对话轮 Head**：最新 `HEAD_AUTO_L1_COUNT` 个有效 L1 摘要的轮次索引。
- **工具轮 Head**：C‑stage 预选产生的 `_pre_upgraded_tool_turns` 集合。
- **Middle**：所有非 Head 的对话轮和工具轮。
- **Tail**：从尾部开始累积 token 量达到 `PROTECT_TAIL_TOKENS` 的位置，Tail 内所有消息保留原文。

### 4.2 检索与拣选

**检索**：`Retriever` 新增 `retrieve_tools` 方法，基于工具轮 BM25 索引和嵌入执行独立检索，返回 `(turn_index, sub_index)` 列表。对话轮检索使用原有 `retrieve` 方法。

**预算闸门**（`_available_budget`）必须精确计算，避免因类型不匹配或映射错误导致预算超发。具体步骤：

1. **系统消息 Token**：遍历 `messages`，累加所有 `role: system` 消息的 `content` Token。
2. **Head Token**：遍历 `messages`，对于每条消息，通过 `idx_to_turn` 映射得到所属轮次的标识：
   - 若消息为系统消息，跳过。
   - 若消息为工具组的一部分（通过 `_extract_tool_groups` 识别），则取整个工具组的原始 Token 总和；若该工具组的复合键 `(turn_index, sub_index)` 在工具轮 Head 集合中，则将整组 Token 累加入 Head 占用。
   - 若为普通对话消息，其 `turn_index` 在对话轮 Head 集合中，则累加该消息 Token。
3. **Tail Token**：从 `tail_start` 索引开始到消息列表末尾，累加所有消息的 Token（工具组同样整体计算）。
4. **预算公式**：`budget = CA_CONTEXT_LENGTH × 0.95 - System_tokens - Head_tokens - Tail_tokens`。若结果 ≤0，则跳过检索。

**候选计算**（`_build_candidates`）计算每个候选的 token 节省量：

- 对话轮：该轮次下所有非 system、非 tool 消息的 token 总和减去摘要 token。
- 工具轮：完整工具组（含 `tool_calls` 消息和所有 `tool` 响应）的 token 总和减去摘要 token。

工具组的匹配使用消息在原始列表中的索引位置（`start_index`），而非对象引用，确保消息列表经过任何重组后仍能正确对应。

**拣选**（`_select_upgrades`）按以下优先级排序：

1. 类型优先级：对话轮 > 工具轮。
2. 同类型内按 token 节省量降序。
3. 在预算范围内依次升级。

### 4.3 消息组装

`_build_final_messages_v4` 处理工具组和普通消息：

**工具组处理**：

1. 提取完整的工具调用组（`tool_calls` 消息 + 后续 `tool` 响应）。
2. 按全局顺序分配 `sub_index`（从 1 开始，与 C‑stage 完全一致）。
3. 构造复合键 `(turn_index, sub_index)`，查找对应的工具轮摘要。
4. 若该键在 Tail 内，保留原文。
5. 若该键在预选集合但未在升级列表中，强制使用 L1 替换（兜底保障）。
6. 若该键在升级列表中，替换为 L1 或 L0 摘要标签 `[~/turn/sub]`。
7. 否则保留原文。

**普通消息处理**：

- Head 中的对话轮使用 L1 替换，标签为 `[~/turn]`。
- Middle 中升级的对话轮使用 L1 替换，未升级的使用 L0。
- Tail 中的消息保留原文。

### 4.4 去重与阶段统计

组装完成后，若去重开关启用，执行全指纹去重，系统消息豁免，其余消息经深度规范化后计算 SHA256 指纹。

**去重 Fail‑Safe 策略**：`CA_DEDUP_ENABLED` 的值通过 `_parse_bool_env` 解析，仅接受 `1/true/yes`（启用）和 `0/false/no`（禁用）。若环境变量设置为任何其他值，系统将**强制启用去重**（保守策略），并记录 WARNING 日志，明确告警配置错误。这遵循“故障导向安全”原则：宁可误杀重复消息，不可因去重失效导致上下文溢出。

**阶段统计模块**：`CompressStats` 已重命名为 `AssembleStats`，并新增以下工具轮相关统计字段：

- `tool_upgrade_count`：A‑stage 升级工具轮数量。
- `tool_pre_upgrade_count`：C‑stage 预选工具轮数量。
- `tool_backfill_success` / `tool_backfill_failure`：L‑stage 补全成功/失败计数。

---

## 5. L‑stage 异步补全设计

### 5.1 线程模型

每个 `ContextAssembler` 实例创建两个守护线程：

- `DialogueBackfillThread`：补全对话轮缺失摘要。
- `ToolBackfillThread`：补全工具轮缺失摘要。

两个线程以固定速率（由配置项控制）独立扫描本会话 `turn_cache` 中 `_assemble_status = 1` 的记录。

### 5.2 补全流程

1. 查询本会话 `_assemble_status = 1` 的记录。
2. 使用记录中的 `l2_text` 作为输入（不依赖外部消息快照）。
3. 对话轮：调用 `_call_llm_for_l1` 重新生成，解析清洗后写回。
4. 工具轮：调用规则引擎重新生成摘要。
5. 成功：更新 `_assemble_status` 为 0，写入数据库和缓存。
6. 失败：递增 `backfill_attempts`；达到 3 次后标记 `_assemble_status = 2`，永久跳过。

### 5.3 触发与关闭

- **触发**：C‑stage 结束时无条件唤醒两个补全线程。L‑stage 线程被唤醒后，会自行查询 `_assemble_status=1` 的记录，若无待处理记录则立即休眠，不会产生无效计算开销。
- **定时自检**：线程内部每 60 秒自动扫描一次，防止事件丢失。
- **关闭**：引擎销毁时，先等待所有 C‑stage 任务完成（`wait_for_pending`），再通知 L‑stage 停止并 `join` 等待，最后释放底层资源。

---

## 6. 线程安全设计

| 组件                        | 机制                    | 说明                                                                              |
| --------------------------- | ----------------------- | --------------------------------------------------------------------------------- |
| `SessionManager`          | `threading.Condition` | 等待会话销毁完成，避免异步销毁期间的竞态                                          |
| `SessionManager` 超时保护 | 强制移除标记            | 当等待销毁超过 `SHUTDOWN_TIMEOUT` 时，强制移除 `_destroying` 标记以防止死锁   |
| `AssemblyCache`           | `RLock` 保护快照读写  | `rebuild_bm25_snapshot` 与 `get_bm25_snapshot` 互斥                           |
| `AssemblyCache` 数据复制  | 锁内复制、锁外重建      | `add_turn` 仅短暂加锁修改字典，快照构建在锁外进行                               |
| L‑stage 线程               | 写入锁 + 快照机制隔离   | 补全线程修改数据时通过 `store.write_turn` 加锁；A‑stage 读取快照副本，不受影响 |
| `SQLiteStore`             | WAL 模式 + 写入重试     | 多线程并发读写安全                                                                |

---

## 7. 错误处理与降级

| 场景                     | 处理                                                                                            |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| 工具 JSON 解析失败       | 生成降级摘要（`result_summary="无法解析"`），不重试，不进入 L‑stage                          |
| C‑stage LLM 失败        | 写入 `_assemble_status=1`，由 L‑stage 后续补全                                               |
| L‑stage 补全失败 ≥3 次 | 写入 `_assemble_status=2`，记录 WARNING，永久跳过                                             |
| 预选等待超时             | 放弃工具轮预选，A‑stage 仅使用对话轮拣选结果                                                   |
| 快照构建失败             | 自动调用 `rebuild_bm25_snapshot()` 重试一次；仍失败则降级为直接返回原始消息（若溢出则硬截断） |
| 嵌入维度不一致           | 检索时过滤不匹配条目，记录 WARNING；若全部不匹配则降级为纯 BM25                                 |
| 会话销毁超时             | `Config.SHUTDOWN_TIMEOUT` 控制，超时后强制继续，记录 WARNING                                  |

---

## 8. 关键设计决策

| 决策                                     | 理由                                                                                                                                                                       |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **工具组粒度**                     | 一个 `tool_calls` 消息及其后续所有 `tool` 响应视为一个工具轮，分配一个 `sub_index`。C‑stage 和 A‑stage 的索引逻辑完全一致，避免逐个 `tool_call` 拆分导致的不匹配 |
| **C/A 阶段子索引逻辑强一致**       | C‑stage 和 A‑stage 均通过遍历消息列表中的工具组顺序分配 `sub_index`（从 1 开始），确保跨阶段复合键 `(turn_index, sub_index)` 绝对匹配，防止摘要覆盖或丢失            |
| **子索引从 1 开始**                | 对话轮 `tool_sub_index=0`，工具轮从 1 递增，清晰区分，便于日志和调试                                                                                                     |
| **预算计算精确化**                 | 使用 `idx_to_turn` 映射和工具组识别，确保 Head 层 Token 包含对话轮和工具组，避免因类型不匹配导致预算超发                                                                 |
| **补全数据源复用 `l2_text`**     | 避免依赖外部消息快照，降低耦合；L‑stage 可独立运行，无需 `messages` 锁                                                                                                  |
| **兜底预选工具轮 L1**              | 即使 A‑stage 预算极度紧张，预选工具轮保证至少以 L1 形式保留在上下文中，防止关键工具信息完全丢失                                                                           |
| **工具组匹配使用索引而非对象引用** | 消息列表可能被重组，对象引用不可靠；使用 `start_index` 确保匹配鲁棒性                                                                                                    |
| **L‑stage 无条件触发**            | C‑stage 无论成功或失败均触发补全线程，由线程自行判断是否有待处理记录，避免遗漏。线程被唤醒后若无待处理记录立即休眠，无额外开销                                            |
| **快照构建失败重试**               | 快照是 A‑stage 的核心依赖，失败时调用 `rebuild_bm25_snapshot()` 重试一次，提高可用性                                                                                    |
| **销毁超时强制清理**               | 当等待会话销毁超过 `SHUTDOWN_TIMEOUT` 时，强制移除 `_destroying` 标记，防止死锁                                                                                        |
| **去重 Fail‑Safe 策略**           | 非法配置值时强制启用去重，遵循“故障导向安全”原则，宁可误杀重复，不可撑爆窗口                                                                                             |

## 附录 A：正向追溯表（需求 → 设计/实现）

| 需求 ID             | 需求描述                           | 设计章节    | 实现模块/方法                                       |
| ------------------- | ---------------------------------- | ----------- | --------------------------------------------------- |
| REQ-FUNC-CSTAGE-001 | 异步非阻塞执行                     | 3.1         | `process_turn_async`                              |
| REQ-FUNC-CSTAGE-002 | 增量 L1 摘要生成                   | 3.5, 3.1    | `_run_c_stage` → `_call_llm_for_l1`            |
| REQ-C-ERROR         | C‑stage 降级标识                  | 3.5         | `_run_c_stage` 中设置 `_assemble_status`        |
| REQ-FUNC-CSTAGE-003 | L0 原文提取                        | 3.1         | `_extract_l0`                                     |
| REQ-FUNC-CSTAGE-004 | 按需计算优化                       | 3.1         | 检索仅在 A‑stage 执行                              |
| REQ-FUNC-CSTAGE-005 | 防覆盖索引计算                     | 3.1         | `process_turn_async` 内 `max(...)`              |
| REQ-TOOL-C001       | 识别工具调用组                     | 3.2         | `_extract_tool_groups`                            |
| REQ-TOOL-C002       | 规则生成 L1 模板                   | 3.3         | `ToolSummarizer.summarize/summarize_group`        |
| REQ-TOOL-C003       | L1 错误处理                        | 3.3         | 错误字段处理逻辑                                    |
| REQ-TOOL-C004       | L0 生成规则                        | 3.3         | L0 生成及 thinking 追加                             |
| REQ-TOOL-C005       | 字段分级体系                       | 3.3         | 优先级配置与提取                                    |
| REQ-TOOL-C006       | 存储与索引                         | 2.1, 2.2    | `store.write_turn`, `AssemblyCache` 工具轮字典  |
| REQ-TOOL-C007       | 无条件摘要生成                     | 3.1         | C‑stage 必定生成                                   |
| REQ-TOOL-C008       | 规则引擎容错                       | 7           | 降级摘要生成                                        |
| REQ-FUNC-ASTAGE-001 | Head/Middle/Tail 分层              | 4.1         | `_compute_layers_v2`, `_compute_tail_start`     |
| REQ-FUNC-ASTAGE-002 | 双路检索与 RRF 融合                | 4.2         | `Retriever.retrieve/retrieve_tools`               |
| REQ-FUNC-ASTAGE-003 | 动态预算闸门（含系统、Head、Tail） | 4.2         | `_available_budget`（使用 idx_to_turn，精确计算） |
| REQ-FUNC-ASTAGE-004 | 预算耗尽短路                       | 4.2         | `budget > 0` 检查                                 |
| REQ-FUNC-ASTAGE-005 | 无效摘要过滤                       | 4.3         | `_is_valid_summary`                               |
| REQ-FUNC-ASTAGE-006 | 统一 Token 估算                    | 4.2         | `_token_estimate`                                 |
| REQ-FUNC-ASTAGE-007 | 硬截断兜底                         | 4.3         | `_hard_truncation`                                |
| REQ-FUNC-ASTAGE-008 | 工具组完整性保护                   | 4.3         | `_hard_truncation` 分组逻辑                       |
| REQ-FUNC-ASTAGE-009 | 截断提示消息                       | 4.3         | `_hard_truncation` 插入提示                       |
| REQ-TOOL-P001       | C‑stage 预选                      | 3.4         | `_pre_upgrade_tools`                              |
| REQ-TOOL-P002       | A‑stage Tail 保护（工具轮）       | 4.3         | `_build_final_messages_v4` 中 tail_start 判断     |
| REQ-TOOL-P003       | 兜底保障                           | 4.3         | 预选工具轮强制 L1                                   |
| REQ-TOOL-P004       | 确定性降级                         | 4.2         | `_select_upgrades` 排序                           |
| REQ-TOOL-P005       | 同类型内确定性排序                 | 4.2         | 按 RRF 得分排序                                     |
| REQ-TOOL-P006       | 升级上限                           | 4.2         | `CA_TOOL_MAX_UPGRADE_K` 控制                      |
| REQ-FUNC-DEDUP-001  | 系统消息豁免                       | 4.4         | `_deduplicate_messages`                           |
| REQ-FUNC-DEDUP-002  | 深度规范化指纹                     | 4.4         | `_deep_normalize`                                 |
| REQ-FUNC-DEDUP-003  | 跨角色防误杀                       | 4.4         | 指纹含 role                                         |
| REQ-FUNC-DEDUP-004  | 时序保持                           | 4.4         | 顺序遍历                                            |
| REQ-FUNC-DEDUP-005  | Fail‑Safe 降级（非法值启用）      | 4.4, 2.3    | `_parse_bool_env` 强制启用策略                    |
| REQ-FUNC-DEDUP-006  | 调试日志                           | 4.4         | DEBUG 输出                                          |
| REQ-L-STAGE-001~007 | L‑stage 补全各项需求              | 5.1~5.3     | `BackfillThread` 及触发逻辑                       |
| REQ-TOOL-CFG01~10   | 配置项                             | 2.3         | `config.py`                                       |
| REQ-PERF-001~007    | 性能需求                           | 3.1, 4.2 等 | 设计保证，需基准测试                                |
| REQ-REL-001~005     | 可靠性需求                         | 6, 7, 8     | 相应模块实现                                        |
| REQ-OBS-001~002     | 可观测性需求                       | 4.4, 外部   | `AssembleStats`, `health.py`                    |

---

## 附录 B：反向追溯表（实现模块 → 需求）

| 模块                                  | 主要职责                             | 满足的需求 ID                                                                                                           |
| ------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `ca/__init__.py` (ContextAssembler) | C/A/L 三阶段控制、拣选组装、预算计算 | REQ-FUNC-CSTAGE-001~005, A‑stage-001~009, DEDUP-001~006, TOOL-C001, TOOL-P001~006, L-STAGE-001~007, OBS-002, C-ERROR |
| `ca/config.py`                      | 集中配置与校验                       | REQ-FUNC-DEDUP-005, TOOL-CFG01~10                                                                                       |
| `ca/store.py`                       | 持久化存储                           | REQ-REL-003, TOOL-C006, L-STAGE-003                                                                                     |
| `ca/cache.py`                       | 内存缓存与 BM25 快照                 | REQ-REL-001, TOOL-C006, 检索性能                                                                                        |
| `ca/retrieval.py`                   | 双路检索 + RRF 融合                  | REQ-FUNC-ASTAGE-002, TOOL-P005                                                                                          |
| `ca/embedding.py`                   | 嵌入服务与 LRU 缓存                  | REQ-PERF-003                                                                                                            |
| `ca/ooda_parser.py`                 | OODA 解析与语义去重                  | REQ-FUNC-CSTAGE-002 (辅助)                                                                                              |
| `ca/post_process.py`                | JSON 容错与清洗                      | REQ-FUNC-CSTAGE-002 (辅助)                                                                                              |
| `ca/prompts.py`                     | L1 生成提示词                        | REQ-FUNC-CSTAGE-002                                                                                                     |
| `ca/stats.py`                       | 阶段统计（`AssembleStats`）        | REQ-OBS-002                                                                                                             |
| `ca/health.py`                      | 健康检查与 Prometheus                | REQ-OBS-001                                                                                                             |
| `ca/tool_summarizer.py`             | 工具轮摘要规则引擎                   | REQ-TOOL-C002~C005, C008                                                                                                |
| `ca/tool_field_priority.yaml`       | 字段优先级配置                       | REQ-TOOL-C005                                                                                                           |
| `ca/lstage.py`                      | 异步补全线程                         | REQ-L-STAGE-001~006                                                                                                     |
| `plugins/.../__init__.py`           | 插件适配与断路器                     | REQ-REL-004                                                                                                             |
| `migration_v4_3_to_v4_4.py`         | 数据库迁移                           | REQ-REL-003 (升级保障)                                                                                                  |
| `plugin.yaml`                       | 插件清单                             | 配置与部署说明                                                                                                          |

---

**文档结束**

---

## v4.4.1 补充设计（2026-06-05）

以下变更在本次调试会话中完成，尚未合并回源项目。

### A. 对话轮与工具轮尾区分离（§4.2 修正）

**问题**：原设计将所有消息放在同一 `_compute_tail_start` 切割线下处理。工具响应（read_file 5K-57K chars）位于消息列表末尾，单条即消耗完对话 tail 预算（20K tokens），导致最近 N 轮对话轮落入压缩区而非保护为原文。

**修正**：对话轮与工具轮是两种不同资源，功能不同，不应混用同一拣选规则。

| 维度 | 对话轮 | 工具轮 |
|------|--------|--------|
| 尾区保护 | `_compute_tail_start`：反向累计**仅对话消息**（跳过 `role=tool`），10K tokens（`//2` 估算后 ≈ 20K chars） | `TOOL_TAIL_TURN_COUNT=2`：最近 N 个**对话轮**的工具原文保留 |
| Head | 最后 3 个有效 L1 → L1 JSON 摘要 | 预升级（`_pre_upgrade_tools`）→ L1 JSON 摘要 |
| Middle | → L0 一行（或检索升级→L1） | → L0 一行（与对话 Middle 一致） |

**工具尾区按对话轮判定而非消息索引**：一个工具组的 `(turn_index, sub_index)` key 中 `turn_index` 标记了它属于哪个对话轮。工具是否在尾区取决于其所属对话轮是否在最后 N 轮，与工具组在消息列表中的物理位置无关。这避免了消息排列导致的误判。

### B. Token 估算修正（§4.3 修正）

**问题**：原估算 `max(1, len(text))` 对 ASCII 文本 1 char = 1 token，严重高估。一条 28K chars 的工具响应被估算为 28K tokens，实际应为 ~7K（4 chars/token）。

**修正**：对齐 Hermes `context_compressor._CHARS_PER_TOKEN=4`：

```python
def _token_estimate(text):
    cjk = sum(1 for c in text if 0x4e00 <= c <= 0x9fff)
    if cjk > len(text) * 0.5:
        return int(len(text) * 1.5)   # CJK: 保守 1.5×
    return max(1, len(text) // 2)     # ASCII: 2 chars/token（偏保守）
```

### C. 上下文窗口查询（§8 修正）

**问题**：`_MODEL_CONTEXT_WINDOW` 维护过时的值（deepseek-v4-flash: 91.5K，实际 1M 窗口），且无法覆盖 Ollama 自定义模型。

**修正**：三级回退。

```
1. Hermes agent.model_metadata.get_model_context_length()
   → 覆盖所有 provider（Ollama / Anthropic / OpenRouter）
   → 同一进程 import，缓存命中时零开销

2. 自有 _MODEL_CONTEXT_WINDOW 查表
   → 离线/单元测试时

3. CONTEXT_LENGTH 默认值（200K，环境变量可覆盖）
   → 兜底
```

压缩预算 = `model_window × COMPRESSION_THRESHOLD`（默认 0.50，对齐 Hermes `compression.threshold`）。
deepseek-v4-flash: 1M × 0.50 = **500K tokens** 预算。

### D. 后台审查轮跳过 LLM（§3 修正）

**问题**：Hermes 后台 skill/memory review 注入的 ~6000 字符系统 prompt（"Review the conversation above and update the skill library..."）被 CA C-stage 送入 LLM 生成 OODA 摘要，结果始终为 `"无有效增量"`——浪费一次 LLM 调用的同时后续 A-stage 无法感知该轮。

**修正**：`process_turn_async`（主线程）读取 `tools.skill_provenance.get_current_write_origin()` ContextVar（由 `conversation_loop.py:412` 设置）。若为 `"background_review"`，将标志传入 daemon 线程（ContextVar 不跨线程传播），跳过 `_call_llm_for_l1`，规则生成结构化 L1：

```json
{"core_change": "系统后台审查", "_assemble_status": 0, ...}
```

与工具轮规则摘要思路一致。

### E. 话题边界检测

C-stage 末尾：当前轮 L1 向量与上一对话轮 L1 向量（从 DB 读取，避免异步线程竞态）计算余弦相似度。低于 `TOPIC_BOUNDARY_DISTANCE`（默认 0.50）→ 递增 `topic_id`。结果通过 `store.upsert_turn_plan_topic()` 持久化到 `turn_plan.topic_group`，供后期 A-stage 按话题归并邻接轮次。

### F. turn_plan 表（新增）

新增 `turn_plan` 表（schema v3），记录每次 `assemble()` 的拣选决策：

```sql
turn_plan(session_id, turn_index, turn_type, tool_sub_index,
          target_level, decision_reason,
          l2_tokens, summary_tokens, tokens_saved,
          rrf_score, upgrade_rank, budget_remaining,
          topic_group)
```

`decision_reason` 枚举：`head | tail | retrieved | pre_upgraded | tool_head | middle`。
`topic_group` 预留话题归并字段。

当前用途：调试比对拣选决策。下一步：基于 turn_plan 做 turn 级拣选组装，不再重建全量消息列表。

### G. 挑拣流程更新（§4.1 修正）

同步流程第 5 步之后增加：

5a. 计算 `tool_tail_turns = 最后 TOOL_TAIL_TURN_COUNT 个对话轮`（默认 2）。
5b. 预算计算中工具轮按 `key[0] in tool_tail_turns` 计尾区 token（而非 `i >= tail_start` 按消息索引）。
5c. 消息组装中工具尾区判定同样改为按 `key[0] in tool_tail_turns`，`tool_head` 优先级高于 `tail`。
5d. 组装后调用 `_compute_and_store_turn_plan()` 写入拣选决策。

### 配置项汇总

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PROTECT_TAIL_TOKENS` | 10000 | 对话尾区 token 预算（`//2` 后 ≈ 20K chars） |
| `TOOL_TAIL_TURN_COUNT` | 2 | 工具尾区保护最近 N 个对话轮 |
| `CONTEXT_LENGTH` | 200000 | 未知模型兜底窗口大小 |
| `COMPRESSION_THRESHOLD` | 0.50 | 压缩警戒比值 |
| `TOPIC_BOUNDARY_DISTANCE` | 0.50 | 话题边界余弦距离阈值 |
