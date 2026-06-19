# decision-points-wiki.md 更新说明

> 本文件记录对 `design/decision-points-wiki.md` 需要做的更新（OV 不支持直接编辑）。
> 对应的更新版 TP-* 页面已上传至 OV：`viking://resources/TP-{001,002,004,006,007}`

---

## 更新 1：替换测试套件对照表

**位置:** 第 1196 行 (`## 测试套件对照表`) 至 第 1225 行 (`## 独立 Wiki 页面` 前)

**原内容 (20 行表格):**

```markdown
## 测试套件对照表

每个测试文件列明其覆盖的决策点，形成双向可追溯图。

| 测试文件 | 覆盖决策点 | 决策类型 |
|---------|-----------|---------|
| ...20 行... |
| `tests/audit/cross_ref_wiki_audit.py` | TP-001, TP-002, TP-006, TP-007 | 交叉验证审计 |

---
```

**替换为:**

```markdown
## 测试套件对照表

测试覆盖的详细对照矩阵、跑法说明、缺口登记 → `tests/INDEX.md`（本地 filesystem）
  
OV 资源:
- `viking://resources/TP-001` — TP-001（话题分割，Tests→`tests/INDEX.md §TP-001`）
- `viking://resources/TP-002` — TP-002（三级话题定级）
- `viking://resources/TP-004` — TP-004（topic_boost）
- `viking://resources/TP-006` — TP-006（水位压力）
- `viking://resources/TP-007` — TP-007（故障安全降级）

所有 19 个测试文件的 `设计决策对照` 节均指向 `tests/INDEX.md`。

---
```

## 更新 2：修改每个 TP-* 节的 Tests 字段

每个 TP-* 节的 `Tests:` 字段改为简写：
- `Tests: → tests/INDEX.md §TP-XXX`

## 更新 3：节点数更新

- `design/decision-points/INDEX.md`: 节点数 122 → 123（新增 `tests/` 子节点）
- `design/INDEX.md`: 新增 `design/tests/INDEX.md` 条目
