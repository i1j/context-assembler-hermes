# Bug 卡片 #7: OODA 解析器 — core_change 前导冒号未剥离

## 现象
LLM 基准测试项目中，通过 `deepseek-v4-pro` 生成的标准参考输出（如 `核心摘要：对话中多次查询...`）经 OODA Parser 解析后，`core_change` 字段值带前导中文冒号 `：对话中多次查询...`。

## 失败样例
```
// 模型输出
核心摘要：对话中多次查询美伊冲突与AI进展，但搜索工具返回过时信息。

// _extract_sections 提取的 content
：对话中多次查询美伊冲突与AI进展，但搜索工具返回过时信息。

// _build_result 产出的 core_change
：对话中多次查询美伊冲突与AI进展，但搜索工具返回过时信息。
```

`_CORE_CHANGE_RE` 正则兜底路径不受影响（正则本身捕获冒号后的内容）。

## 影响范围
- **所有**经过 `OODAParser.parse()` 解析的 L1 摘要 — C-stage 产出、A-stage 检索、benchmark 评估均受影响
- 生产环境 L1 摘要的 `core_change` 字段长期带前导冒号，但 L0（前 100 字符截取）和向量嵌入略微偏移，实际影响低
- `clean_increment` 不会过滤冒号，冒号不会被判定为空或误去重
- 基准测试中 PRO vs 被测模型的 ROUGE / BERTScore 因此偏移字符级对齐

## 根因
`_extract_sections` 的 anchor 正则只捕获字段标题文本，分隔符 `：` 不进入捕获组：

```
anchor_re = r"(?:^|\n)\s*((?:{}))[ \t]*[:：]"
#                                  ~~~~~~ 这部分不进入 group(1)
```

字段标题匹配后，`m.end(1)` 指向标题末尾（`核心摘要` 之后，冒号之前）。从 `text[m.end(1):next_start]` 提取的内容字符串以 `：` 开头。原代码只有 `.strip()`，无法移除中文全角冒号。

## 修复
`ca/ooda_parser.py:70` — content 提取后追加 `.lstrip(":：　 ")`：

```diff
- content = text[end:next_start].strip()
+ content = text[end:next_start].strip().lstrip(":：　 ")
```

同时更新 benchmark 侧 `evaluators/ooda_parser.py`，改为从 CA 模块直接 import，不再维护独立拷贝。

## 复现
```bash
cd ~/projects/context-assembler
source venv/bin/activate
python3 -c "
import sys
sys.path.insert(0, '.')
from ca.ooda_parser import OODAParser
p = OODAParser(emb_client=None)
r = p.parse('核心摘要：正常内容\n资源与观察：\n- 项目A\n- 项目B\n事实与约束：\n- 无\n决策与结论：\n- 无\n后续行动：\n- 无')
print('core_change:', repr(r.get('core_change','')))
# 修复前: core_change: '：正常内容'
# 修复后: core_change: '正常内容'
"
```

## 涉及模块
- `ca/ooda_parser.py` — `_extract_sections` L70 (修复)
- `evaluators/ooda_parser.py` — 改为 CA 薄封装 (已修改)

## 关联记录
- `docs/changelog.md` — v4.3.2‑ooda‑fix（调查）→ v4.4.0‑ooda‑fix（修复）

## 修复记录
- **2026-06-03** — `ca/ooda_parser.py:70` 已应用修复（`.lstrip(":：　 ")`）
- 验证通过：`core_change` 不再带前导 `：`/`:`/`　`

## 优先级
中 — 性能/正确性影响小，但基准测试已使用修复版重新评估，避免持续漂移
