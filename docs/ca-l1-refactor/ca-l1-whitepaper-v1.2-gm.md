# ContextAssembler (CA) L1 摘要系统技术白皮书

**版本**: v1.2 Gold Master  
**状态**: ✅ Approved for Production  
**存档时间**: 2026-06-15  
**来源**: 用户提供的外部设计文档，用于重构参考  
**存档说明**: 本文档描述的目标架构与当前 v4.6.0 基线有 14 项核心差异，不可直接作为合并标准。详见 `ca-devtest-workflow-selfcheck-report.md`。

---

## 文档摘要与演进历史

在 Hermes Agent 的早期架构中，L1 摘要生成强依赖 4B 级别小参数模型输出严谨的 JSON 格式。但在生产环境中，小模型暴露出格式脆弱、认知污染、字数失控与近因效应等致命弱点。

本系统通过引入 PDD (Prompt-Driven Development) 理念，彻底重构了摘要生成链路。经过 v1.0 至 v1.2 的三轮严苛红队审查，修复了正则转义丢失、幻觉标签误杀、API 契约信任风险等核心隐患。

| 版本 | 状态 | 核心变更与修复里程碑 |
|------|------|---------------------|
| v1.0 | 废弃 | 初始 PDD 架构设计，确立 Markdown+XML 范式 |
| v1.1 | 废弃 | 修复提示词"自我检查"污染、正则贪心吞噬及标点截断问题 |
| v1.2 | Release | 最终定稿。引入 re.compile 预编译免疫转义丢失；移除过度防御的标签清洗；增加 endswith 物理边界双重校验 |

## 核心设计哲学：PDD (Prompt-Driven Development)

放弃"逼迫小模型输出严谨机器语言（JSON）"的执念，转向 "顺应小模型预训练天性，由外围 Python 代码兜底约束"。

- **模型负责**（做它擅长的）：语义理解、信息抽取、顺应 Markdown 续写天性
- **代码负责**（做它做不好的）：字数截断、格式清洗、边界校验、新旧数据兼容、下游防护

---

## 1. 提示词工程

### `ca/prompts.py`

```python
L1_GENERATION_PROMPT = """你是一个研发对话意图分析器。请对比【历史摘要】与【本轮对话】，提取本轮新增的业务意图与技术决策。

<example>
### 现象与问题
- 用户反馈登录接口响应缓慢

### 背景与约束
- 数据库连接池已达上限 (100)

### 决策与共识
- 临时扩容连接池至200

### 后续行动
- 排查慢查询日志

<core_change>
排查登录接口慢查询并临时扩容数据库连接池
</core_change>
</example>

【历史摘要】
{previous_summary}

【本轮对话】
{current_dialog}

【输出规则 - 必须严格遵守】
- 增量提取：仅提取【本轮对话】中首次出现的新状态、新决策或新任务。
- 语义分类：
    - ### 现象与问题：当前发生的故障、Bug 或用户反馈。
    - ### 背景与约束：客观存在的限制、配置参数或前置条件。
    - ### 决策与共识：双方确认的解决方案或技术选型。
    - ### 后续行动：明确的 TODO 或下一步操作。
- 格式锁定：必须严格使用上述 4 个 Markdown 标题和 <core_change> XML 标签。若某节无新信息，在该标题下写"无"。
- 核心摘要：<core_change> 标签内请用一段话完整概括本轮核心增量，允许使用长句，不要遗漏关键技术名词。

请直接输出分析结果，不要包含任何解释或检查过程："""
```

**设计要点**：
- 移除诱发小模型废话的"自我检查"
- 增加 `<example>` 界定符
- 规则后置对抗近因效应

---

## 2. 推理配置与 API 调用

### `ca/config.py`

```python
class Config:
    CA_L1_MAX_TOKENS = 800      # 放宽至 800，确保长对话 OODA 列表不被截断
    CA_L1_TEMPERATURE = 0.1     # 摘要需要高确定性，降低温度
```

### `ca/llm_client.py`

```python
def _call_llm_for_l1(self, prompt: str) -> Tuple[str, str]:
    llm_kwargs = {
        "temperature": Config.CA_L1_TEMPERATURE,
        "max_tokens": Config.CA_L1_MAX_TOKENS,
        # 【关键决策】：彻底移除 stop=["</core_change>"]。
        # 原因：防止 API 提前掐断导致 OODA 列表丢失，依靠 max_tokens 和代码层正则兜底。
    }
    response = self.llm_client.generate(prompt, **llm_kwargs)
    return response.text, response.finish_reason
```

---

## 3. 终极防御性解析器

### `ca/post_process.py`

