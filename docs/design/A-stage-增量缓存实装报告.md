# A-stage 增量缓存 — 实装报告

## 修改的文件

### `ca_assembler/__init__.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| `__init__` (L214-225) | 新增 `_A_stable_cache` / `_A_cache_turns` / `_A_cache_is_stale` | 缓存数据结构 |
| `on_session_reset` (L281-286) | 新增缓存重置 | /new 或 /reset 时清理 |
| `_simple_mutation_mode_v5` (L453-456) | 末尾新增缓存写入 + `_A_cache_turns` 计数 | 全量路径结果被缓存 |
| `_simple_mutation_mode_v5` → `_full_mutation` 别名 | 下行 `_full_mutation = _simple_mutation_mode_v5` | 调度入口统一叫法 |
| `_incremental_mutation` (新方法, ~170 行) | 增量路径：Step 1(尾部边界) → Step 2(缓存扫描) → Step 3(Delta收集) → Step 4(替换+Fct pending防护) → Step 5(写缓存) | 核心逻辑 |
| `pre_llm_call_v5` (L634-678) | 缓存调度：话题切换→cache=None→全量; stale→全量; 有效→增量 | 分派入口 |
| `_on_pre_llm_call_v5` (L731-755) | bg_review 检测后直接写 Fct/Hdl 列（代码生成摘要，不同步走 F-stage） | 后台轮 E-stage 一步到位 |

### `ca/__init__.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| `process_turn_f_stage` (L257-265) | 开头检查 turn 的 seq=0 是否已有 Fct → 有则跳过 F-stage | 防止 bg_review 起空线程 |

## 未修改但保留兼容

- `_run_f_stage` 中的 `bg_review` 分支：保留。旧版 CA 升级后，尚未走过新 E-stage 的 bg_review 轮次仍可正常走 F-stage 补摘要。

## 测试结果

**242 passed, 17 skipped** — 与实装前完全一致，无回归。

## 增量缓存路径触发条件

- 话题未切换（`detect()==False`）
- `_A_stable_cache is not None`（冷启动或切换后已重建过一次）
- `_A_cache_is_stale == False`（无 Fct pending）
