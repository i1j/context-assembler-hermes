---
title: bg_review 同步写入
slug: bg-review-sync-write
category: architecture
version_introduced: v5.5
status: 已实装
decisions: ["bg-review-sync", "plugin-responsibility"]
depends_on: ["storage-model", "e-stage-write-protocol", "f-stage-async-summary"]
updated: 2026-06-23
source_files: ["ca/lstage.py", "ca/e_stage.py"]
---

## 问题

bg_review（后台审查轮）是 Hermes 在 skill 执行后发起的对话轮，用于确认操作结果。这类轮次的特点是：
- 不需要 A-stage 上下文装配（与当前对话上下文无关）
- 内容已在 skill 执行中生成，不需要 LLM 摘要
- 如果不写 Fct 列，下游（topic_manager 等）会出现数据空洞

## 决策

### 备选方案

1. **bg_review 走完整 A-stage + LLM 摘要** — 浪费时间和 token
2. **bg_review 跳过写入 Fct** — 数据空洞
3. **bg_review 检测 + 同步写 Fct（选定）** — 不走 LLM，从已有数据直接填充

### 选定方案

```python
def process_turn_f_stage(self, turn_index):
    _bg_review = (get_current_write_origin() == "background_review")
    if _bg_review:
        # bg_review 轮：从 DB 读 user Elm，同步写代码级 Fct
        user_elm = ...  # 从 turn_stream 读取
        cleaned = {
            "changes": [{"stage_tag": "已实施", "core_change": user_elm[:80]}],
            "core_change": user_elm[:80],
            "_assemble_status": 0,
        }
        self._update_fct_v5(self._session_id, turn_index,
                           json.dumps(cleaned, ensure_ascii=False), user_elm[:80])
        return turn_index  # 跳过 F-stage LLM 路径
```

**实现要点**：
- **检测方式**：`get_current_write_origin()` 返回 `'background_review'` 时判定为 bg_review
- **处理位置**：`process_turn_f_stage` 中路由决策，而非 `_on_pre_llm_call_v5`
- **跳过 A-stage 组装**：不执行角色队列匹配和 grade 替换
- **同步写 Fct**：从 user Elm 提取 80 字符摘要填入 Fct 列
- **不调用 LLM**：不走 F-stage 的异步 LLM 摘要调用

## 数据验证

```sql
-- 检查 bg_review 行的 Fct 是否等于 content
SELECT turn, content, Fct,
       CASE WHEN Fct = content THEN '同步写入' ELSE '异常' END AS status
FROM turn_stream
WHERE biz_category = 'bg_review'
ORDER BY turn;
```

## 优点

- 节省 LLM 调用：bg_review 内容无需摘要
- 消除数据空洞：Fct 列始终有值
- 逻辑简单：跳过装配路径，Fct 直接等值填充

## 约束 / 已知问题

- bg_review 检测依赖 Hermes 的 `get_current_write_origin()`，该函数在不同版本中可能不稳定
- bg_review 路径现在有独立测试覆盖（`TestBgReview.test_bg_review_writes_fct_equals_content`）
- bg_review 路径现在有独立测试覆盖（`TestBgReview.test_bg_review_writes_fct_equals_content`）
- bg_review 的 Fct 值实际是原文摘要——消费方需知晓此语义差异