```python
import re
from typing import Dict, Tuple, Optional

# 【工程决策】：仅覆盖简体中文与英文常见无意义词，不处理繁体（如"無"），作为已知边界。
MEANINGLESS_CORE = {"无", "暂无", "无有效增量", "无新增", "none", "null", ""}

# 【v1.2 核心修复】：使用 re.compile 预编译正则对象。
# 彻底免疫 Markdown 渲染、日志打印或字符串传递过程中反斜杠 \ 被意外吞噬变成 s+ 的幽灵 Bug。
WHITESPACE_PATTERN = re.compile(r'\s+')

# 【v1.2 核心修复】：OODA 提取正则预编译，统一使用 \Z 锚定绝对末尾，兼容标题后直接跟内容
OODA_PATTERNS = {
    "现象与问题": re.compile(r'###\s*现象与问题\s*\n?(.*?)(?=###|<core_change>|\Z)', re.DOTALL | re.IGNORECASE),
    "背景与约束": re.compile(r'###\s*背景与约束\s*\n?(.*?)(?=###|<core_change>|\Z)', re.DOTALL | re.IGNORECASE),
    "决策与共识": re.compile(r'###\s*决策与共识\s*\n?(.*?)(?=###|<core_change>|\Z)', re.DOTALL | re.IGNORECASE),
    "后续行动":   re.compile(r'###\s*后续行动\s*\n?(.*?)(?=###|<core_change>|\Z)', re.DOTALL | re.IGNORECASE)
}

CORE_CHANGE_PATTERN = re.compile(r'<core_change>(.*?)(?:</core_change>|\Z)', re.DOTALL | re.IGNORECASE)


def parse_v1_markdown_xml(llm_output: str) -> Tuple[Dict[str, list], Optional[str]]:
    """v1.2 防御性解析器：处理格式退化、内部污染及尾随噪音。"""
    if not llm_output:
        return _build_empty_result()

    # 1. 物理截断尾随噪音 (Tail Truncation)
    end_tag = llm_output.find('</core_change>')
    if end_tag != -1:
        llm_output = llm_output[:end_tag + len('</core_change>')]

    # 2. 提取 <core_change>
    core_match = CORE_CHANGE_PATTERN.search(llm_output)
    clean_core = "无有效增量"
    if core_match:
        raw_core = core_match.group(1).strip()
        # 【v1.2 重大调整】：彻底移除 KNOWN_HALLUCATION_TAGS 清洗逻辑。
        # 使用预编译的 WHITESPACE_PATTERN，绝对安全地压缩所有换行和多余空格
        clean_core = WHITESPACE_PATTERN.sub(' ', raw_core).strip()

    # 3. 全局语义短路 (Semantic Short-circuit)
    l0_text = None
    if clean_core and clean_core.lower() not in MEANINGLESS_CORE:
        l0_text = _safe_truncate(clean_core, 100)

    # 4. 提取 OODA Markdown 列表
    l1_dict = {"core_change": clean_core}
    for sec_name, pattern in OODA_PATTERNS.items():
        match = pattern.search(llm_output)
        if match:
            items = [line.strip('- \n') for line in match.group(1).split('\n') if line.strip().startswith('-')]
            l1_dict[sec_name] = items if items else ["无"]
        else:
            l1_dict[sec_name] = ["无"]

    return l1_dict, l0_text


def _safe_truncate(text: str, max_len: int) -> str:
    """在标点或空格处安全截断，避免腰斩技术名词"""
    if len(text) <= max_len:
        return text
    trunc = text[:max_len]
    # 【优化】：将英文句号 '.' 移出首选列表，防止 'config.py' 被错误截断
    separators = ['。', '！', '？', '，', '；', '、', '：', ' ', ',', ';', '!', '?', ':']
    for sep in separators:
        idx = trunc.rfind(sep)
        if idx > max_len * 0.6:
            return trunc[:idx] + "..."
    return trunc + "..."


def _build_empty_result() -> Tuple[Dict[str, list], Optional[str]]:
    empty_dict = {
        "core_change": "无有效增量",
        "现象与问题": ["无"],
        "背景与约束": ["无"],
        "决策与共识": ["无"],
        "后续行动": ["无"]
    }
    return empty_dict, None
```

---

## 4. 防弹历史适配器

### `ca/store.py`

```python
import json

def format_previous_summary_for_prompt(l1_text_from_db: str) -> str:
    """将 DB 中的 l1_text 统一转换为 v1.2 提示词期望的 Markdown 格式。"""
    if not l1_text_from_db or l1_text_from_db in ("无", "无有效增量"):
        return "无"
    if l1_text_from_db.strip().startswith('{'):
        try:
            data = json.loads(l1_text_from_db)
            return _legacy_json_to_v1_markdown(data)
        except json.JSONDecodeError:
            pass
    return l1_text_from_db


def _legacy_json_to_v1_markdown(data: dict) -> str:
    mapping = {
        "资源与观察": "现象与问题",
        "事实与约束": "背景与约束",
        "决策与结论": "决策与共识",
        "后续行动": "后续行动"
    }
    md_lines = []
    for old_key, new_title in mapping.items():
        md_lines.append(f"### {new_title}")
        raw_items = data.get(old_key, [])
        if raw_items is None:
            raw_items = []
        if isinstance(raw_items, str):
            items = [raw_items] if raw_items and raw_items != "无" else []
        elif isinstance(raw_items, list):
            items = [str(i) for i in raw_items if i and str(i) != "无"]
        else:
            items = []
        md_lines.extend([f"- {item}" for item in items] if items else ["- 无"])
    core = data.get("core_change", "无有效增量")
    if not isinstance(core, str):
        core = str(core)
    md_lines.append(f"<core_change>\n{core}\n</core_change>")
    return "\n".join(md_lines)
```

