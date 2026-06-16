# 云端 LLM 推理过程（reasoning）中的用户意图提取调查

## 发现日期
2026-06-15

## 背景
在 Hermes CLI 中，云端大模型（当前 deepseek-v4-flash via deepseek provider）在回复前会先输出一段推理/思考过程（reasoning）。这段文本通常包含对用户提问的归纳，格式为 numbered list（`1. ... 2. ... 3. ...`）。

## 字段位置

| 字段 | 含义 | 来源模型 |
|------|------|---------|
| `assistant_message.reasoning` | 直接推理文本 | DeepSeek, Qwen 等 |
| `assistant_message.reasoning_content` | 推理文本别名 | Moonshot AI, Novita 等 |
| `assistant_message.reasoning_details` | OpenRouter 统一格式数组 | OpenRouter（多供应商） |

代码位置：`hermes-agent/agent/agent_runtime_helpers.py:991`，函数 `extract_reasoning()`

## 当前 CA 插件状态

**未落盘。** 当前 `_on_api_response_v5`（`ca/__init__.py:414`）只取了：
```python
thought = getattr(assistant_message, "content", "") or ""
```
没有取 `.reasoning`。

## 用户 Fct 提取方案（探索阶段）

### 思路
在 E-stage `_on_post_api_request_v5` 中捕获 `assistant_message.reasoning`，从其中提取 numbered list（`1. ... 2. ... 3. ...`）作为用户 Fct，写入 turn_stream (turn, seq=0) 的 Fct。

### reasoning 典型结构
```
用户的请求涉及三个方面：
1. 列出 /tmp 目录
2. 过滤 .py 文件
3. 显示文件详情

Let me think about how to implement this...
```
前段 = 用户意图归纳（numbered list）
``` 之后 = 解题思路

### 仅处理单个点的情况
如果 reasoning 只有一个编号项，直接取该项内容。

### 当前认为不必要的理由
A-stage `_simple_mutation_mode_v5` 对 user 行执行 `continue`（跳过替换），提取出的用户 Fct 无消费端。需要 etc.：
1. 开放 A-stage 对 user 行的替换规则
2. 或改变当前 "user继承elm不变" 的设计约定

## 截断规则发现（同批）

当前 `ToolSummarizer.generate_group_summary()`（`ca/tool_summarizer.py:892`）的 thought Fct 截断逻辑有 bug：

```python
# 当前：找到第一个句尾且位置 ≤ 90 就返回
for sep in ("\n\n", "。", "！", "？", ".", "!", "?"):
    cut = text.find(sep)
    if cut != -1 and cut <= 90:
        return text[:cut + len(sep)]
```

**问题**：只取了第一句，即使它仅几个字。
**正确应为**：按句子累加直到超过 100 字，在该句尾处截断，允许结果 > 100 字。
