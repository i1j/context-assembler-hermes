# CA 测试总览

> 测试体系入口文档 — 测试-调试记录索引见 `INDEX.md`，完整测试策略见 `13-test-strategy.md`，环境配置见 `test-env.md`。
> 权威本地源：`~/.hermes/profiles/tester/plugins/ca_assembler/tests/`（本地仓库 `tests/INDEX.md` 为对照矩阵）。

## 分层架构（对齐实际目录）

| 层 | 目录 | 说明 |
|----|------|------|
| 单元测试 | `tests/unit/` | 模块级函数测试（topic_manager 99 例、grade、cache、retrieval、stats、tool_summarizer、reality_prompts 等） |
| 阶段测试 | `tests/stage/` | A-stage、E-stage、F-stage 独立管线测试 |
| 存储测试 | `tests/store/` | turn_stream CRUD、契约列完整性、embedding |
| 插件测试 | `tests/plugin/` | 插件适配层（生命周期、断路器、hook 注册） |
| 配置测试 | `tests/config/` | 配置加载与校验、健康检查 |
| 解析测试 | `tests/parse/` | Fct 解析（PAIR_PATTERN、clean_increment） |
| 审计测试 | `tests/audit/` | 交叉验证、debug dump 审计、DOA 自防御 |
| fixtures | `tests/fixtures/` | 下沉的 store/engine fixture（conftest 收窄中） |

无独立 `tests/integration/` 目录，端到端覆盖由 plugin/stage 层承担。

## 测试状态（v6.0，2026-08-08 实测）

**826 collected = 824 passed / 1 skipped / 1 xfailed**。运行：

```bash
cd ~/.hermes/profiles/tester/plugins/ca_assembler
/usr/bin/python3 -m pytest tests/ -q -p no:cacheprovider
```

| 覆盖 | 说明 |
|------|------|
| topic_manager | ✅ `tests/unit/test_topic_manager.py`（99 用例，TP-001/002/006/007） |
| reality 链路 | ✅ `tests/unit/test_reality_prompts.py`（32 用例，决策 37/38/39/40/41） |
| fact_linking | ✅ 信号 B/C 建边（depends_on/continues/references_ov） |
| 图一致性 | ✅ graphify reality 节点补建 / merge 清理 / 注入图路 / timeline 结构化 |
| 契约测试 | ✅ `tests/store/test_store_contract.py` 逐列验证 |

> 沙箱/受限环境下 3 个外部依赖用例会失败（不计入基线）：`tests/store/test_embedding.py` 2 个（Ollama 不可达）、`tests/plugin/test_plugin.py` 1 个（profile ca_cache 写入受限）。

## 缺口登记

| 缺口 | 状态 | 详情 |
|------|------|------|
| GAP-8 | ⏳ 敞口 | 空壳测试文件 |
| GAP-9 | ⚠️ 部分修复 | 端到端全 mock LLM → 真实调用已验证（llm_return 注入机制） |
| GAP-10 | ⚠️ 分化 | v5.1 核心实现 tester profile 独立副本 |
| reasoning_content | ⏳ 待定 | 模型思考链在 conv_history 重建中的处理 |
| conftest 收窄 | ⏳ 计划中 | P0 计划：fixture 下沉 + 去 autouse mock（见本地 `docs/P0-ca-test-conftest-improvement-plan.md`） |

## 相关文档

- `INDEX.md` — 测试-调试记录索引（本目录）
- `13-test-strategy.md` — 完整测试策略（分层/不变量/统计）
- `test-env.md` — 测试环境配置（路径/依赖/命令）
- `../architecture/test-system-refactoring-v5.0.md` — v5.0 测试系统重构设计
- `../architecture/debug-20260612-verification-report.md` — 调试验证报告（历史存档）
- `../architecture/20260610-ca-mutation-thinking-rootcause.md` — 根因分析
- `wiki/architecture/13-test-strategy.md` — 本地 wiki 同源副本
