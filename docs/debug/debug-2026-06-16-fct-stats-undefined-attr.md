# F-stage 因 AssembleStats 缺失属性全部降级

## 现象
agent.log 中每轮 F-stage 报错：
```
ERROR ca: F‑stage crash turn 8: 'AssembleStats' object has no attribute 'fct_latency_ms'
```

F-stage 全程 fallback，dialogue_ok=False，对话轮 Fct 恒为"本轮无新内容"。

## 根因
未提交的 `ca/__init__.py` 新增 `_call_llm_for_fct` 中引用两处新 stats 属性：
- line 998: `self.stats.fct_latency_ms += elapsed_ms`
- line 341/1002: `self.stats.fct_truncated_fallback += 1`

但 `ca/stats.py` `AssembleStats.__init__` 未定义这两个属性。LLM 成功返回后在第 998 行抛 AttributeError → 异常冒泡到 `_run_f_stage` 外层 except → 写 fallback Fct。

## 影响
| 影响 | 程度 |
|------|------|
| 对话轮 Fct | 全量 fallback（"本轮无新内容"）|
| per-tool Fct | 不受影响（`_on_post_tool_call_v5` 独立路径）|
| A-stage 工具注入 | 正常（per-tool Fct 正确读取）|
| 话题分割 | 数据质量下降导致精度降低 |

## DB 验证
所有 session 中 tool 行 Fct 覆盖率 100%——per-tool 摘要一直是工作的。

| session | tool 行 | 缺 Fct | 覆盖率 |
|---------|---------|--------|--------|
| 20260613_224455 | 462 | 0 | 100% |
| 20260613_153329 | 305 | 0 | 100% |
| 20260614_120409 | 305 | 0 | 100% |
| mqf2n0hkqqsgha | 159 | 0 | 100% |
| mqfhfc2vm0be4e | 136 | 0 | 100% |
| mqennsf1t970ru | 217 | 0 | 100% |

## 修复
`ca/stats.py` 新增两属性：
- `fct_truncated_fallback: int = 0`（第 341/1002 行引用）
- `fct_latency_ms: float = 0.0`（第 998 行引用）

同步更新 `__str__` 展示行。

## 验证
- 编译: `python3 -c "from ca.__init__ import ContextAssembler"` OK
- 测试: `pytest tests/ -q` → 269 passed, 19 skipped
- 属性: `s.fct_latency_ms += 100.0` ✅ `s.fct_truncated_fallback += 1` ✅

## 建议
- 后续增加 F-stage 统计属性时同步更新 stats.py
- 考虑在 F-stage try/except 中添加属性缺失的快速诊断日志
