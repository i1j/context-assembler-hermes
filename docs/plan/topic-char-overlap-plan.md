# 话题分割技术方案：todo 字符级合并

## 背景

当前话题分割有两条合并路径：
1. **todo 链合并**（v4.6.0 原始设计）：`has_todo_overlap AND J ≥ threshold`，需要两个连续实义轮之间 todo 字段与 core_change/new_materials 精确字符串匹配
2. **Jaccard 独立合并**（v5.2.1 补充）：`J ≥ jaccard_merge_threshold`，不依赖 todo 重叠，用 Jaccard 阈值独立决定合并

问题：
- 路径 2 是粗糙的统计近似，已被识别为「0.18 的阈值切割通道」，决定删除
- 路径 1 在 v4.6.0 测试时通过，但后续 F-stage 提示词优化使 Fct 语言更自然多样，**精确字符串匹配在 116 对 S→S 中 0 次命中**

实证数据表明，CJK 字符级重叠（≥3 个共享字符）可以准确区分同话题延续 vs. 真正的话题切换。

## 目标

恢复「todo 链 + Jaccard 辅助 + 20K 尾区保底」的原始设计。具体：

1. **删除** Jaccard 独立合并路径（`elif j >= jaccard_merge_threshold`）
2. **放宽** todo_overlap 的匹配粒度：从精确字符串匹配改为 CJK 字符集重叠检测
3. **保持** JACCARD_ENTRY=0.03 / JACCARD_CHAIN=0.04 不动（辅助门禁，防误合）
4. **清理** 废弃配置项（CA_TOPIC_JACCARD_MERGE、自适应阈值体系）

## 算法

### 新 todo_overlap 计算

```python
def _todo_char_overlap(prev_todo, curr_fields) -> bool:
    """
    检测上一轮 todo 与当前轮 core_change/new_materials 的 CJK 字符重叠。
    
    如果共享字符 ≥ 3 个（来自不同词间），视为语义连续 -> todo_overlap=True
    
    Args:
        prev_todo: list[str] — 上一轮的 todo 字段
        curr_fields: dict — 当前轮的 Fct 字段
    Returns:
        bool
    """
    # 提取所有 CJK 字符（去重）
    prev_chars: Set[str] = set()
    for item in prev_todo:
        if isinstance(item, str):
            for ch in item:
                if '\u4e00' <= ch <= '\u9fff':
                    prev_chars.add(ch)
    
    if not prev_chars:
        return False
    
    # 从 core_change + new_materials 提取 CJK 字符
    curr_texts = []
    core = curr_fields.get("core_change", "")
    if isinstance(core, str):
        curr_texts.append(core)
    new_mat = curr_fields.get("new_materials", [])
    if isinstance(new_mat, list):
        curr_texts.extend(str(v) for v in new_mat)
    
    curr_chars: Set[str] = set()
    for text in curr_texts:
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                curr_chars.add(ch)
    
    if not curr_chars:
        return False
    
    common = prev_chars & curr_chars
    return len(common) >= 3  # 阈值，后续实验可调
```

### 为什么是 ≥3

实证数据：所有同话题延续对（T16→T17, T19→T20, T22→T23, T36→T37, T51→T52）都有 ≥4 个共享 CJK 字符。所有话题切换对（T4→T5, T17→T18, T27→T28）共享字符 <3 或为 0。3 是一个干净的分隔线。

## 改动范围

### `ca/__init__.py`

1. **删除 R2 第三条路径**（lines 741-743）：
```python
-                elif j >= jaccard_merge_threshold:
-                    # Jaccard 独立合并路径：不依赖 todo_overlap，用于长对话同话题扩展
-                    _extends_chain(turn)
```

2. **替换 todo_overlap 计算**（lines 713-732）：
```python
-                # todo 重叠检测
-                prev_todo = set()
-                if prev_fields:
-                    todo_val = prev_fields.get("todo", [])
-                    if isinstance(todo_val, list):
-                        prev_todo = set(str(v) for v in todo_val)
-                    elif isinstance(todo_val, str):
-                        prev_todo = {todo_val}
-                
-                curr_core = set()
-                if curr_fields:
-                    core = curr_fields.get("core_change", "")
-                    if isinstance(core, str):
-                        curr_core.add(core)
-                    new_mat = curr_fields.get("new_materials", [])
-                    if isinstance(new_mat, list):
-                        curr_core.update(str(v) for v in new_mat)
-                
-                todo_overlap = prev_todo & curr_core
-                has_todo_overlap = len(todo_overlap) >= 1
+                # todo 字符重叠检测（CJK 字符级，非精确字符串匹配）
+                has_todo_overlap = self._todo_char_overlap(prev_fields, curr_fields)
```

