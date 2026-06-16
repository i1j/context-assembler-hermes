# F-stage 重构 + 术语重命名 — 实施记录

## 日期
2026-06-15

## 版本对照

| 提交 | 阶段 | 说明 |
|------|------|------|
| `138ae8c` | 前置 | 删除旧死代码（旧buffer/conv_encoding/assemble路径） |
| `260000d` | Phase 1 | 术语重命名 L2/L1/L0→Elm/Fct/Hdl, C-stage→F-stage |
| `6e1bfe5` | Phase 2 | F-stage 从 DB 读 Elm，不再依赖函数参数 |
| `9dd4893` | Phase 3+4 | stage_tag 独立 + thought 截断修正 |

## Phase 1 详解 — 术语重命名

### 方法名

| 旧名 | 新名 | 位置 |
|------|------|------|
| `_run_c_stage` | `_run_f_stage` | ca/__init__.py |
| `process_turn_async` | `process_turn_f_stage` | ca/__init__.py, __init__.py(插件适配器) |
| `_call_llm_for_l1` | `_call_llm_for_fct` | ca/__init__.py, ca/lstage.py |
| `_get_previous_l1` | `_get_previous_fct` | ca/__init__.py |
| `_update_l1_v5` | `_update_fct_v5` | ca/__init__.py |
| `_format_l1_for_display` | `_format_fct_for_display` | ca/__init__.py |
| `_is_valid_summary` | `_is_valid_fct` | ca/__init__.py |
| `_L1_DEBUG_PATTERNS` | `_FCT_DEBUG_PATTERNS` | ca/__init__.py |
| `read_l1_v5` | `read_fct_v5` | ca/store.py, __init__.py |
| `update_seq0_l1_v5` | `update_seq0_fct_v5` | ca/store.py, ca/__init__.py |
| `L1TruncatedException` | `FctTruncatedException` | ca/__init__.py, ca/lstage.py |
| `L1_GENERATION_PROMPT` | `FCT_GENERATION_PROMPT` | ca/prompts.py, ca/__init__.py |

### 局部变量

| 旧名 | 新名 |
|------|------|
| `prev_l1` | `prev_fct` (函数参数/变量) |
| `l1_str` | `fct_str` |
| `Elm` | `elm_text` (仅 F-stage 相关处) |
| `Hdl` | `hdl_text` (仅 F-stage 相关处) |
| `l0_emb` / `l1_emb` | `hdl_emb` / `fct_emb` |
| `l1_dict` | `fct_dict` |
| `parser_hdl` | 新增变量名 |
| `stage_tag` | 新增变量名 |

### 日志/注释/统计

| 旧名 | 新名 |
|------|------|
| `C‑stage` / `CStage` / `C-stage` | `F‑stage` / `FStage` / `F-stage` |
| `ca.l1.` 指标 | `ca.fct.` |
| `self.stats.truncated_fallback` | `self.stats.fct_truncated_fallback` |
| `self.stats.l1_latency_ms` | `self.stats.fct_latency_ms` |

### DB 列名

未改。`Fct` / `Hdl` 在 SQLite 中保持原名，Python 层用别名访问（`fct_text`/`hdl_text` 作为函数参数名）。

## Phase 2 详解 — F-stage 数据源改为 DB

### 改动前
```python
def process_turn_async(self, user_message, assistant_response, conversation_history):
    elm_text = f"User: {user_message}\nAssistant: {assistant_response}"
    prev_l1 = self._get_previous_l1()
    # ... 传到 _run_c_stage
```

### 改动后
```python
def process_turn_f_stage(self, turn_index):
    # 只接收 turn_index，由 post_llm_call_v5 提供
    thread = Thread(target=self._run_f_stage, args=(session_id, turn_index, bg_review))

def _run_f_stage(self, session_id, turn_index, bg_review=False):
    rows = read_turn_elm_rows(self.store, session_id, turn_index)
    # 从 DB 拼 elm_text = 所有 seq 的 content
    prev_fct = read_prev_fct(self.store, session_id, turn_index)
```

### 新增 store 函数
```python
def read_turn_elm_rows(store, session_id, turn) -> list:
    """返回 [(seq, role, content, tool_name, tool_call_id), ...]"""

def read_prev_fct(store, session_id, turn) -> str:
    """读前一轮 seq=0 的 fct_text（l1_text）"""
```

### 插件适配器调用变更
```python
# 改前
engine.process_turn_f_stage(user_message, assistant_response, history_copy)
# 改后
engine.process_turn_f_stage(turn)  # turn = engine._current_turn
```

## Phase 3 详解 — stage_tag 独立

### prompt 变更
- 旧: `<core_change>` 内嵌 `【已实施】xxx`，`_extract_l0` 从 core_change 首句截断
- 新: `<stage_tag>` 独立标签，`<core_change>` 只含内容不含状态前缀

### prompt example
```
<stage_tag>
已实施
</stage_tag>
<core_change>
临时扩容数据库连接池至200，并排查慢查询日志
</core_change>
```

### 解析器变更
- `post_process.py`: 新增 `STAGE_TAG_PATTERN` 正则
- `parse_v1_markdown_xml`: 提取 stage_tag 写入 `l1_dict["stage_tag"]`
- `clean_increment`: 保留 `stage_tag` 到 cleaned 输出
- `_run_f_stage`: 移除旧的 `core_state` 处理（`parse_core_change_state` 不再有效）

### Fct JSON 新格式
```json
{
  "core_change": "临时扩容连接池至200...",
  "stage_tag": "已实施",
  "new_materials": [...],
  "objective_facts": [...],
  "consensus": [...],
  "todo": [],
  "_assemble_status": 0
}
```

## Phase 4 详解 — thought 截断修正

### 旧逻辑
```python
for sep in ("\n\n", "。", "！", "？", ".", "!", "?"):
    cut = text.find(sep)
    if cut != -1 and cut <= 90:           # bug: 只取第一个句尾，≤90 才返回
        return text[:cut + len(sep)]
return _safe_truncate(text, 100)          # fallback 硬截断
```

**问题**: 找到第一个句号就返回，不管它前面才几个字。

### 新逻辑
```python
sentences = re.split(r'(?<=[。！？.!?])\s*', text)
result = ""
for s in sentences:
    s = s.strip()
    if not s:
        continue
    if len(result) + len(s) > 100 and result:
        return (result + " " + s).strip()  # 超 100 时含当前句返回
    if result:
        result += " "
    result += s
return result if result else _safe_truncate(text, 100)
```

**效果**: 句子1(30字) + 句子2(50字) + 句子3(40字) → 累计120 > 100 → 返回全部 3 句

## 测试结果

全部 29 个测试通过，在所有 4 个阶段均保持 29/29。

## 明天测试清单

- [ ] 完整 Hermes 集成测试（跑一个真实对话，确认 F-stage 写入 DB）
- [ ] 新 prompt 格式的 LLM 输出解析验证
- [ ] thought 截断边界测试（刚好 100、超 100、多个句子）
- [ ] stage_tag 缺失/错误的降级处理
- [ ] 旧 DB 中仍含状态前缀的 L1 数据兼容性
