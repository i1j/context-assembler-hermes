# CA ↔ OpenViking 话题自动提交 — 技术方案

## 1. 目标

CA 检测到话题切换时，将**已完成话题的全部 L1 cache** 提交给 OpenViking，由 OpenViking 自动完成语义摘要（`.abstract.md` / `.overview.md`）和向量化，使已结束话题可被 `viking_search` 检索。

## 2. 设计约束

| 约束 | 原因 |
|------|------|
| CA 与 OpenViking 共享同一套大模型 | 避免模型争用，OpenViking 优先级最低 |
| 不阻塞 Hermes 主流程 | 提交是 fire-and-forget 侧效应 |
| 不可丢数据 | 提交失败必须自动重试，无丢弃阈值 |
| 零状态跟踪 | 不使用计数器、重试队列、已提交标记集 |
| 话边界稳定后触发 | LLM 上下文已提交云端后再触发，不影响 LLM call 延迟 |

## 3. 触发时序

```
pre_llm_call (A-stage)          _run_c_stage (L-stage, daemon thread)
  │                               │
  ├→ topic_seg                     │
  │  turn_to_topic, topic_data     │
  │                                │
  ├→ [检测] 话题切换               │
  │  if last != current:           │
  │    packages old topic L1       │
  │    → _pending_ov_submit        │
  │  _last_topic_id = current      │
  │                                │
  ├→ 组装上下文 → 返回 Hermes      │
  │                                │
  └→ Hermes 提交 ctx 到云端 LLM    │
                                    │
  post_llm_call (C-stage)           │
    ├→ flush_tool_buffer()          │
    ├→ 恢复快照                     │
    ├→ process_turn_async()         │
    │   └→ _run_c_stage (后台线程)  │
    │        ├→ _call_llm_for_l1()  │
    │        ├→ embed()             │
    │        ├→ write_turn()        │
    │        ├→ backfill.trigger()  │
    │        ├→ _fire_ov_submit()   │ ← [新] L-stage 最末尾
    │        │   POST content/write │
    │        │   HTTP 200 → clear   │
    │        │   HTTP !200 → retain │
    │        └→ backfill.trigger()  │
```

## 4. 数据流

### 4.1 A-stage：检测 + 打包

位置：`ca/__init__.py` → `_compute_assemble_plan()` → topic segmentation 段后

```python
# 检测条件：
#   1. _pending_ov_submit is None（无待提交任务）
#   2. turn_to_topic 非空（有话题分割结果）
#   3. 最新对话轮的 topic_id != 上次记录的 _last_topic_id（话题已切换）
#   4. 旧话题非 BG 轮（is_bg=False）
if _pending_ov_submit is None and turn_to_topic and l1_texts:
    _current_topic_id = turn_to_topic[latest_turn]
    if _last_topic_id is not None and _current_topic_id != _last_topic_id:
        old_topic = topic_data[_last_topic_id]
        if old_topic and not old_topic["is_bg"]:
            # 打包：l1_texts[turn_index] + tool_group_l1[(turn,sub)]
            _pending_ov_submit = _TopicSwitchData(
                topic_id=_last_topic_id,
                turn_indices=old_topic["turn_indices"],
                agg_text=old_topic["agg_text"],
                l1_texts={t: l1_texts[t] for t in turn_indices if t in l1_texts},
                tool_group_l1={"({t},{s})": v for ... old topic turns ...},
            )
    _last_topic_id = _current_topic_id
```

关键变量：
- `_last_topic_id: Optional[int]` — 上次检测到的最后话题 ID（engine 属性）
- `_pending_ov_submit: Optional[_TopicSwitchData]` — 待提交数据（engine 属性）

### 4.2 L-stage：提交 + 重试

位置：`ca/__init__.py` → `_run_c_stage()` → finally 块末尾

```python
# 提交逻辑（L-stage 末尾，CA 所有模型调用已结束）：
if _pending_ov_submit is not None:
    if _fire_ov_submit(_pending_ov_submit):  # HTTP POST
        _pending_ov_submit = None            # 成功才清
    # 失败：保留 _pending_ov_submit，下次 L-stage 继续试
```

### 4.3 HTTP 提交

位置：`ca/__init__.py` → `_fire_ov_submit(_TopicSwitchData) -> bool`

```python
def _fire_ov_submit(self, ts: _TopicSwitchData) -> bool:
    # 1. 组装话题 Markdown
    # 2. POST /api/v1/content/write
    #    URI: viking://user/{user}/memories/ca_topics/topic_{id}_{ts}.md
    # 3. 返回 True (HTTP 200) / False (其他)
```

