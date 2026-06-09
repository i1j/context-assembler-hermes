# 测试技术方案 v1: 工具轮摘要流程重构

## 1. 测试策略

| 维度 | 方案 |
|------|------|
| 类型 | 单元测试 + 集成测试 |
| 级别 | 全量回归 + 新增场景 + 重构验证 |
| 方法 | 自动化（pytest） |
| Mock 策略 | `_mock_embed` (autouse) → `[0.1]*768`；`_mock_llm` (autouse) → `('mock_response', 'stop')`；`post_tool_call` 参数模拟 |

## 2. 测试场景

### 2.1 新增：post_tool_call 钩子注册

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 注册接口 | register() 调用 | 6 个 hooks 注册（新增 post_tool_call） | REQ-1 |
| 无可用引擎 | post_tool_call 无 session_id | 静默跳过，不抛异常 | REQ-1 |
| 引擎 errored | post_tool_call 引擎已故障 | 静默跳过 | REQ-1 |
| 正常调用 | post_tool_call 带回所有参数 | buffer_tool_call 被调用且参数完整 | REQ-1 |

### 2.2 新增：buffer_tool_call 方法

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 单工具单组 | 1 个工具调用 | buffer 中有 1 个条目，api_request_id 正确分组 | REQ-2 |
| 多工具同组 | 同一 api_request_id 3 个工具 | 1 组 × 3 条目 | REQ-2 |
| 多工具异组 | 2 个不同 api_request_id | 2 组各 x N 条目 | REQ-2 |
| 线程安全 | 并发 buffer_tool_call | 无数据竞争，最终计数正确 | REQ-2 |

### 2.3 新增：_extract_current_tool_thought

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 有 thought | 最后一条 assistant 含 tool_calls + content="思考中" | 返回"思考中" | REQ-3 |
| 无 thought | assistant 含 tool_calls, content="" | 返回空字符串 | REQ-3 |
| 无 tool_calls | 纯对话历史 | 返回空字符串 | REQ-3 |
| 混排 | tool_calls 在中间位置 | 只取最后一条 assistant.tool_calls | REQ-3 |
| 空列表 | conversation_history=[] | 返回空字符串 | REQ-3 |

### 2.4 新增：flush_tool_buffer

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 空 buffer | buffer 为空 | 返回 0，不写 DB | REQ-4 |
| 单组单工具 | 1 组 1 工具 | write_turn 写入 1 条 tool 记录 | REQ-4 |
| 单组多工具 | 1 组 3 工具 | write_turn 写入 3 条 tool 记录 | REQ-4 |
| 多组多工具 | 2 组各 2 工具 | 4 条写入，分组正确 | REQ-4 |
| thought 注入 | thought="我在查" | L2 中 assistant.content 正确 | REQ-4 |
| L2 格式验证 | 重建的 L2 | 符合 OpenAI 标准格式 | REQ-4 |
| L1/L0 依赖 | flush 后 | ToolSummarizer 引用 L2 args（dict） | REQ-4 |
| summarize 异常 | ToolSummarizer 抛出 | 单条失败不影响组内其他工具 | REQ-4 |

### 2.5 修改：_run_c_stage 删除 messages 分支

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 无工具轮 | 纯对话 | 正常写入 dialogue 记录 | REQ-5 |
| 工具轮已 flush | 工具数据已由 flush 写入 | _run_c_stage 不写工具轮（无 if messages:） | REQ-5 |
| L-stage backfill | 从 L2 提取 | 不变，仍调用 _extract_tool_calls | REQ-5 |

### 2.6 修改：process_turn_async 签名

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 无 messages | process_turn_async(um, ar, history) | 正常运行，turn_index 递增 | REQ-6 |
| 旧签名叫用 | messages=xxx 参数 | TypeError（旧调用必须修复） | REQ-6 |

### 2.7 集成：端到端零重复验证

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 3 轮各 2 工具 | post_tool_call ×6 + post_llm_call ×3 | DB 中工具记录 = 6（非 20×） | REQ-7 |
| 无重复 | 模拟生产流水 | 每工具只写出 1 行 turn_cache | REQ-7 |

### 2.8 回归：现有 A-stage 功能不受影响

| 场景 | 输入 | 预期结果 | 关联需求 |
|------|------|---------|---------|
| 对话轮 A-stage | 现有 test_a.py 全部用例 | 全部通过 | REG-1 |
| 工具轮 A-stage | TestToolTurnAStage 全部用例 | 全部通过 | REG-1 |
| L-stage backfill | TestLStageBackfill 全部用例 | 全部通过 | REG-1 |
| 生命周期 | TestLifecycle 全部用例 | 全部通过 | REG-1 |
| 断路器 | TestIsAvailable 全部用例 | 全部通过 | REG-1 |

## 3. 环境与工具

| 项目 | 配置 |
|------|------|
| 测试框架 | pytest |
| Mock 策略 | `_mock_embed`: `@mock.patch('ca.embedding.EmbeddingClient.embed', return_value=[0.1]*768)` |
|  | `_mock_llm`: `@mock.patch('ca.__init__.ContextAssembler._call_llm_for_l1', return_value=('mock_response', 'stop'))` |
| `post_tool_call` 参数模拟 | `fixture` 返回标准 kwargs dict |
| 现有 fixture 适配 | `ca_engine`: 新增 `flush_tool_buffer` mock；`process_turn_async` 签名适配 |
| 环境 | `cd /home/i1j/.hermes/profiles/tester/plugins/ca_assembler && python -m pytest`

## 4. Mock 策略详细

### 4.1 新增 fixture: `mock_post_tool_call_kwargs`

```python
@pytest.fixture
def mock_post_tool_call_kwargs():
    return {
        "tool_name": "read_file",
        "args": {"path": "/tmp/test.txt"},
        "result": "file content",
        "status": "ok",
        "tool_call_id": "call_abc123",
        "api_request_id": "req_001",
        "turn_id": "turn_001",
        "duration_ms": 123,
        "session_id": "test_session",
        "error_type": None,
        "error_message": None,
    }
```

### 4.2 `post_tool_call` hook 的 pytest mock

在 test_plugin.py 中新增测试：模拟 Hermes 调用 `_on_post_tool_call(**kwargs)`，验证：
1. `engines` 中存在对应 session 的 plugin
2. `engine.buffer_tool_call` 被调用（可通过 mock 或检查 buffer 状态）

### 4.3 数据库验证模式

```python
def count_tool_turns(engine) -> int:
    """返回 DB 中 tool 类型记录总数。"""
    store = engine.store
    rows = store.read_session(engine._session_id)
    return sum(1 for r in rows if r.get("turn_type") == "tool")
```

## 自检
- [x] 所有新增功能点有对应测试场景
- [x] 回归场景覆盖现有所有测试文件
- [x] Mock 策略完整（embed + llm + hook 参数）
- [x] 零重复验证有具体方法
- [x] 环境准备已明确
