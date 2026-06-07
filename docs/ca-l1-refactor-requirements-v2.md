# L1 摘要系统重构 — 需求与设计方案 v2

**状态**: 技术方案阶段 · 待交叉评审  
**基线**: v4.6.0 (commit `1afc1df`)  
**日期**: 2026-06-15  
**来源**: `docs/ca-l1-whitepaper-v1.2-gm.md`（已通过三轮红队审查 ✅）  
**前序**: v1 技术方案存档于 `docs/ca-l1-refactor-technical-proposal-v1.md`

---

## 1. 需求规格

### 1.1 功能概述

重构 L1 摘要生成链路，将当前"会议纪要摘要助手+JSON 解析+无截断检测"架构，替换为基于 PDD (Prompt-Driven Development) 的 Markdown+XML 范式，由外围 Python 代码兜底约束。

### 1.2 功能需求

| # | 需求点 | 验收标准 |
|---|--------|---------|
| REQ-1 | 替换 L1 生成提示词为新 PDD 版 | 新 prompt 使用"研发对话意图分析器"人设，输出 4 类 Markdown + `<core_change>` XML 标签 |
| REQ-2 | 新增 `CA_L1_TEMPERATURE` 配置项 | 可通过环境变量设置，默认 0.3，validate/reload 支持 |
| REQ-3 | 新增 `CA_L1_MAX_TOKENS` 配置项 | 可通过环境变量设置，默认 800，validate/reload 支持 |
| REQ-4 | L1 生成调用使用独立 temperature 和 max_tokens | 不再硬编码，不再共用 `LLM_NUM_PREDICT` |
| REQ-5 | 新增截断检测机制 | 双重校验：`finish_reason=='length'` + `endswith('</core_change>')`，任一触发抛 `L1TruncatedException` |
| REQ-6 | 新增 `L1TruncatedException` 异常类 | C-stage 和 L-stage 都能捕获并触发降级 |
| REQ-7 | 新增 `parse_v1_markdown_xml()` 防御性解析器 | 解析 Markdown+XML 输出为(l1_dict, l0_text)，含语义短路和智能截断 |
| REQ-8 | 新增 `format_previous_summary_for_prompt()` 适配器 | 将 DB 中历史 l1_text 统一转换为新提示词期望的 Markdown 格式 |
| REQ-9 | OODAParser.TITLE_ALIASES 扩展 | 支持新 4 类中文标题别名（现象与问题/背景与约束/决策与共识/后续行动） |
| REQ-10 | L-stage `_backfill_dialogue` 同步 | 使用新配置参数，支持截断检测 |
| REQ-11 | DB 存储格式不变 | 仍使用 5 类英文 key 的 JSON，通过 `TITLE_ALIASES` 映射实现兼容 |
| REQ-12 | Metrics 接入（4 个指标） | ca.l1.truncated_fallback, ca.l1.parse_fallback_count, ca.l0.skipped_empty, ca.l1.latency_ms |
| REQ-13 | 向后兼容 | 现有 DB 中的旧格式 l1_text 在历史摘要注入时自动转换为新格式 |

### 1.3 约束条件

| 维度 | 约束 |
|------|------|
| 技术栈 | 纯 Python，无新增第三方依赖 |
| DB 格式 | 保持 5 类英文 key JSON，不做 schema 迁移 |
| 向后兼容 | 旧 DB 数据读入时自动适配 |
| 模型 | 兼容 4B 级别小参数模型（当前使用 qwen3-4b-instruct） |
| 宿主 | 不修改 Hermes 核心代码 |

### 1.4 不做什么（明确边界）

- 不修改 DB schema（无 migration）
- 不修改 Hermes 插件层（pre_llm_call / post_llm_call 钩子）
- 不修改 ToolSummarizer
- 不修改 A-stage 检索/升级逻辑
- 不修改 topic picking 相关代码

---

## 2. 总体设计

### 2.1 方案概述

采用 PDD (Prompt-Driven Development) 哲学：

- **模型负责**: 语义理解、信息抽取、Markdown 续写（模型预训练擅长的）
- **代码负责**: 字数截断、格式清洗、边界校验、新旧数据兼容、下游防护

数据流：

```
旧 DB JSON (5类英key)               LLM 输出 (4类中文+XML)
    │                                      │
    ▼                                      ▼
format_previous_summary_for_prompt()    parse_v1_markdown_xml()
    │                                      │
    ├─ None/"无" → "无"                    ├─ 物理截断尾随噪音
    ├─ JSON → _json_to_v1_markdown         ├─ 提取 <core_change>
    │        → 4类 Markdown                ├─ 提取 OODA 4 类列表
    └─ 纯文本 → 原样返回                   └─ 语义短路 → (l1_dict, l0_text)
           │                                      │
           ▼                                      ▼
    ┌──────────────────────────────────────────────┘
    │  主流程仍走 ooda_parser.parse()
    │  （TITLE_ALIASES 扩展后兼容新旧两种格式）
    ▼
ooda_parser.parse() → clean_increment() → DB (5类英key JSON)
```