## 5. 内容格式

提交到 OpenViking 的话题文档示例：

```markdown
# Topic 2 — CA Conversation Summary

> Turns: 5-9

## Overview

用户从项目讨论切换到部署方案，涉及 Docker 配置、环境变量管理和 CI/CD 流程设计...

## Dialogue Turn 5

部署方案选定 Docker Compose，要求配置 PostgreSQL、Redis 和 Nginx
> new_materials: docker-compose.yml 定义服务

## Dialogue Turn 6

配置 PostgreSQL 连接参数，讨论持久化方案
> consensus: 使用 named volume

## Tool Groups

### (6,1)
使用 file_tool 创建 docker-compose.yml 并写入 PostgreSQL 配置
```

OpenViking 收到后自动：
1. 存储原始 `.md` 文件
2. SemanticProcessor → 生成 `.abstract.md`（L0 摘要）+ `.overview.md`（L1 概述）
3. EmbeddingQueue → 向量化 → 可通过 `viking_search` 检索

## 6. 重试机制

**必须成功为止，没有丢弃阈值。** 设计依据：

| 场景 | 行为 |
|------|------|
| HTTP POST 网络超时 | `except Exception` 捕获 → 仅打 warning → 保留 `_pending_ov_submit` |
| OpenViking 返回非 200 | `else` 分支 → 仅打 warning → 保留 |
| OpenViking 停机一周 | 每轮对话的 L-stage 都试一次 → 恢复后首轮成功 |
| 进程重启 | `_pending_ov_submit` 随 engine 销毁而丢失（可接受——话题已过去） |
| 新会话 `/new` | engine 重置 = `_pending_ov_submit` 清空 |

重试没有队列、没有计数器，因为：
1. `_pending_ov_submit` 数据是冻结的 Python dict，内存占用量忽略不计
2. L-stage 每轮对话都跑一次（用户说话频率 ≈ 重试频率）
3. API 调用 (content/write) 是幂等的（`mode: "create"` + 时间戳 URI）

## 7. 边界处理

| 场景 | 处理 |
|------|------|
| 首轮对话 | `_last_topic_id is None` → 跳过 |
| BG 话题完成 | `is_bg=True` → 跳过 |
| 话题无 L1 数据 | `l1_texts` 为空 → 跳过 |
| 话题只有 1 轮 | 单轮话题 → 正常提交 |
| 连续多发重试 | 检测块被 `_pending_ov_submit is None` 阻截 → 不会多包 |
| 重试期间又有新切换 | 新切换不会被检测（已有 pending）→ 旧话题先完成再检测新话题 |
| LLM call 失败 | 不进入 post_llm_call → L-stage 不跑 → 不提交 |
| OpenViking disabled | `CA_OV_SUBMIT_ENABLED=0` / `Config.CA_OV_SUBMIT_ENABLED=False` |

## 8. 改动清单

### 新增（`ca/__init__.py`）

```python
@dataclass
class _TopicSwitchData:
    """A-stage 检测到话题切换后暂存的数据，供 L-stage 提交 OpenViking。"""
    topic_id: int
    turn_indices: List[int]
    agg_text: str
    l1_texts: Dict[int, str]
    tool_group_l1: Dict[str, str]
```

### 修改

| 文件 | 位置 | 改动 |
|------|------|------|
| `ca/__init__.py` | `ContextAssembler.__init__` | 加 `_last_topic_id`, `_pending_ov_submit` |
| `ca/__init__.py` | `_compute_assemble_plan()` topic seg 段后 | 话题切换检测 + 打包 |
| `ca/__init__.py` | `_run_c_stage()` finally 末尾 | `_fire_ov_submit` 调用 |
| `ca/__init__.py` | 新增 `_fire_ov_submit()` | HTTP POST + 成功/失败处理 |
| `ca/config.py` | 末尾 | `CA_OV_SUBMIT_ENABLED` 配置项 |

### 零改动

- `plugins/ca_assembler/__init__.py`（plugin 适配层）— 不碰
- 原有测试套件 — 零回归

## 9. 配置

环境变量（默认启用）：

```bash
CA_OV_SUBMIT_ENABLED=1    # 设为 0 关闭
```

连接信息复用 Hermes 现有的 OpenViking 配置：

```bash
OPENVIKING_ENDPOINT=http://localhost:1933
OPENVIKING_USER=tester
```

URI 路径：`viking://user/{user}/memories/ca_topics/topic_{id}_{timestamp}.md`
