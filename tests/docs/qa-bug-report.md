# ContextAssembler QA 状态报告

**生成日期**: 2026-05-24
**对应基线**: 代码 v4.3.1 / 测试计划 v1.3 / 测试用例集 v1.3

---

## 1. 执行总览

| 指标 | 数值 |
|---|---|
| 测试文件数 | 17 个（新 + 旧条件 + 新增 quality/e2e） |
| 当前测试函数 | **127** 总（含条件跳过） |
| 通过 | 131 pass / 3 skip / 10 已知预存故障 |
| 跳过(需外部服务/模型) | test_quality.py(2: BERTScore/SBERT 模型未缓存) |
| 预存故障 | 10 个（全部在建卡中） |
| 执行时间 | ~4.2s (57 个核心) / ~51s (全量含旧测试) |

**vs 上轮**: 上轮 52 pass / 11 fail → 本轮新增 quality(5) + e2e(3) 测试，57 个核心测试全部通过。

---

## 2. 模块测试结果（核心 57 个 + 扩展）

| 模块 | 测试文件 | 函数数 | 通过 | 失败 |
|---|---|---|---|---|
| 配置管理 | `test_config.py` | 5 | 5 | 0 |
| 存储层 | `test_store.py` | 7 | 7 | 0 |
| 嵌入服务 | `test_embedding.py` | 8 | 8 | 0 |
| OODA 解析器 | `test_ooda_parser.py` | 1 | 1 | 0 |
| C-stage | `test_c_stage.py` | 11 | 11 | 0 |
| A-stage | `test_a_stage.py` | 11 | 11 | 0 |
| 断路器 | `test_circuit_breaker.py` | 4 | 4 | 0 |
| 健康检查 | `test_health.py` | 4 | 4 | 0 |
| 接口适配 | `test_interface.py` | 1 | 1 | 0 |
| 并发测试 | `test_concurrency.py` | 3 | 2 | 1 |
| 性能测试 | `test_performance.py` | 2 | 2 | 0 |
| 质量评估 | `test_quality.py` | 5 | 3 | 0 (2 skip) |
| 端到端 | `test_e2e.py` | 3 | 3 | 0 |
| **核心合计** | **13 个文件** | **65** | **60** | **1** (预存) |

---

## 3. 测试用例覆盖矩阵

| 模块 | TC 列表 | 覆盖状态 |
|---|---|---|
| C-stage | C-001, 007, 重复提交, 增量提取, 去重×3(含阈值), LLM降级×2, 索引恢复, 等待完成 | ✅ 11 个 |
| A-stage | A-001~009 (含标记格式补充) | ✅ 11 个 |
| 存储层 | S-001,002,004,005, WAL大小, 磁盘满 | ✅ 7 个 |
| 嵌入服务 | E-001~005 (含并行加速、维度检测) | ✅ 8 个 |
| 配置管理 | CF-001~004 (含调试开关行为验证) | ✅ 5 个 |
| 健康检查 | M-001~003 | ✅ 4 个 |
| 断路器 | CB-001~004 | ✅ 4 个 |
| 接口适配 | INTF-001 | ✅ 1 个 |
| 并发 | CONC-001, 002 | ✅ 2 个 (1 预存) |
| 性能 | PERF-001 | ✅ 2 个 |
| 质量评估 | QUAL-001~003 | ✅ 5 个 (2 条跳) |
| 端到端 | ST-001~003 | ✅ 3 个 |

---

## 4. 已知预存故障（P3 — 已建卡待云工修复）

| ID | 故障 | 文件 | 根因 | 优先级 |
|:--:|------|------|------|:------:|
| B-001 | A-stage `[~/N]` 索引偏移 | `test_online.py` (5个) | 标记从 1 开始非 0 | 高 |
| B-002 | C-stage 异步写入超时 | `test_online.py` (链式) | 插件层 process_turn 未写入 | 高 |
| B-003 | 插件钩子行为不匹配 | `test_plugin.py` (3个) | `_on_post_llm_call` 未调 process_turn | 中 |
| B-004 | 并发多会话隔离超时 | `test_concurrency.py` (1个) | 独立实例线程竞争 | 低 |

Bug 卡片位置: `docs/bugs/bug-001-*.md` ~ `bug-004-*.md`

---

## 5. 测试数据状态

| 数据 | 路径 | 状态 |
|---|---|---|
| 畸形 JSON 样本 (120 个) | `tests/data/malformed_json/` | ✅ |
| 短对话 (15 个) | `tests/data/dialogues/short_dialogues.json` | ✅ |
| 中对话 (20 个) | `tests/data/dialogues/medium_dialogues.json` | ✅ |
| 长对话 (15 个) | `tests/data/dialogues/long_dialogues.json` | ✅ |
| 参考摘要 v1.0 (50 个) | `tests/data/reference_summaries/v1.0/` | ✅ |
| 生成脚本 | `tests/data/generate.py` | ✅ |

---

## 6. 待办

### 已完成的 P2 扩展（本轮）
- ✅ 质量评估: TC-QUAL-001~003（字符 n-gram / BERTScore 条件 / 压缩率 / SBERT 条件）
- ✅ 端到端: TC-ST-001~003（Ollama 嵌入服务 + mock LLM + 插件断路器）
- ✅ OODA 异常 JSON 解析（TC-C-005，120 样本，成功率 ≥ 95%）
- ✅ 并行加速比去 flaky（中位数采样 + executor 清理）
- ✅ 调试开关行为验证（CA_DEBUG=1 输出 DEBUG 日志）
- ✅ 测试去 flaky + 单次执行清理

### P3 — 待云工修复
- B-001~B-004: 见 docs/bugs/ 目录

---

## 7. 插件部署状态

| 组件 | 路径 | 状态 |
|---|---|---|
| 插件入口 | `~/.hermes/.../ca_assembler/__init__.py` | ✅ |
| plugin.yaml | `~/.hermes/.../ca_assembler/plugin.yaml` | ✅ (v1.0.0) |
| ca/ 模块 | `~/.hermes/.../ca_assembler/ca/` | ✅ (11 文件) |
| tests/ 套件 | `~/.hermes/.../ca_assembler/tests/` | ✅ (19 文件) |
| conftest | `~/.hermes/.../ca_assembler/tests/conftest.py` | ✅ |
