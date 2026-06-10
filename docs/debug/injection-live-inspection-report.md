# 调试报告：CA 注入状态实时查看方法

## 背景

本对话（session 20260610_013946_bf8a52）在调试 CA（ContextAssembler）的三路注入模式过程中，
发现无法通过调试脚本（`debug_injection_demo.py`）查看**当前实时会话**的注入结果。

## 问题

`debug_injection_demo.py` 使用硬编码合成数据，只能验证 `_build_aligned_outcomes()` 的函数逻辑，
无法回答"当前对话 replace mode 实际往 LLM context 里灌了什么"。

## 解决方案：直接从 CA 缓存 DB 调注入接口

核心发现：不依赖 Hermes 运行时，仅凭 CA 缓存 SQLite DB 就可以**完整复现注入结果**。

### 实现步骤（3 行 Python）

```python
# 1. 从 DB 重建引擎
engine = ContextAssembler(session_id=session_id, db_path=db_path)

# 2. 重建内存 cache（含 l1_texts、tool_group_l1_texts 等）
from ca.cache import CacheBuilder
builder = CacheBuilder(engine.store)
engine.cache = builder.build(session_id)

# 3. 调用当前对话的注入接口
result = engine._compute_assemble_plan(user_message="系统后台审查")
plan = result.plan                                    # 真实 plan
messages = result.messages                            # 真实 history
outcomes = engine._build_aligned_outcomes(plan, messages)  # 真实注入结果
```

### 为什么可行

- `_compute_assemble_plan()` 从 `_rebuild_messages_from_cache()` 读取 turn_cache
- `_build_aligned_outcomes()` 只依赖 `engine.cache`（l1_texts、tool_group_l1_texts 等）
- 全部数据已持久化在 CA 缓存 DB 中，不需要 Hermes 运行中状态

### 与 debug_injection_demo.py 的差异

| 维度 | debug_injection_demo.py | 本方法 |
|------|------------------------|--------|
| 数据源 | `_make_history()` 硬编码 | CA 缓存 DB 真实数据 |
| plan | `_make_plan()` 伪造 | `_compute_assemble_plan()` 真实 |
| cache | 手动写入字典 | `CacheBuilder.build()` 从 DB 恢复 |
| 覆盖范围 | 仅验证函数逻辑 | 验证当前会话真实注入 |
| 运行依赖 | pytest + ca_engine fixture | 仅需 Python + CA 缓存 DB |

## 当前对话注入状态（2026-06-10 02:30）

- 对话轮数: 19
- 消息总数: 468
- plan 条目: 155
- L1 填充率: 100%（424 行全非空）
- 降级行: 0

### 观察到的行为

1. **尾区保护生效**: 最近 2 轮 user 行被 L2 保留原文
2. **工具组摘要正常**: `"工具组：调用 skill_manage（1个，ok）"`
3. **工具摘要正常**: `"skill_manage: 成功: action=patch, name=tester-workflow, ..."`
4. **assistant 纯回复**: 保留原文（L2 tail 或非注入行）
5. **所有行 _assemble_status=0**: 无降级

## 后续调校方向

1. 验证 replace mode 下 L0 注入的移除行为（当前测试全是 L1） — `_build_aligned_outcomes` 的 L0 分支
2. 检查多工具组场景下的 `group_idx` 边界（当前测试全是单组） — 插件层 `_on_pre_tool_call` 的 `api_call_count`
3. 检查 `_merge_consecutive_tool_outcomes` 的 ×N 合并是否真实触发

## 文件

- 调试脚本: `tests/debug_injection_demo.py`
- 运行时注入查看代码: 上述 3 行 Python（每次运行需替换 session_id 和 user_message）