---

## 5. C-Stage 消费端与下游防护

### `ca/c_stage.py`

```python
from ca.post_process import parse_v1_markdown_xml
import json

class L1TruncatedException(Exception):
    pass


def process_and_store_l1(dialog_text: str, llm_response: object, turn_id: str):
    llm_output = llm_response.text
    finish_reason = llm_response.finish_reason

    # 【v1.2 核心修复】：双重校验机制
    # 1. 检查 API 官方截断标识
    # 2. 检查物理边界：</core_change> 结尾
    is_truncated_by_api = (finish_reason == 'length')
    is_truncated_by_boundary = not llm_output.strip().endswith('</core_change>')

    if is_truncated_by_api or is_truncated_by_boundary:
        raise L1TruncatedException("LLM output truncated unexpectedly.")

    # 1. 调用 v1.2 解析器
    l1_dict, raw_l0_text = parse_v1_markdown_xml(llm_output)

    # 2. 下游消费端防护：将 None 转换为空字符串
    final_l0_text = raw_l0_text if raw_l0_text is not None else ""

    # 3. 写入 DB
    l1_text_for_db = json.dumps(l1_dict, ensure_ascii=False)
    db.execute("UPDATE turn_cache SET l0_text = ?, l1_text = ? WHERE turn_id = ?",
               (final_l0_text, l1_text_for_db, turn_id))

    # 4. 生成 L0 嵌入（向量化防护）
    if final_l0_text and len(final_l0_text.strip()) > 0:
        l0_embedding = embedding_client.encode(final_l0_text)
        vector_db.upsert(id=turn_id, vector=l0_embedding, metadata={"text": final_l0_text})
    else:
        metrics.increment("ca.l0.skipped_empty")
```

---

## 6. 架构决策记录 (ADR)

| ADR | 决策内容 | 理由与权衡 |
|-----|---------|-----------|
| ADR-01 | 使用 re.compile 预编译正则 | 彻底免疫 Markdown 渲染、日志系统或字符串传递中反斜杠 \ 被意外吞噬的"幽灵 Bug" |
| ADR-02 | 移除 `<core_change>` 内部的标签清洗 | 研发对话中高频出现 XML 标签和泛型，过度防御导致真实技术细节被静默误杀 |
| ADR-03 | 增加 endswith 物理边界双重校验 | 部分 LLM API 在达到 max_tokens 时可能错误返回 finish_reason="stop" |
| ADR-04 | 繁简体无意义词覆盖边界 | MEANINGLESS_CORE 仅覆盖简中与英文，4B 模型输出繁体概率极低 |
| ADR-05 | 英文句号截断风险权衡 | 降低 `.` 优先级，牺牲极小概率的"完美英文断句"，换取 config.py 不被腰斩 |
| ADR-06 | 移除 Prompt 中的"自我检查" | 4B 模型缺乏隐式思维链控制力，极易将检查过程直接打印出来污染输出 |

---

## 7. 监控指标与运维体系

| 指标名称 | 类型 | 告警阈值 | 业务含义 |
|---------|------|---------|---------|
| ca.l1.truncated_fallback | Counter | > 5% / 小时 | 因 max_tokens 截断触发 L-stage 降级的次数 |
| ca.l1.parse_fallback_count | Counter | > 2% / 小时 | 正则未匹配到 `<core_change>` 而触发兜底的次数 |
| ca.l0.skipped_empty | Gauge | N/A | 触发语义短路、跳过向量化的轮次比例 |
| ca.l1.latency_ms | Histogram | P99 > 3000ms | L1 摘要生成端到端延迟 |

---

## 8. 上线 Checklist

- [ ] 确认 ca/post_process.py 中的正则表达式均已使用 re.compile 预编译
- [ ] 确认 ca/c_stage.py 中的 L1TruncatedException 已被外层 L-stage 降级逻辑正确捕获
- [ ] 确认数据库 turn_cache 表的 l0_text 字段已添加 NOT NULL DEFAULT '' 约束
- [ ] 确认 Embedding 客户端在接收到空字符串时不会抛出未捕获异常
- [ ] 确认 Prometheus/Grafana 监控面板已配置上述 4 个核心 Metrics