### 2.2 涉及变更

| 操作 | 文件 | 改动内容 |
|------|------|---------|
| 修改 | `ca/config.py` | 新增 `L1_TEMPERATURE` + `L1_MAX_TOKENS` + validate + reload |
| 修改 | `ca/prompts.py` | 替换 `L1_GENERATION_PROMPT` 为 PDD 新版 |
| 新增函数 | `ca/post_process.py` | 新增 `parse_v1_markdown_xml()` + `_safe_truncate()` + `_json_to_v1_markdown()` + 常量/预编译正则 |
| 修改 | `ca/ooda_parser.py` | `TITLE_ALIASES` 扩展 4 类中文别名 |
| 修改 | `ca/__init__.py` | `_call_llm_for_l1` 改返回 `Tuple[str,str]` + 截断检测 + 新参数 + `L1TruncatedException` |
| 新增函数 | `ca/store.py` | 新增 `format_previous_summary_for_prompt()`（从 post_process 引用） |
| 修改 | `ca/lstage.py` | `_backfill_dialogue` 同步配置参数 + 截断检测 |
| 新增 | `tests/test_parse_v1.py` | ~15 测试用例 |
| 新增 | `tests/test_store_adapter.py` | ~8 测试用例 |
| 修改 | `tests/test_c.py` | 追测 ~3 个截断检测用例 |
| 修改 | `tests/test_v440.py` | 追测 ~2 个 L-stage 截断路径用例 |

---

## 3. 接口设计

### 3.1 新增/修改的公共接口

| 接口 | 签名 | 说明 |
|------|------|------|
| `parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]` | 新增 | 主解析器：返回 (l1_dict, l0_text) |
| `_safe_truncate(text: str, max_len: int) -> str` | 新增 | 智能截断，降`.`优先级防`config.py`被腰斩 |
| `_json_to_v1_markdown(data: dict) -> str` | 新增 | JSON→新 4 类 Markdown 格式 |
| `format_previous_summary_for_prompt(l1_text_from_db: str) -> str` | 新增 | DB → prompt 输入适配 |
| `_call_llm_for_l1(prev_l1, l2_text) -> Tuple[str, str]` | **改签名** | 返回 `(response_text, finish_reason)` |
| `L1TruncatedException` | 新增 | 截断时抛出的异常类 |

### 3.2 数据流（C-stage 对话轮）

```
_run_c_stage()
  │
  ├─ prev_l1 = _get_previous_l1()  # DB JSON → dict
  │
  ├─ prompt = L1_GENERATION_PROMPT.format(
  │     previous_summary=format_previous_summary_for_prompt(prev_l1),  # ← 新适配器
  │     current_dialog=l2_text)
  │
  ├─ response_text, finish_reason = _call_llm_for_l1(prev_l1, l2_text)  # ← 新签名
  │     └─ 截断检测: finish_reason=='length' || !endswith('</core_change>')
  │        → raise L1TruncatedException → _assemble_status=1 (backfill)
  │
  ├─ l1_dict, l0_text = parse_v1_markdown_xml(response_text)  # ← 新解析器
  │
  ├─ parsed = ooda_parser.parse(json.dumps(l1_dict), previous_summary=prev_l1)
  │     └─ TITLE_ALIASES 扩展后能解析新 4 类中文标题
  │
  └─ → DB write (5类英key JSON, 与旧格式相同)
```

### 3.3 Metrics 接口

4 个 Prometheus Counter/Gauge/Histogram：

| 指标 | 类型 | 指标名 | 接入点 |
|------|------|--------|--------|
| 截断降级次数 | Counter | `ca.l1.truncated_fallback` | `_call_llm_for_l1` 抛出 `L1TruncatedException` 时 |
| 解析 fallback 次数 | Counter | `ca.l1.parse_fallback_count` | `parse_v1_markdown_xml` 未匹配到 `<core_change>` 时 |
| 跳过向量化轮次 | Gauge | `ca.l0.skipped_empty` | L0 为空字符串跳过 embedding 时 |
| L1 延迟 | Histogram | `ca.l1.latency_ms` | `_call_llm_for_l1` 调用前后计时 |

---

## 4. 测试策略

### 4.1 测试范围

