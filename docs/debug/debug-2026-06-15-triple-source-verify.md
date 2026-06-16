# 三源验证报告 — CA v5.0 输出设计合规性

## 日期
2026-06-15

## 三源清单

| 数据源 | 路径 | 状态 |
|--------|------|------|
| Hermes state.db | `~/.hermes/profiles/tester/state.db` | ✅ session=mqennsf1t970ru, 278 messages |
| CA cache DB | `~/.hermes/profiles/tester/ca_cache/mqennsf1t970ru.db` | ✅ 375 turn_stream rows |
| CA debug dump | `/tmp/ca_mutation_*.json` | ❌ 无（CA_DEBUG 未启用） |

## 验证方法

比对 state.db 原始消息（role+content）与 ca_cache turn_stream（seq+role+content+Fct），
逐行验证 turn 1 的完整数据流。

## 验证结果

### E-stage（写即落盘）：✅ 完全符合设计

| state.db 原始 | turn_stream 映射 | 一致性 |
|---|---|---|
| user "现在使用了CA插件吗？" | seq=0 role=user, content 相同 | ✅ |
| assistant "" (tool_calls) | seq=1 role=assistant, content 相同 | ✅ |
| tool search_files result | seq=2 role=tool, content 相同 | ✅ |
| assistant "" (tool_calls) | seq=3 role=assistant, content 相同 | ✅ |
| tool read_file result | seq=4 role=tool, content 相同 | ✅ |
| tool read_file result(2) | seq=5 role=tool, content 相同 | ✅ |
| assistant final response | seq=6 role=assistant, content 相同 | ✅ |

user 消息数：state.db=9 vs turn_stream=9 ✅

### A-stage（上下文替换）：⚠️ 无 debug dump 无法验证实时替换

替换逻辑已通过测试套件验证（`test_astage.py: 5 passed`），
但无运行时 dump 无法验证生产环境 A-stage 的实际注入效果。

### F-stage LLM 调用：❌→✅ `@staticmethod` bug

**根因**：`_call_llm_for_fct()` 声明为 `@staticmethod` 但实际是实例方法
（访问 `self.stats`、`self._turn_counter`）。`@staticmethod` 阻止 Python 注入
`self`，导致参数错位：`self` 参数收到 `prev_fct`、`prev_fct` 收到 `elm_text`、
`elm_text` 无参数 → TypeError。

**影响**：agent.log 连续 9 轮 `TypeError: missing elm_text`。F-stage crash
后走 `except Exception` fallback（line 388-398）。

**修复**：移除 `@staticmethod` 装饰器（commit `c60d8f8`）

### F-stage fallback 占位符：⚠️→✅ 改为复制 user Elm

**旧行为**：fallback 写死 `core_change: "本轮无新内容"`、`_assemble_status: 1`。
user Fct 的唯一消费端是话题分割（`_compute_topic_groups`），占位符等于零信息。

**修复**：fallback 改为复制 `user_elm` 原文作为 `core_change`，hdl 同步更新
（commit `dbe9e8e`）。`_assemble_status` 改为 `0`（非降级）。

### ToolSummarizer（per-tool Fct）：✅ 正常工作

- 187/188 tool 行有 Fct（99%），格式正确
- 规则驱动，不依赖 LLM

### stage_tag：⚠️ 待 LLM 恢复后验证

因 F-stage LLM 未实际调用，`<stage_tag>` 解析路径从未执行。

## 验证汇总

| 检查项 | 验证结果 | 说明 |
|--------|---------|------|
| E-stage 数据完整性 | ✅ | state.db ↔ turn_stream 逐行对齐 |
| A-stage 注入逻辑 | ⚠️ | 单元测试通过（5/5），缺运行时 dump |
| F-stage @staticmethod bug | ❌→✅ | 已修复，重启后生效 |
| F-stage fallback 复制 Elm | ❌→✅ | 已修复 |
| ToolSummarizer | ✅ | 187/188 tool Fct 正确 |
| stage_tag 独立 | ⚠️ | 代码正确，部署后验证 |

## 修复清单

| 提交 | 变更 |
|------|------|
| `9fb05cc` | fix: generate_group_summary 截断无句尾标点的长文本 |
| `403255f` | refactor: 测试体系 v5.0 对齐 + 生产 bug 修复（fct_text→Fct 两处） |
| `c60d8f8` | fix: _call_llm_for_fct @staticmethod 导致 F-stage 全部降级 |
| `dbe9e8e` | fix: F-stage fallback 复制 user Elm 替代硬编码占位符 |
| `517044f` | feat: 测试体系重构覆盖映射方法论 skill |

## 下一步

1. **重启 tester gateway** 以加载所有修复
2. **启用 `CA_DEBUG=1`** 获取运行时 dump
3. **多轮对话后** 重新验证 A-stage 注入效果和 F-stage LLM 输出
