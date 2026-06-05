# CA pre_llm_call: 增强而非压缩

## 核心发现

CA 插件的 `pre_llm_call` hook 返回 `Optional[str]`，Hermes 将其**注入到 user message 中**（追加文本到当前轮的 user 消息）。

**这不是真正的上下文压缩**——它不替换、不删除对话历史中的任何消息。原始消息以完整内容发送给 LLM，额外追加的摘要文本只是 prompt 的附加信息。

## 实际效果

| 方面 | 效果 |
|------|------|
| Token 用量 | 不减少。原始历史 + 摘要文本两套内容并存 |
| Cache 可用性 | 不变。原始历史的 cache prefix 仍在 |
| 信息密度 | 显著提升。结构化 JSON 摘要（core_change / new_materials / objective_facts / consensus / todo）提供清晰的对话发展脉络 |
| 上下文窗口压力 | 增加。摘要文本额外消费窗口预算 |

## C-stage 异步时序

C-stage（摘要生成）通过 `process_turn_async` 异步执行。`post_llm_call` 触发后启动 C-stage 线程，持续 ~9-17s。

**关键时序**：连续轮次中，第 N+1 轮的 pre_llm_call 执行时，第 N 轮的 C-stage 可能尚未完成。

实测时序：
```
11:22:05.149  post_llm_call turn 1 → process_turn_async
11:22:05.796  pre_llm_call turn 2 ← C-stage turn 1 尚未完成！
11:22:22.275  C-stage FINISH turn 1 (17.1s 后)
```

## 轮次注入演化（会话初期的实际模式）

| 轮 | pre_llm_call 注入内容 | 原因 |
|----|----------------------|------|
| 第 1 轮 | 无 `[~/N]` | 尚无任何 C-stage 完成 |
| 第 2 轮 | ❌ 通常无 | C-stage turn 1 未完成 |
| 第 3 轮 | ✅ turn 1 摘要 + turn 3 摘要 | 前 2 轮 C-stage 已完成 |
| 第 4 轮 | ✅ **密集注入**：turn 1/3/4 + 工具轮 | 累计摘要已就绪 |

⚠️ **不要因为前 2 轮无注入就判定 CA 失效**。第 3 轮起注入会密集生效，且后续轮次注入量持续增长。已在第 4 轮单次注入 6 条 `[~/N]`（含对话摘要 + 工具子轮摘要）。

## CA 的实际价值

在当前插件架构下，CA 的实际运行时贡献：
1. **结构化脉络注入**：LLM 在 user message 前缀看到 `[~/N]` 标记的摘要，包含 core_change / new_materials / objective_facts / consensus / todo 字段。虽然不压缩 token，但提供了**比逐条阅读历史更快的信息提取路径**
2. **增量追踪**：`core_change` 字段准确反映每轮的关键变化，帮助 LLM 理解对话是如何逐步演进的
3. **跨会话恢复**：ca_cache DB 中的结构化摘要数据为 session resume 提供历史快照
4. **工具轮摘要**：工具子轮也生成 L1 摘要，标注 tool_name / tool_args / result_summary / error

## 诊断验证方法

确认 pre_llm_call 是否注入了内容：
1. 查 agent.log 的 `_on_pre_llm_call` 条目（确认被调用）
2. 观察 user message 前缀是否出现 `[~/N]` 标记（直接可见）
3. 查 ca_cache DB 的 `turn_cache` 表确认 C-stage 摘要已写入
4. 比较各轮 input token 差异：第 3+ 轮的 input 会因追加摘要而略高于基线
5. 若 pre_llm_call 返回 None（无 `[~/N]` 消息可提取），则输入无变化

## 实测数据（session 20260605_110355_c7b752）

| 轮 | 用户消息 | input tokens | 注入 [~/N] 数 |
|----|---------|-------------|--------------|
| 1 | 检查一下当前CA插件的情况 | 23,549 | 0 |
| 2 | Review the conversation (skill update) | 74,247 | 0 |
| 3 | 检查刚才对话轮次的压缩情况 | 73,297 | ？— 日志无记录 |
| 4 | 上一轮呢 | — | 6（含工具轮） |

C-stage 完成情况：
- Turn 1: FINISH at +17.1s, dialogue_ok=True
- Turn 2: FINISH at +9.0s, dialogue_ok=True

ca_cache DB 摘要量：
- turn_cache: 54 rows
- Turn 1 L1: 268 chars JSON
- Turn 2 L1: 47 chars "无有效增量"
- 各 turn 各有 26 个工具子轮 L1 摘要
