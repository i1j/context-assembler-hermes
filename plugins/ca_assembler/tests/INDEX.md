# CA 测试体系总览

> 上下文汇编器（Context Assembler）测试体系文档。
> 代码位置: `~/.hermes/profiles/tester/plugins/ca_assembler/`
> 决策树主入口（OV）: `design/decision-points-wiki.md`
> 节点索引（OV）: `design/decision-points/INDEX.md`

---

## 1. 测试哲学

1. **Hermetic 环境** — 跑测试用 `tests/run_ca_tests.py` 包装器（清空 API key、临时 HOME、UTC 时区、C.UTF-8 locale、`-n 4` xdist）。`conftest.py` 强制 autouse fixture 实现上述隔离。
2. **不写 change-detector 测试** — 不断言模型数量/配置版本号/枚举值等「预期会变化的数据」。只断言行为和不变关系。
3. **交叉验证** — 根因分析需 ≥2 源（代码 + 运行结果 + 日志）一致。
4. **七维分析** — 覆盖率/边界/负面/异常/增量/幂等/性能（详见 `tests/audit/cross_ref_wiki_audit.py`）。

## 2. 跑法

```bash
# 完整套件（推荐）
source venv/bin/activate
python tests/run_ca_tests.py

# 直接跑（需已激活 venv）
python -m pytest tests/ -q -n 4

# 单元测试（topic_manager）
python -m pytest tests/unit/test_topic_manager.py -v

# 集成测试（topic-aware A-stage）
python -m pytest tests/stage/test_a_stage_topic_aware.py -v

# 全部：398 passed, 10 skipped（v460 归档标记）
```

## 3. 分层

| 层级 | 目录 | 内容 |
|------|------|------|
| 单元 | `tests/unit/` | 单模块全 mock（topic_manager、grade、cache、retrieval、stats、tool_summarizer） |
| 集成（stage） | `tests/stage/` | 跨模块管线 + mock LLM/embed（a_stage、e_stage、f_stage） |
| 存储 | `tests/store/` | SQLite schema/query（store_v5、embedding） |
| 插件 | `tests/plugin/` | Hermes Plugin 生命周期（test_plugin） |
| 配置 | `tests/config/` | 配置加载/校验（test_config、test_health） |
| 审计 | `tests/audit/` | 交叉验证（cross_ref_wiki_audit） |
| 独立 | `tests/` | 独立测试（test_fstage、test_parse_v1、test_store） |
| 归档 | `tests/legacy/` | v4.6 旧 API 测试（test_v460_archive，标记 `@pytest.mark.v460` 跳过） |

## 4. 决策点 ↔ 测试文件 对照矩阵

| 测试文件 | 覆盖决策点 | 决策类型 |
|---------|-----------|---------|
| `tests/unit/test_topic_manager.py` (99) | TP-001, TP-002, TP-006, TP-007 | 话题拣选 |
| `tests/unit/test_grade.py` (10) | TP-002 (from_topic_grade) | 话题拣选 |
| `tests/unit/test_cache.py` | D-001, D-003, RE-001 | 缓存/检索 |
| `tests/unit/test_retrieval.py` | RE-001, RE-002, C-015 | 检索 |
| `tests/unit/test_stats.py` | CR-002, R-004 | 统计/装配 |
| `tests/unit/test_tool_summarizer.py` | C-004 | C-stage 工具 |
| `tests/stage/test_a_stage.py` | R-001, C-010, V-001 | A-stage |
| `tests/stage/test_a_stage_topic_aware.py` (13) | TP-002, TP-004, TP-007 | 话题感知 A-stage |
| `tests/stage/test_e_stage.py` | SC-001, SC-002 | E-stage Schema v5 |
| `tests/stage/test_f_stage.py` | R-004, L1-004~L1-011 | F-stage/L1 |
| `tests/test_fstage.py` | R-004, L1-001 | F-stage |
| `tests/test_parse_v1.py` (P1-P15) | L1-004~L1-011 | L1 摘要 |
| `tests/test_store.py` | S-001, S-002, S-003 | 存储层 |
| `tests/store/test_store_v5.py` | SC-001, SC-003, SC-004 | Schema v5 存储 |
| `tests/store/test_embedding.py` | TP-002, SC-004 | 嵌入/话题 |
| `tests/plugin/test_plugin.py` | P-001, P-004 | 插件层 |
| `tests/config/test_config.py` | Config 体系 | 配置 |
| `tests/config/test_health.py` | Config 体系 | 配置 |
| `tests/legacy/test_v460_archive.py` (v4.6) | TP-001, TP-002, TP-003 | 话题拣选(归档) |
| `tests/audit/cross_ref_wiki_audit.py` | TP-001, TP-002, TP-006, TP-007 | 交叉验证审计 |

### 按决策类型反向查找

| 决策类型 | 测试文件 |
|---------|---------|
| 话题拣选 (TP-*) | test_topic_manager, test_grade, test_a_stage_topic_aware, test_embedding, test_v460_archive, cross_ref_wiki_audit |
| A-stage (C-010, V-*) | test_a_stage, test_a_stage_topic_aware |
| E-stage (SC-*) | test_e_stage, test_store_v5, test_embedding |
| F-stage (R-004, L1-*) | test_f_stage, test_fstage, test_parse_v1 |
| 存储层 (S-*) | test_store, test_store_v5 |
| 缓存 (D-*, RE-*) | test_cache, test_retrieval |
| 工具摘要 (C-004, T-*) | test_tool_summarizer |
| 插件 (P-*) | test_plugin |
| 配置 (F-*) | test_config, test_health |
| 审计 | cross_ref_wiki_audit |

## 5. 测试缺口登记

| ID | 决策点 | 问题 | 状态 |
|----|--------|------|------|
| GAP-7 | S-001, D-001, RE-001 | cache/retrieval/SessionManager 零直接单元测试 | ⏳ 推迟 |
| GAP-8 | — | 空壳测试文件（test_concurrency.py 等） | ⏳ 推迟 |
| GAP-9 | INC-003 | 端到端测试全部 mock LLM → 真实调用从未验证 | ⚠️ 测试设计缺陷 |
| GAP-10 | V-004 | v5.1 `_format_tool_group_assembly` 核心实现在 tester profile 独立副本 | ⚠️ 分化 |

## 6. 覆盖统计

- 单元测试: 6 文件（test_topic_manager、test_grade、test_cache、test_retrieval、test_stats、test_tool_summarizer）
- 集成测试: 5 文件（test_a_stage、test_a_stage_topic_aware、test_e_stage、test_f_stage、test_fstage、test_parse_v1）
- 存储测试: 2 文件（test_store、test_store_v5、test_embedding）
- 插件测试: 1 文件（test_plugin）
- 配置测试: 2 文件（test_config、test_health）
- 审计测试: 1 文件（cross_ref_wiki_audit）
- 归档测试: 1 文件（test_v460_archive）

决策点覆盖率: 所有 7 个 TP 决策点已覆盖（单元+集成），其他决策点含单文件覆盖。

## 7. 相关文档

- `design/decision-points-wiki.md` — 决策树主入口（OV）
- `design/decision-points/INDEX.md` — 节点索引（OV）
- `AGENTS.md` — 项目开发指南（测试跑法）
- `tests/conftest.py` — 测试配置（hermetic fixtures）
- `tests/run_ca_tests.py` — 测试运行包装器
- `tests/audit/cross_ref_wiki_audit.py` — 交叉验证审计脚本
