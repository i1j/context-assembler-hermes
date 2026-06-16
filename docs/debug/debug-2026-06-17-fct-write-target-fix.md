# Fct 写入位置纠正 + 命名清理调试报告

时间: 2026-06-16
定位: update_fin_fct_v5 / user Fct = Elm / _extract_hdl 重命名

## 触发条件

三源验证时发现助理 fin 行 Fct 覆盖率为 0%（本该是对话摘要存储位），跟踪到 `_update_fct_v5` → `update_seq0_fct_v5` 硬编码写 `seq=0`（user 行）。同时发现 user 行的 Fct 列也缺失。

## 根因链

### Bug 1: Fct 写到 user 行而非 assistant_fin

`_run_f_stage` 调用 `_update_fct_v5` → `store.py:update_seq0_fct_v5`：

```sql
UPDATE turn_stream SET l1_text=?, l0_text=? WHERE session_id=? AND turn=? AND seq=0
```

`seq=0` 是 user 行。设计上 user 行只存 raw Elm（content + Fct=Elm 拷贝），整轮对话摘要 Fct 应存到 assistant_fin 行（最后 seq）。

影响：F-stage 生成的对话摘要覆盖了 user 的 Elm Fct，user 行 Fct 既不是 Elm 也不是摘要——两头不对。

### Bug 2: user Fct 没设 = Elm

`_on_pre_llm_call_v5` 写 user 行时只传 `content=user_message`，没传 `Fct`。设计上 user Fct = Elm（复制 content）。

### Bug 3: read_prev_fct 读错行

读前轮 Fct 供 LLM 做上下文时，`read_prev_fct` 读 `seq=0`。Fct 搬到 fin 行后，这个读取返回了前轮 user 的 raw Elm 而不是对话摘要。

### 连带问题: _extract_l0 命名残留

函数名 `_extract_l0` 用了旧名 L0。按 v5.0 术语该叫 Hdl。调用处 2 个（定义 + 调用点）均更新。

## 改动清单

| # | 文件 | 改动 | 行 |
|---|------|------|:--:|
| 1 | plugin `__init__.py` | `write_turn_v5` 加 `Fct=user_message` | 425 |
| 2 | `ca/store.py` | 新增 `update_fin_fct_v5`, 删除 `update_seq0_fct_v5` | 1396-1410 |
| 3 | `ca/__init__.py` | 调 `update_seq0_fct_v5` → `update_fin_fct_v5` | 546-547 |
| 4 | `ca/__init__.py` | `_extract_l0` → `_extract_hdl`（定义+调用） | 370, 1007 |
| 5 | `ca/store.py` | `read_prev_fct` 改读 fin 行, 不读 seq=0 | 1389-1397 |
| 6 | `tests/test_store.py` | 2 个测试函数重写, 测 fin 行写入 + user 行不变 | 600-622 |

## 验证

```python
# 修复2: update_fin_fct_v5 写 fin 行
write_turn_v5(s, "t", 1, 0, role="user", content="u")
write_turn_v5(s, "t", 1, 1, role="assistant", content="fin")
update_fin_fct_v5(s, "t", 1, fct_text='{"core_change":"new"}', hdl_text="hdl")
assert read_fct_v5(s, "t", 1, 1) == '{"core_change":"new"}'  # fin 行有 Fct
assert read_fct_v5(s, "t", 1, 0) == ""                       # user 行无 Fct

# 修复1: user Fct = Elm
# plugin __init__.py 传 l1_text=user_message → write_turn_v5 写 seq=0 时同时写 l1_text

# 修复3: read_prev_fct 读 fin 行
# 前轮有 assistant_fin 时返回 fin 行的 Fct, 没有时返回 ""
```

## 遗留

- 列名 `Fct`/`Hdl` → `fct`/`hdl` 改名为 Schema migration，需要下次 Hermes 重启时执行 `ALTER TABLE turn_stream RENAME COLUMN`
- 架构文档（CA_v5.0_架构设计.md、test-system-refactoring-v5.0.md）部分引用旧函数名