| 层次 | 范围 | 方式 |
|------|------|------|
| 单元测试 | `parse_v1_markdown_xml` 各种输入（正常/退化/空/截断） | 自动化 |
| 单元测试 | `_safe_truncate` 标点截断边界 | 自动化 |
| 单元测试 | `MEANINGLESS_CORE` 语义短路 | 自动化 |
| 单元测试 | `format_previous_summary_for_prompt` 各种输入 | 自动化 |
| 单元测试 | `_json_to_v1_markdown` 新旧格式转换 | 自动化 |
| 回归测试 | C-stage 截断检测 + `L1TruncatedException` | 追测 |
| 回归测试 | L-stage `_backfill_dialogue` 截断路径 | 追测 |
| 回归测试 | 全量测试（除 system 测试） | `pytest tests/ --ignore=tests/test_system.py` |

### 4.2 关键测试场景

| 场景 | 输入 | 预期 |
|------|------|------|
| 正常 Markdown+XML 输出 | 完整 4 类 + `</core_change>` | 正确解析各字段 |
| 无 `<core_change>` | 只有 Markdown 无 XML | fallback 空结果 |
| 截断输出（无 `</core_change>`） | 无结尾标签 | `_safe_truncate` 截断 |
| 语义无意义 core | `core_change: "无"` | `l0_text=None` |
| 旧 JSON → Markdown | 旧 l1_text 5 类 JSON | 正确转换为 4 类 Markdown |
| 空历史摘要 | `None` 或 `""` | 返回 `"无"` |
| LLM 返回截断 | finish_reason='length' | 抛 `L1TruncatedException` |
| l0_text 超长 | 200 字符 | 智能截断 100 字符 |

---

## 5. 边界与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| endswith 误判截断 | L-stage 降级率升高 | L-stage 有 3 次重试 + 永久标记兜底 |
| `_json_to_v1_markdown` 遇到未知 JSON 结构 | 转换失败 | fallback 原样返回纯文本 |
| Metrics 基础设施不完整 | 指标无法上报 | 先使用 logger.warning 计数，确认 Hermes Metrics 接口后接入 |
| 新 prompt 在小模型上表现不佳 | 摘要质量下降 | 保留旧格式读取能力，可回退 |

---

## 6. 实施步骤

### 6.1 开发线步骤

1. **Config** — 新增 `L1_TEMPERATURE` + `L1_MAX_TOKENS` + validate + reload
   - 验证: `python -c "from ca.config import Config; print(Config.L1_TEMPERATURE, Config.L1_MAX_TOKENS)"`

2. **Prompts** — 替换 `L1_GENERATION_PROMPT`
   - 验证: `python -c "from ca.prompts import L1_GENERATION_PROMPT; print(L1_GENERATION_PROMPT[:50])"`

3. **OODAParser** — 扩展 `TITLE_ALIASES`
   - 验证: `python -c "from ca.ooda_parser import OODAParser; print(OODAParser.TITLE_ALIASES['new_materials'])"`

4. **post_process.py** — 新增 `parse_v1_markdown_xml()` + `_safe_truncate()` + `_json_to_v1_markdown()` + 常量/预编译正则
   - 验证: `python -c "from ca.post_process import parse_v1_markdown_xml; r = parse_v1_markdown_xml('### 现象与问题\n- 测试\n<core_change>测试</core_change>'); print(r)"`

5. **store.py** — 新增 `format_previous_summary_for_prompt()`
   - 验证: `python -c "from ca.store import format_previous_summary_for_prompt; print(format_previous_summary_for_prompt('{\"core_change\":\"test\"}'))"`

6. **L1TruncatedException + _call_llm_for_l1 改造** — 改签名、截断检测、新参数
   - 验证: `pytest tests/ -v -k "test_c" --ignore=tests/test_system.py`

7. **L-stage 同步** — `_backfill_dialogue` 同步配置 + 截断检测
   - 验证: `pytest tests/ -v -k "test_v440" --ignore=tests/test_system.py`

8. **Metrics 接入** — 4 个指标接入点
   - 验证: 日志确认指标触发

### 6.2 测试线步骤

1. 新增 `tests/test_parse_v1.py` — `parse_v1_markdown_xml` 全场景测试
2. 新增 `tests/test_store_adapter.py` — `format_previous_summary_for_prompt` 测试
3. 追测 `tests/test_c.py` — C-stage 截断检测 + `L1TruncatedException`
4. 追测 `tests/test_v440.py` — `_backfill_dialogue` 截断路径
5. 全量回归

---

## 自检

- [x] 无 TODO/TBD 占位符
- [x] 需求与设计一致
- [x] 验收标准可验证
- [x] 改动范围明确（8 文件 + 2 新测试文件）
- [x] 测试策略可执行
- [x] 边界条件有处理方案
- [x] 明确标注了"不做什么"
