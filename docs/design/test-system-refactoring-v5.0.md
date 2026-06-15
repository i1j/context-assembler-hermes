# 测试体系重构方案 — v5.0 对齐

## 日期
2026-06-15

## 背景
CA v5.0 重构后（E-stage 写即落盘、F-stage Fct 生成、A-stage DB 直查替换），46 个测试
引用已删除的 v4.x API，无法通过。同时 v5.0 核心管线（A-stage/E-stage/F-stage）的
P0 路径零测试覆盖。

## 方案

### 一、清理死测试（46 个）

| 文件 | 操作 | 数量 | 原因 |
|------|------|------|------|
| `test_a.py` | 整文件删除 | 18 | 全部引用已删除 `assemble()` |
| `test_c.py` | 整文件删除 | 14 | 全部引用旧 buffer/plan API |
| `test_aligned_outcomes.py` | 整文件删除 | 1 | 引用已删除 `TurnPlanEntry` |
| `test_v460.py` (4 个) | 删除 4 条 | 4 | `assemble()`, `_compute_turn_plan_v2` |
| `test_lifecycle.py` (2 个) | 删除 2 条 | 2 | 旧 API |
| `test_degradation.py` (1 个) | 删除 1 条 | 1 | 旧 API |
| `test_v521_topic.py` (6 个) | 删除 6 条 | 6 | `_detect_forced_split_turns` 已删除 |

执行后：291 ✅ / 22 ⏭️ / 0 ❌

### 二、补齐 P0 测试

| P0 路径 | 测试内容 | 文件 |
|---------|---------|------|
| `_simple_mutation_mode_v5` | 替换逻辑、尾部保护区、Fct 降级 | `test_astage.py` |
| `_on_post_api_response_v5` | thought 落盘、tool 占位写入 | `test_estage.py` |
| `_run_f_stage` | DB 读 Elm → 拼内容 → 写 Fct/Hdl | `test_fstage.py` |
| `read_turn_elm_rows` / `read_prev_fct` | turn_stream 读取 | `test_store.py` 追加 |
| `_update_fct_v5` / `update_seq0_fct_v5` | Fct/Hdl 更新 | `test_store.py` 追加 |
| `_extract_hdl` | Hdl 提取 + stage_tag 截断 | `test_fstage.py` |
| `_format_fct_for_display` | 调试模式检测 + 格式化 | `test_fstage.py` |
| `_is_valid_fct` | 有效性验证 | `test_fstage.py` |
| `_on_post_tool_call_v5` | tool 行回填 + per-tool Fct | `test_estage.py` |
