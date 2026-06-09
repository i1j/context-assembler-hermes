# CA 调试报告：工具组内存缓存缺失 + L-stage 迁移

## 时间：2026-06-09

## 现象

当前会话的 CA 注入 ctx 中，工具组条目完全缺失。DB 存有 53 行 `tool_calls_json` 非空（含 131 条工具缓存条目），但 `_compute_turn_plan_v2` 读不到任何工具组，turn_plan 中 0 条工具组条目。

ctx 结构（仅对话轮摘要，无 `[~/N/M]`）：

```
[~/1/0] L0 — 截断在代码引用中间
[~/2/0] L1 — 多行格式化
[~/3/0] L2 — 系统后台审查（thought 原文透传）
[~/4/0] L0
[~/6/0] L1
[~/8/0] L0
[~/9/0] L1
[~/10/0] L1
```

## 根因

### 根因 1：flush_tool_buffer() 写 DB 后未更新 cache

```
session start → CacheBuilder.build()  → DB 空 → cache 空
对话进行中  → flush_tool_buffer()  → store.write_turn() ✓
                                    ↛ cache.add_tool_group()  ✗   ← 缺失
下一轮 assemble() → cache.get_tool_group_snapshot_data() → 返回空 {}
                  → _compute_turn_plan_v2() 读 0 工具组
                  → ctx 无 [~/N/M]
```

`cache.py` 有 `add_turn()`（对话轮）和 `add_tool_turn()`（旧 per-tool），但缺 `add_tool_group()` 方法。`flush_tool_buffer()` 写完 DB 后只更新了 DB，没更新内存 cache，A-stage 读到的永远是 session start 时构建的 cache 快照（当时无数据）。

### 根因 2：L-stage 补全在 v5 schema 下不工作（预存问题，不因本次恶化）

`get_pending_backfill()` 的 v5 路径（store.py L726）将 `l2_text` 字段映射为 `content` 列（单条消息纯文本），而非全长对话 JSON 数组。这导致：

- `_backfill_dialogue` 传给 `_call_llm_for_l1` 的是单条消息而非全长对话
- `_backfill_tool_group` 的 `json.loads(l2_text)` 在纯文本上必然失败返回

在 v4 只读旧库路径下正确工作。v5 schema 迁移后的预存缺口。

## 修复

### 文件 a: ca/cache.py

新增 `add_tool_group()` 方法，与 `add_turn()` 平行：

```python
def add_tool_group(self, turn_index, api_call_count, l0_text, l1_text):
    gkey = (turn_index, api_call_count)
    with self._lock:
        self.tool_group_l0_texts[gkey] = l0_text
        self.tool_group_l1_texts[gkey] = l1_text
        self._dirty = True
    self._submit_rebuild()
```

使用 `(turn_index, api_call_count)` tuple key，支持一轮多工具组。

删除 `add_tool_turn()` 方法（0 调用方残留）。

### 文件 b: ca/__init__.py

`flush_tool_buffer()` 中写 DB 后同步更新 cache：

```python
# ⑤ assistant{tc} 行
self.store.write_turn(...)

# ⑥ 同步更新内存缓存
self.cache.add_tool_group(
    turn_idx, buf.api_call_count,
    group_l0, json.dumps(group_summary, ensure_ascii=False),
)
```

### 文件 c: ca/lstage.py

L-stage 工具补全从 per-tool 完全迁移到 tool_group：

- 新增 `_backfill_tool_group()` — 提取工具调用 → 逐工具 summarize → 拼组摘要 → 写 tool 行 + assistant{tc} 行 → add_tool_group 更新 cache
- `_backfill_tool()` — 简化为直接 delegate 到 `_backfill_tool_group()`（legacy 兼容）
- `_update_record()` — 移除 tool 分支，仅处理 dialogue
- 删除 `_backfill_dialogue` 中原 per-tool 写入循环（约 25 行）
- bugfix: thought 提取从 `msgs[0]`（user 消息）改为遍历找 assistant{tc} 消息

### 文件 d: tests/test_v440.py

3 处 `add_tool_turn()` → `add_tool_group()` 替换。

## 自查发现的 bug

| Bug | 影响 | 修复 |
|-----|------|------|
| `_backfill_tool_group` thought 取自 `msgs[0]`（user 消息） | group_intent 从用户问题而非 assistant thought 提取，意图错误 | 改为遍历找 assistant{tc} 消息 |
| `_backfill_tool` 重复调 `_extract_tool_calls` | 浪费一次 JSON 解析 + 遍历 | 直接 delegate |
| `add_tool_turn` 删除前未确认无调用方 | 编译错误 | 已确认 0 调用方后删除 |

## 当前 ctx 附加问题（本次未修）

| 问题 | 说明 |
|------|------|
| L0 截断在语法边界 | 纯字符截断切在代码引用中间，LLM 看到残缺语法片段 |
| 系统后台审查注入 L2 | 3 轮"系统后台审查"的 thought 原文注入为 L2，消耗 token 无实质信息 |
| L1 new_materials/objective_facts 边界模糊 | 同源信息在两个字段中重复（如 changelog 日期错误同时在两字段出现） |
| L0 嵌入全部为 0 | 向量检索退化 BM25-only |

## 测试验证

284 passed, 13 skipped, 0 failed（全活跃测试套件，与基线一致）

## 生效条件

重启 Hermes tester profile 进程后生效：

- **新会话**：`flush_tool_buffer` → `add_tool_group` 保持 cache 同步，工具组正确注入
- **旧会话**：session start 时 `CacheBuilder.build()` 从 DB 重建 cache，已有工具组数据被自动加载