3. **新增辅助方法**：
```python
def _todo_char_overlap(self, prev_fields, curr_fields) -> bool:
    """检测上一轮 todo 与当前轮 core_change/new_materials 的 CJK 字符重叠。"""
    if not prev_fields or not curr_fields:
        return False
    prev_todo = prev_fields.get("todo", [])
    if isinstance(prev_todo, str):
        prev_todo = [prev_todo]
    if not prev_todo:
        return False
    
    # 提取 prev.todo 的 CJK 字符
    prev_chars = set()
    for item in prev_todo:
        for ch in str(item):
            if '\u4e00' <= ch <= '\u9fff':
                prev_chars.add(ch)
    if not prev_chars:
        return False
    
    # 提取 curr.core_change + curr.new_materials 的 CJK 字符
    curr_chars = set()
    core = curr_fields.get("core_change", "")
    if isinstance(core, str):
        curr_chars.update(ch for ch in core if '\u4e00' <= ch <= '\u9fff')
    new_mat = curr_fields.get("new_materials", [])
    if isinstance(new_mat, list):
        for item in new_mat:
            curr_chars.update(ch for ch in str(item) if '\u4e00' <= ch <= '\u9fff')
    
    if not curr_chars:
        return False
    
    return len(prev_chars & curr_chars) >= 3
```

4. **清理自适应阈值体系**（如果确认不再使用）：
   - 删除 `_load_start_threshold()` 方法
   - 删除 `persist_ideal_threshold()` 方法
   - 删除 `_compute_ideal_threshold()` 方法
   - 删除 `_load_topic_meta()` 方法
   - 删除 `_save_topic_meta()` 方法
   - 删除 `__init__` 中 `_topic_jaccard_threshold` 初始化（line 194）
   - 删除 `__init__` 中 `_ideal_threshold_this_session` 字段（line 191）
   - 删除 `session_start` 日志中的阈值输出
   - 删除 `on_session_reset` 中的 `persist_ideal_threshold()` 调用

### `ca/config.py`

- 删除 `TOPIC_JACCARD_MERGE` 配置项
- 删除 TOPIC_JACCARD_MERGE 相关 `_YAML_DEFAULTS` 和 settings.yaml 同步

### `ca/settings.yaml`

- 删除 `CA_TOPIC_JACCARD_MERGE` 条目

### 单元测试

新增测试用例 `test_v460.py` 中：
- 旧 todo_exact_match 测试 → 加上 CJK 字符级匹配测试
- 新 `_todo_char_overlap` 方法单元测试（≥3 字符命中、<3 字符不命中、空输入、非 CJK 输入）
- 删除 Jaccard merge threshold 相关测试

## 验证预期

在分析过的 8 个会话（116 对 S→S）上：

| 对 | 原算法（精确匹配） | 新算法（CJK ≥3） | 预期结果 |
|---|---|---|---|
| T16→T17 `更新生成路径文档`→`生成路径由规则决定` | ❌ 不合并 | ✅ 合并 (共享4字符) | 归入同话题 |
| T19→T20 `实施参数截断规则`→`撤回参数截断规则` | ❌ 不合并 | ✅ 合并 (共享6字符) | 归入同话题 |
| T22→T23 `设计工具类型摘要规则`→`发现摘要逻辑...` | ❌ 不合并 | ✅ 合并 (共享6字符) | 归入同话题 |
| T36→T37 `监控终端输出结构变化`→`结构化输出已具雏形` | ❌ 不合并 | ✅ 合并 (共享5字符) | 归入同话题 |
| T4→T5 `检查...`→`_shutdown...` | ❌ 不合并 | ❌ 不合并 (0字符) | 正确分裂 |
| T17→T18 `标记工具轮类型`→`L1工具输出截断` | ❌ 不合并 | ❌ 不合并 (0字符) | 正确分裂 |

**预期效果：** 116 对中约 15-20% 获得合并，形成有意义的 2-5 轮话题组。其余维持单轮话题，靠 20K 尾区保质量。

## 待实验参数

- **CJK 重叠阈值 3**：需在更多会话上验证是否为最佳分隔线
- **JACCARD_ENTRY 0.03/CHAIN 0.04**：todo_overlap 变宽松后，这些 Jaccard 门禁可能需要同步调整（避免宽松 todo + 宽松 Jaccard = 假合并放大）
- **是否保留 Jaccard 辅助门禁**：如果 CJK 字符级 todo_overlap 足够可靠，Jaccard 辅助检查可能冗余
