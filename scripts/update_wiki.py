#!/usr/bin/env python3
"""Update wiki architecture pages to reflect current code state."""
import os

ROOT = "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler"


def update_f_stage():
    path = os.path.join(ROOT, "docs/wiki/architecture/03-f-stage-async-summary.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("status: 已实装", "status: 已实装（v5.10 重构）")
    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")

    # Replace daemon model with current trigger model
    old = "### 选定方案\n\nF-stage 在独立的 daemon 线程中运行，异步读取 Elm 生成 Fct/Hdl"
    new = "### 选定方案\n\nF-stage 由 `post_llm_call` 回调触发，调用 `process_turn_f_stage` 启动异步线程执行 LLM 摘要"
    content = content.replace(old, new)

    # Remove daemon code block
    old_code = "```python\ndef _f_stage_daemon(self):\n    while not self._stop_event.is_set():\n        turns = self._fetch_ungenerated_turns()\n        for turn_data in turns:\n            fct = self._call_llm_for_fct(turn_data)\n            hdl = self._call_llm_for_hdl(turn_data)\n            self.store.write_fct_hdl(turn_data.turn, fct=fct, hdl=hdl)\n        time.sleep(0.5)\n```\n"
    new_impl = (
        "```python\n"
        "# 在 post_llm_call_v5 中触发\n"
        "engine.process_turn_f_stage(turn)\n"
        "```\n\n"
        "### 实现要点\n\n"
        "- **触发条件**：`post_llm_call_v5` 回调末尾调用 `engine.process_turn_f_stage(turn)`\n"
        "- **异步线程**：`process_turn_f_stage` 在 daemon 线程中执行 `_run_f_stage`\n"
        "- **线程防护**：`_pending_tasks[turn_index].is_alive()` 防止重复线程\n"
        "- **Fct 写入**：LLM 响应解析后通过 `_update_fct_v5` 写回\n"
        "- **Hdl 写入**：从 Fct dict 的 `core_change` 提取（`_extract_hdl`）\n"
        "- **截断回退**：保留 partial[:500] 并标记 `_assemble_status=1`\n"
        "- **LLM 全失败回退**：fallback Fct + `_assemble_status=1`\n"
        "- **FctTruncatedException**：catch 后保留 partial text\n"
    )
    content = content.replace(old_code, new_impl)

    with open(path, "w") as f:
        f.write(content)
    print("Updated 03-f-stage-async-summary.md")


def update_test_strategy():
    path = os.path.join(ROOT, "docs/wiki/architecture/13-test-strategy.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")

    # Update test counts
    content = content.replace(
        "CA 插件包含 261 个测试用例",
        "CA 插件包含 407 个测试用例（v5.10 重构后）"
    )
    content = content.replace(
        "当前 261 个测试",
        "当前 407 个测试"
    )

    # Replace the test classification table
    old_table = """| 分类 | 文件 | 数量 | 覆盖内容 |
|------|------|------|----------|
| E-stage 测试 | `test_estage.py` | ~60 | 写入协议、Hook 分发、字段完整性 |
| F-stage 测试 | `test_fstage.py` | ~50 | 异步 LLM 调用、结果回写、daemon 生命周期 |
| A-stage 测试 | `test_astage.py` | ~70 | 角色队列匹配、grade 驱动替换、尾巴保护 |
| Topic 测试 | `test_v521_topic.py` | ~30 | 话题分割集成、TopicGradeManager |
| Config/Health 测试 | `test_config.py`, `test_health.py` | ~15 | 配置加载、健康检测 |
| 其余 | `test_circuit.py`, `test_embedding.py`, `test_degradation.py`, `test_lifecycle.py`, `test_parse_v1.py` | ~36 | 断路器、嵌入服务、降级、生命周期、Fct 解析 |

**死测试清理**（v5.7）：已删除 46 个指向已移除代码的测试（`a_planner`、`a_injector`、`compute_topic_groups` 等），测试集从 ~307 减少至 261。"""

    new_table = """| 分类 | 文件 | 数量 | 覆盖内容 |
|------|------|------|----------|
| E-stage 测试 | `tests/stage/test_e_stage.py` | ~20 | 写契约、Hook 字段、幂等性、行不可变 |
| F-stage 测试 | `tests/stage/test_f_stage.py` | ~31 | LLM 摘要、截断回退、故障隔离、Fct 覆盖 |
| A-stage 测试 | `tests/stage/test_a_stage.py` | ~15 | 角色队列匹配、增量缓存、快照可逆 |
| A-stage topic 测试 | `tests/stage/test_a_stage_topic_aware.py` | ~20 | ACT/REL/FAR grade 驱动替换 |
| Fct 解析测试 | `tests/parse/test_parse_v1.py` | ~24 | PAIR_PATTERN、截断检测、所有格式场景 |
| 话题管理测试 | `tests/unit/test_topic_manager.py` | ~99 | Jaccard、强制短语、半径定级、全管线 |
| 存储层测试 | `tests/store/test_store_v5.py` | ~6 | 列契约、session_id |
| 嵌入测试 | `tests/store/test_embedding.py` | ~6 | 嵌入缓存、降级 |
| 插件测试 | `tests/plugin/test_plugin.py` | ~35 | 生命周期、断路器、bg_review |
| 审计测试 | `tests/audit/` | ~48 | 交叉验证、DOA 自防御、dump 监测 |
| 配置测试 | `tests/config/` | ~17 | 配置加载、健康检测 |

**目录重组**（v5.10）：根目录独立测试已合并入 stage/、parse/ 目录。conftest 拆分为 tests/fixtures/ 子包。"""

    content = content.replace(old_table, new_table)

    # Update known gaps
    content = content.replace(
        "**已知缺失**：`TopicGradeManager` 无独立单元测试（仅通过集成测试覆盖）。",
        "**已填补**：`TopicGradeManager` 现有 99 个独立单元测试（TestGradeTopicsByRadius 11 个、TestDetect 8 个、TestGradeOnSwitch 6 个等）。"
    )

    # Update DOA section
    content = content.replace(
        "**死测试清理**（v5.7）：已删除 46 个指向已移除代码的测试",
        "**死测试修复**（v5.10）：修正 `L1TruncatedException` → `FctTruncatedException` 类名（3 个测试从 skip→pass）。"
    )

    with open(path, "w") as f:
        f.write(content)
    print("Updated 13-test-strategy.md")


def update_topic_segmentation():
    path = os.path.join(ROOT, "docs/wiki/architecture/08-topic-segmentation.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    content = content.replace(
        "- `TopicGradeManager` 当前无单元测试覆盖",
        "- `TopicGradeManager` 现有 99 个单元测试覆盖（v5.10 补齐）"
    )
    with open(path, "w") as f:
        f.write(content)
    print("Updated 08-topic-segmentation.md")


def update_bg_review():
    path = os.path.join(ROOT, "docs/wiki/architecture/07-bg-review-sync-write.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    content = content.replace(
        "- bg_review 检测依赖 Hermes 的 `get_current_write_origin()`，该函数在不同版本中可能不稳定",
        "- bg_review 检测依赖 Hermes 的 `get_current_write_origin()`，该函数在不同版本中可能不稳定\n- bg_review 路径现在有独立测试覆盖（`TestBgReview.test_bg_review_writes_fct_equals_content`）"
    )
    with open(path, "w") as f:
        f.write(content)
    print("Updated 07-bg-review-sync-write.md")


def update_incremental_cache():
    path = os.path.join(ROOT, "docs/wiki/architecture/05-incremental-cache.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    content = content.replace(
        "- 当前未持久化到磁盘，session 重启丢失缓存",
        "- 当前未持久化到磁盘，session 重启丢失缓存\n- **测试覆盖**：全量路径（`test_cache_replaces_stable_area`）、stale→rebuilt（`test_stale_flag_triggers_full_mutation`）、Fct-pending→stale（`test_pending_stale_cycle`）"
    )
    with open(path, "w") as f:
        f.write(content)
    print("Updated 05-incremental-cache.md")


def update_naming():
    path = os.path.join(ROOT, "docs/wiki/architecture/12-naming-convention.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    content = content.replace(
        "- 部分注释和日志中可能仍有残留的 L2/L1/L0 未清理干净",
        "- ~~部分注释和日志中可能仍有残留的 L2/L1/L0 未清理干净~~ ✅ v5.10 全部清理完毕\n- CA-METRIC 日志键名审计测试强制 `ca.{fct|hdl}.{metric}` 格式，防止旧术语回归"
    )
    # Remove grep check
    old_grep = "```bash\n# 检查代码中是否还残留旧术语\ngrep -rn '\\bL[012]\\b' ca_assembler/*.py | grep -v '.pyc' | grep -v deprecated\n```\n"
    new_grep = "```bash\n# 检查代码中是否还残留旧术语（v5.10 已全部清理）\ngrep -rn 'ca\\.l[012]\\.\\\\|\\bL[012]\\b' ca/ --include='*.py' | grep -v '.pyc' || echo '无残留'\n```\n"
    content = content.replace(old_grep, new_grep)
    content = content.replace(
        "## 数据验证\n\n```bash\n# 检查代码中是否还残留旧术语\ngrep -rn '\\bL[012]\\b' ca_assembler/*.py | grep -v '.pyc' | grep -v deprecated\n```\n\n## 优点",
        "## 数据验证\n\n```bash\n# 检查代码中是否还残留旧术语（v5.10 已全部清理）\ngrep -rn 'ca\\.l[012]\\.' ca/ --include='*.py' || echo '无残留'\n```\n\n## 优点"
    )
    with open(path, "w") as f:
        f.write(content)
    print("Updated 12-naming-convention.md")


def update_ov_topic_submit():
    path = os.path.join(ROOT, "docs/wiki/architecture/10-ca-ov-topic-submit.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    content = content.replace(
        "status: 已实装",
        "status: 已移除（v5.10 清理，`_fire_ov_submit` 已从 `lstage.py` 删除）"
    )
    content += (
        "\n\n### 变更记录\n\n"
        "- **v5.10** (2026-07-26)：`_fire_ov_submit` 和相关代码已从 `lstage.py` 删除\n"
        "- 对应的占位测试已在 `test_topic_manager.py` 中添加（`pytest.skip` 标记）\n"
    )
    with open(path, "w") as f:
        f.write(content)
    print("Updated 10-ca-ov-topic-submit.md")


def update_decisions_14():
    """14-l-stage-daemon.md — note daemon model replaced"""
    path = os.path.join(ROOT, "docs/wiki/decisions/14-l-stage-daemon.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("status: 已实装", "status: 已替换为 process_turn_f_stage 触发模型（v5.10）")
    with open(path, "w") as f:
        f.write(content)
    print("Updated 14-l-stage-daemon.md")


def update_decisions_29():
    """29-ca-ov-topic-submit.md — note removed"""
    path = os.path.join(ROOT, "docs/wiki/decisions/29-ca-ov-topic-submit.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("status: 已实装", "status: 已移除（v5.10）")
    with open(path, "w") as f:
        f.write(content)
    print("Updated 29-ca-ov-topic-submit.md")


def update_readme():
    path = os.path.join(ROOT, "docs/wiki/README.md")
    with open(path) as f:
        content = f.read()

    content = content.replace("updated: 2026-06-18", "updated: 2026-07-26")
    with open(path, "w") as f:
        f.write(content)
    print("Updated README.md")


if __name__ == "__main__":
    update_f_stage()
    update_test_strategy()
    update_topic_segmentation()
    update_bg_review()
    update_incremental_cache()
    update_naming()
    update_ov_topic_submit()
    update_decisions_14()
    update_decisions_29()
    update_readme()
    print("\nAll wiki updates complete!")
