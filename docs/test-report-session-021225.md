# CA v4.4.0 完整诊断报告 — session 021225

> 生成：2026-06-05，基于 ca_cache `20260605_021225_efd58c.db`（30 轮，797 条记录）
> 对应问题调试记录：`docs/debug-*.md`（3 份）

## 执行概况

| 指标 | 数值 |
|------|------|
| 会话时长 | ~40 分钟 |
| 对话轮次 | 30 轮 |
| CA cache 记录 | 797 条（含对话轮 + 工具子轮） |
| C-stage 成功率 | 100% — 全部 `_assemble_status=0` |
| 有效摘要轮 | 20 轮（t1-t20 有内容，t21-t30 无有效增量） |
| 工具子轮摘要 | 全覆盖 — 每轮 26-35 条 tool L1 摘要 |

## 发现 & 修复对照

| # | 问题 | 根因 | 修复方式 | 调试记录 |
|---|------|------|---------|---------|
| 1 | t3 系统技能库 prompt 被 CA 降级为"无有效增量" | OODA parser 缺系统消息分类 | 修改区分用户触发 vs 系统触发 | `debug-t3-summary-downgrade.md` |
| 2 | CA 预算严重不足，16K 下只能压缩最早两轮 | `context_length` 默认 32K，实际 91.5K | 加模型查找表 + 修 hook 传参 | `debug-context-length-mismatch.md` |
| 3 | A-stage head/middle/tail 分层失效 | `_rebuild_messages_from_cache()` 不设 `_turn_index` | 注入 `_turn_index` 到重建消息 | 同上 |
| 4 | C-stage executor shutdown 竞态 | `on_session_end` 每轮触发 → 5s wait < 10s embedding | `_on_session_end` → no-op | `debug-cstage-executor-shutdown.md` |

## 会话摘要时间线

| 轮 | core_change | 类型 |
|----|------------|------|
| t1 | 读取CA插件目录下agents.md文件 | 初始化 |
| t2 | 第一轮数据完整落地运行正常 | 验证 |
| t3 | ❌ 无有效增量（误判） | **Bug** |
| t4 | 第二轮完整落库17条subturn | 继续验证 |
| t5-t6 | 交互式逐轮探查 | 排查 |
| t7-t8 | 识别系统消息与CA无关 → 误判确认 | **诊断** |
| t9-t10 | 内存仅存索引指针，记录路径更正 | 路径约定 |
| t11 | 用户偏好嵌入技能体 | 技能更新 |
| t12 | **CA仅摘要最早两轮内容** | **关键发现** |
| t13 | 记录存放约定已嵌入技能库 | 约定固化 |
| t14-t15 | 全部轮次生成且完整无异常 | 基线确认 |
| t16 | **ctx仅压缩t1摘要** → 压缩几乎不生效 | **严重发现** |
| t17 | **根因在hook传参缺失** | **根因定位** |
| t18-t19 | 补传context_length根因已明 | 根因确认 |
| t20 | 管线运行稳定 无异常 | 修复确认 |
| t21-t30 | 无有效增量 | 稳定运行 |

## C-stage 性能

| 指标 | 数值 |
|------|------|
| C-stage 单轮耗时 | ~9-17s |
| 嵌入后端 | ollama qwen3-embedding:0.6b on :11439 |
| LLM 摘要模型 | qwen3-4b-instruct on :11440 |
| C-stage 成功率 | 30/30 (100%) |

## 核心结论

1. **C-stage 写入层稳定**：DB 写入（turn_cache）始终健康，未丢失数据
2. **A-stage 组装层两处断裂**：`_turn_index` 缺失 + `context_length` 未传 → 压缩策略几乎不生效
3. **修复已落地**：入 git（commit `2420d51`），含 3 个根因修复 + 15 个 INFO 级别诊断日志
4. **CA 是增强不是压缩**：`pre_llm_call` 仅追加摘要文本，不替换历史——token 无节省，但信息密度提升

## 关联文件

| 文件 | 位置 |
|------|------|
| 调试记录 1 | `docs/debug-t3-summary-downgrade.md` |
| 调试记录 2 | `docs/debug-context-length-mismatch.md` |
| 调试记录 3 | `docs/debug-cstage-executor-shutdown.md` |
| pre_llm_call 行为分析 | `docs/ca-pre-llm-call-augmentation-pattern.md` |
| 原始 CA 数据 | `ca_cache/20260605_021225_efd58c.db` |
