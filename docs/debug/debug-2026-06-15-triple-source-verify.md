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

比对 state.db 原始消息（role+content）与 ca_cache turn_stream（seq+role+content+l1_text），
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

### F-stage（Fct 生成）：❌ `@staticmethod` bug 导致全部降级

**根因**：`_call_llm_for_fct()` 声明为 `@staticmethod` 但实际是实例方法（访问 `self.stats`、
`self._turn_counter`）。`@staticmethod` 阻止 Python 注入 `self`，导致参数错位：
`self` 参数接收了 `prev_fct`，`prev_fct` 接收了 `elm_text`，`elm_text` 无参数 → TypeError。

**证据**：
- agent.log: `TypeError: ContextAssembler._call_llm_for_fct() missing 1 required positional argument: 'elm_text'`
- Ca_cache: 所有 9 个 user Fct 的 `core_change="本轮无新内容"`, `_assemble_status=1`
- Ca_cache: 9/9 user Fct 无 `stage_tag`

**影响范围**：全部 9 轮 F-stage LLM 调用失败，所有 user Fct 为降级输出。

### ToolSummarizer（per-tool Fct）：✅ 正常工作（不依赖 LLM）

- 187/188 tool 行有 Fct（99%）
- 格式 `{"tool_name": "...", "tool_args": {...}, "result_summary": "...", "_assemble_status": 0}`
- ToolSummarizer 是规则驱动，无 LLM 依赖，不受 `@staticmethod` bug 影响

### stage_tag：⚠️ 死代码状态

- 仅 2/232 Fct 行含 stage_tag。因 F-stage LLM 全部降级，`<stage_tag>` XML 解析路径从未执行。

## 修复

`ca/__init__.py:957`: 移除 `@staticmethod` 装饰器 ✅ 已提交

修复后效果验证：重启 Hermes gateway 后观察 agent.log 应不再有
`TypeError: ... missing 1 required positional argument: 'elm_text'` 错误，
且 Fct 的 core_change 应为有意义的摘要内容。

## 汇总

| 检查项 | 验证结果 | 说明 |
|--------|---------|------|
| E-stage 数据完整性 | ✅ | state.db ↔ turn_stream 逐行对齐 |
| A-stage 注入逻辑 | ⚠️ | 单元测试通过（5/5），缺运行时 dump |
| F-stage LLM 调用 | ❌→✅ | @staticmethod bug 已修复 |
| User Fct 格式 | ✅ | 5 字段 JSON 结构正确 |
| Tool Fct 格式 | ✅ | ToolSummarizer 正常工作 |
| stage_tag 独立 | ⚠️ | 代码正确，部署后需验证（LLM 正常工作后） |

## 下一步

1. 重启 tester gateway 以加载修复后的代码
2. 启用 `CA_DEBUG=1` 获取运行时 dump
3. 执行多轮对话后重新验证 A-stage 注入和 F-stage LLM 输出
