# 决策 45：多事务 OODA 记录单一化（v3 契约，去掉【已完成】等状态标签）

> 版本：v1.0｜状态：已实装（2026-08-17 后续会话）
> 来源：用户 2026-08-17 后裁定——多事务模式下不再存在【已完成】等标签：
> 事务必然处于 OODA 某一环节，记录只需要「事务 hdl + OODA 阶段关键字 → 该阶段变更」。
> 依赖：决策 44 R5.3/R5.5（v2 多事务 JSON）与 handoff §4.1（单一数据源方向）。

## 1. 问题

决策 44 的 v2 多事务记录是双轨：

```json
{"affairs":[{
  "hdl":"连接池扩容","turns":[7],
  "ooda":{"现象与问题":["连接池耗尽"],...,"后续行动":["观察慢查询"]},
  "changes":[{"stage_tag":"已实施","core_change":"连接池扩容到200"}]
}]}
```

`ooda` 四段与 `changes` 是同一批事实的两份表达，`stage_tag`（【已实施】【计划】【探讨】【已取消】）是旧单事务模型的状态标签；多事务下每个事务总处于 OODA 的某一环节，标签维度冗余且与阶段信息重复。

## 2. 裁定

- 多事务记录收敛为单数据源：`affairs[]`，每事务只有 `hdl / turns / ooda`。
- `ooda` 四键的数组项**就是该阶段的变更记录**（change1；change2……），不再输出 `changes`，不再有 `stage_tag`/【已完成】等标签。
- 时态由阶段语义承载（已执行的决策用完成时、后续行动用将来时、取消用完成时否定），不再靠标签。
- 旧库 v2 记录只读兼容：解析保留其 `changes`，strand 输入仅在 v2 记录上做旧版兼容展示；新写入一律 v3。

### v3 记录契约

```json
{"affairs":[{
  "hdl":"连接池扩容",
  "turns":[7],
  "ooda":{
    "现象与问题":["连接池耗尽"],
    "背景与约束":["连接池上限100"],
    "决策与方案":["连接池扩容到200"],
    "后续行动":["观察慢查询"]
  }
}]}
```

`_fct_format = "v3-multi-affair-ooda"`（v2 = `"v2-multi-affair"` 只读）。

## 3. 实施

| 文件 | 改动 |
|---|---|
| `ca/prompts.py` | `FCT_GENERATION_PROMPT_MULTI_AFFAIR` 重写：只输出 hdl/turns/ooda；显式禁止 changes 与状态标签；示例更新 |
| `ca/fct_multi_affair.py` | `parse_fct_multi_affair`：ooda 四键为唯一内容源，v2 changes 仅兼容保留；`flatten_affairs_to_legacy`：v3 从 OODA 阶段项派生 legacy changes（`{core_change, ooda}`，无 stage_tag），v2 原样保留；`_fct_format` → v3 |
| `ca/topic_summary.py` | strand 输入渲染：v3 只输出「事务编号清单 + 事务 hdl + 各 OODA 阶段项」；v2 才输出 `### changes（旧版兼容）`；prompt 明示阶段项即变更、勿发明状态标签 |
| `ca/store.py` | `format_previous_summary_for_prompt`：多事务 Fct 历史摘要按事务 hdl + 阶段记录渲染（无标签）；空历史占位符不再点名旧 stage_tag/core_change 格式 |
| `ca/f_stage.py` | `_extract_hdl` 首选首事务 hdl；`_format_fct_for_display`/`_is_valid_fct` 支持纯 affairs Fct；display 对无 stage_tag 的派生 changes 不渲染空【】 |
| `ca/a_stage.py` | `_format_fct_readable`：多事务 Fct 按事务 hdl + OODA 阶段渲染（无状态标签） |

未做（仍属 handoff §4.1 后续）：删除 Fct 中 legacy 扁平字段本身——v3 写入仍带派生扁平字段，作为旧消费者/embedding/4B 失败兜底的过渡垫层；下次会话完成全链路只读 affairs。

## 4. 测试

- `tests/unit/test_fct_multi_affair.py`：v3 解析（无 changes）、flatten 派生无 stage_tag、v2 只读兼容。
- `tests/unit/test_strand_affair_input.py`：v3 编号清单渲染无状态标签/无 changes 小节；v2 兼容展示。
- `tests/stage/test_f_stage_v7_frames.py`：v3 落库断言（affairs 无 changes、legacy 派生无标签、`_fct_format=v3`）。
- `tests/stage/test_f_stage.py`：`_extract_hdl` 事务 hdl 优先、display 空【】防护、`_is_valid_fct` affairs 判定。
- `tests/store/test_store_contract.py`：v3 历史摘要按 hdl+阶段渲染；空历史占位符无旧标签字样。

全量：`python -m pytest -q -c tests/pytest.ini tests` → 1153 passed / 1 skipped / 1 xfailed。
