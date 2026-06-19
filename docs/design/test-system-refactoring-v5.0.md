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
|| `_on_post_tool_call_v5` | tool 行回填 + per-tool Fct | `test_estage.py` |
|| `TopicGradeManager` (全模块) | detect、grade_on_switch、get_turn_grade、_grade_topics_by_radius、_compute_centroids 等 | `test_topic_manager.py` |
|| 话题感知 A-stage | ACT/REL/FAR 三级驱动替换、_topic_mgr=None 防护、embed 失败降级 | `test_a_stage_topic_aware.py` |

### 三、补充新增测试（2026-06-19）

| 文件 | 测试数 | 覆盖内容 |
|------|--------|---------|
| `tests/unit/test_topic_manager.py` | 99 | 模块级函数（Jaccard/cosine/centroid/grade）+ TopicGradeManager 全部方法 + embed 失败场景 + 完整话题切换管线 |
| `tests/stage/test_a_stage_topic_aware.py` | 13 | ACT/REL/FAR 三级替换、user 行/thought/tool/fin 各路替换、_topic_mgr=None 防护、tail boundary、多轮混合等级、embed 失败 |

#### 覆盖的 7 个缺口

1. ✅ **topic_manager.py 零测试覆盖** → 99 个单元测试覆盖全部方法和模块级函数
2. ✅ **v4.6 遗留测试是假覆盖** → v5 _grade_topics_by_radius 3 参数版已测
3. ✅ **A-stage 只测替换力学不测话题路由** → 13 个集成测试验证 ACT/REL/FAR → mutation
4. ✅ **Grade 映射孤岛化** → 集成测试验证 from_topic_grade → 实际替换结果
5. ✅ **P0 计划缺 topic_manager** → 补入
6. ✅ **Mock 掩埋 embed 失败** → 单元 + 集成测试覆盖 embed 异常 → centroid=None → REL 降级
7. ✅ **_topic_mgr=None 降级路径无人测** → 集成测试验证全线 ACT + 不 crash
