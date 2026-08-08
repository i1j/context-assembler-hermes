
> **⚠️ 历史存档 — v5.1 debug 验证报告。`_mutation_mode()` 已在 v5.10 移除，改用 `_simple_mutation_mode_v5`。**

# 调试报告：v5.1 CA 出口记录验证 — bg_review + 20K 三区行为确认

## 调试会话

- 日期：2026-06-12 02:30~02:45
- Session: `20260612_023032_ac64f4`
- Model: deepseek-v4-flash (Hermes CLI tester profile)
- 插件: CA Assembler (v5.1, commit 5112bfc)
- Env: `CA_DEBUG=1` + `CA_DEBUG_DUMP` 无指定（auto fallback to /tmp/）

## 验证内容

### 1. CA 出口 debug dump 记录

/tmp/ 下生成 8 份 `ca_mutation_*.json` dump，每份对应一次 LLM call 的
`pre_llm_call` → `_mutation_mode()` 出口（mutated conversation_history）。

文件按时间线递次增长：50→52→61→71→75→109 msg（最新）。

### 2. v5.1 四项关键修改验证

| 修改 | 代码位置 | 状态 |
|------|----------|------|
| bg_review A-stage gate | `__init__.py:95,120` | ✓ 导入 + 跳过 |
| mutation: `del`→`content=" "` | `__init__.py:513` | ✓ tool 行清空 |
| post_llm_call 就地恢复 | `__init__.py:546-572` | ✓ snapshot 映射 |
| ca/engine bypass 过滤 | `ca/__init__.py:1055-1070` | ✓ bg_review 排除 |

### 3. 20K 三区行为确认

当前会话 ~500-1000 dialogue tokens，全部在 Zone ②（≤20K）：

```
Zone ② 行为: 对话=Elm(原文)  工具=Fct(清空)
实际 dump:    ✓ 7 user 原文  ✓ 57 tool content=" "
```

Zone ③ Fct 摘要替换未触发（正确）。

### 4. bg_review 运行状态

**CA cache:**
- turn_cache `biz_category="bg_review"`: 6 轮 (turns 5,9,11,14,15,17)
- turn_plan: 无 bg_review 轮（正确排除）
- bg_review user msg 6.4K, tool output 最大 82K chars

**LLM 可见性:**
- bg_review 41 条消息全部 `content=" "`（清空）
- LLM 看不到 bg_review 内容
- 结构开销：~328 tok (user/asst) + tool_call_id/tool_name 结构仍保留

**已知取舍：索引对齐 > token 节省**

### 5. cron mode: deny 影响

- `config.yaml:391` → `cron_mode: deny` 阻止 cron job 创建
- 但 bg_review 不依赖 cron job（通过独立 `hermes bg_review` 或自动触发运行）
- 本会话无 cron job 注册，不影响 bg_review 功能

## 待后续跟踪

- Zone ③ Fct 摘要替换：需对话积累 >20K dialogue token 后抓 dump 验证
- 空骨架行 token 浪费：若后续索引方案改为 `_turn_index` 寻址，可删除空行
