# 决策 45：多事务 OODA 记录单一化（v3 契约，去掉【已完成】等状态标签）

> 版本：v1.1（v1.0 定记录契约；v1.1 完成单一数据源全链路）｜状态：已实装
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

### 3.1 v1.0（记录契约）

| 文件 | 改动 |
|---|---|
| `ca/prompts.py` | `FCT_GENERATION_PROMPT_MULTI_AFFAIR` 重写：只输出 hdl/turns/ooda；显式禁止 changes 与状态标签；示例更新 |
| `ca/fct_multi_affair.py` | `parse_fct_multi_affair`：ooda 四键为唯一内容源，v2 changes 仅兼容保留；`_fct_format` → v3 |
| `ca/topic_summary.py` | strand 输入渲染：v3 只输出「事务编号清单 + 事务 hdl + 各 OODA 阶段项」；v2 才输出 `### changes（旧版兼容）`；prompt 明示阶段项即变更、勿发明状态标签 |
| `ca/store.py` | `format_previous_summary_for_prompt`：多事务 Fct 历史摘要按事务 hdl + 阶段记录渲染（无标签）；空历史占位符不再点名旧 stage_tag/core_change 格式 |
| `ca/f_stage.py` | `_extract_hdl` 首选首事务 hdl；`_format_fct_for_display`/`_is_valid_fct` 支持纯 affairs Fct |
| `ca/a_stage.py` | `_format_fct_readable`：多事务 Fct 按事务 hdl + OODA 阶段渲染（无状态标签） |

### 3.2 v1.1（单一数据源全链路，handoff §4.1 收尾）

- **Fct 落库只写 `affairs + _assemble_status + _fct_format`**：
  `flatten_affairs_to_legacy` 删除，改 `build_fct_multi_affair`；
  F-stage 正常/截断 partial 路径均不再写 legacy 扁平字段。
- **读路径现场派生旧消费者视图**：`ca/store.py::collect_turn_fcts` 新增
  `_entry_from_affairs`——从 affairs[].ooda 派生 changes/tags/ooda_tags/
  todos/consensus/key_facts_supp/new_materials；旧单事务 Fct 走
  `_entry_from_legacy_fct`（行为不变）。
- **topic 4B 失败兜底直接用 affairs 聚合 strands**：
  `ca/topic_summary.py::_fallback_strands_from_affairs`——同名事务跨轮合并
  （hdl 相同 → 同 strand，turns 并集、OODA 四段去重 concat）；
  `_assemble_summary` 在 4B 失败或无 strands 时优先该路径。
- **`_compute_status` 阶段化**：legacy `评估中` 标签之外，任一事务
  `后续行动` 非空 → `active`（事务仍在 OODA 循环中）。
- **embedding 语义提取修正**：`topic_manager._extract_fct_semantic_text`
  对 `affairs[]` 上下文内的 `ooda` 值放行（那是内容），legacy changes
  里的 `ooda` 阶段标签继续作为元数据跳过。

## 4. 测试

- `tests/unit/test_fct_multi_affair.py`：v3 解析 + `build_fct_multi_affair`
  单一数据源落库（无 legacy 键）+ v2 只读兼容。
- `tests/unit/test_strand_affair_input.py`：v3 Fct 经 `collect_turn_fcts`
  现场派生旧视图（changes/ooda_tags/todos/补充四段）；hdl 兜底；
  v2 标签保留；编号清单渲染无状态标签。
- `tests/stage/test_f_stage_v7_frames.py`：落库 Fct 键集合 =
  `{affairs, _assemble_status, _fct_format}`。
- `tests/unit/test_topic_summary.py`：`_fallback_strands_from_affairs`
  同名跨轮合并；4B 失败时 summarize 输出多事务 strands；
  `_compute_status` 按后续行动判 active。
- `tests/unit/test_topic_manager.py`：v3 affairs 的 ooda 值进入语义文本；
  v2 changes 的 ooda 标签/状态值仍被剥除。
- `tests/store/test_store_contract.py`：v3 历史摘要按 hdl+阶段渲染。
- `tests/stage/test_f_stage.py`：hdl/display/validity 对纯 affairs Fct 的契约。

全量：`python -m pytest -q -c tests/pytest.ini tests` → 1161 passed / 1 skipped / 1 xfailed。
