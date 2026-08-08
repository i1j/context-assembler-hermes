# 决策 43：删除 OV wiki/ 镜像（OV design/ 等技术文档为权威源，本地 docs/wiki 为工作副本）

> 版本 v5.0 | 2026-08-08 | 状态：已执行（v5.0 补录精炼轮可行性结论：文档引用链接对齐）
> 触发：用户提出「精炼轮加自动整理 OV 项目文档 wiki 的可行性」→ 评估（结论含「文档引用链接对齐」可行）→ 用户拍板：
> **OV `wiki/` 是本地 docs/wiki 的镜像（冗余副本），应删除；OV design/、AGENTS/ 等技术文档才是权威源**

## 1. 最终裁定（用户 2026-08-07 拍板）

**「OV 中的技术文档才是权威源」**——指 OV `design/`（决策库 34-43）、`AGENTS/AGENTS.md`（完整技术参考）、`changelog` 等。**OV `wiki/` 不是权威**，它是本地 `docs/wiki/` 的镜像副本，应删除。

| 位置 | 角色 | 处置 |
|---|---|---|
| OV `design/`（34-43 + decision-points/ + INDEX.md） | **权威决策库** | ✅ 保留 |
| OV `AGENTS/AGENTS.md` | **完整技术参考**（12KB+ 经验） | ✅ 保留 |
| OV `changelog.md` / `changelog-v6-continuation.md` | 权威变更日志 | ✅ 保留 |
| OV `wiki/`（INDEX/architecture 14/decisions 35） | **本地 docs/wiki 的镜像（冗余）** | ❌ **删除** |
| 本地 `plugins/ca_assembler/docs/wiki/` | 工作副本（git 管理） | ✅ 保留（编辑区） |

## 2. 执行记录（2026-08-07，全部实测）

| 步骤 | 操作 | 验证 |
|---|---|---|
| 1 | `mcp__openviking__forget` recursive 删除 `viking://resources/projects/context-assembler/wiki` | WebDAV GET wiki/INDEX.md → 404；OVFS 路径消失 |
| 2 | OV 权威文档核对 | design/34-43 决策库全部在；AGENTS/AGENTS.md 200；changelog 200 |
| 3 | OV 根目录结构 | AGENTS/、design/、changelog、INDEX.md、test-env、testing 完好，无 wiki/ |

**注**：删除过程曾走偏（误恢复 OV wiki/ 51 文件一次），最终按用户指示重新删除——恢复是错误动作，已纠正。

## 3. 权威源规范（定稿）

- **OV 技术文档（design/ + AGENTS/ + changelog）= 权威源**
- **本地 `docs/wiki/` = 工作副本**（git 管理，编辑区，非权威）
- **OV `wiki/` 镜像已删除，不再重建**
- 精炼轮/脚本不自动整理 OV wiki（不生成文档正文——LLM 语义精度不可靠）；**文档引用链接对齐是可行方向**（L1 结构化维护，代码可精确做）；文档同步走 code-ov-agents-sync 流程（人工驱动）

## 4. 精炼轮改进可行性——文档引用链接对齐（v5.0 补录）

> 2026-08-08 用户纠正补录：当初评估「精炼轮加自动整理 OV 项目文档」时，结论不止「删除镜像」，还确认了 **「文档引用链接对齐」是可行方向**——这是精炼轮/脚本自动整理 OV 文档最有价值且代码可精确做的切入点（L1 结构化、零 LLM 生成）。

### 4.1 可行性评估结论（2026-08-07 会话 msiz9z18twcuvh）

| 方向 | 可行性 | 说明 |
|---|---|---|
| **文档引用链接对齐**（INDEX 链接 / 决策树映射 / 文档互引修正） | ✅ **高可行** | L1 结构化维护——代码精确做、零 LLM；把人工手工维护的 INDEX/映射代码化 |
| INDEX 同步 / changelog 结构化追加 | ✅ 高 | 单向覆盖、mtime 触发 |
| 文档正文生成（LLM 语义内容） | ⚠️ 不可靠 | 用户原则「先考虑哪种容易准确生成」——精度不可靠的字段/功能不加 |
| 残留清理 / 结构修复 | 🟡 需人工确认 | 破坏性操作宁缺勿错 |

### 4.2 已执行实例（OV 结构迁移后引用链接对齐，2026-08-07 实测）

OV 结构迁移（design/ → decisions/ + architecture/ + testing/）后，用户指示「修改链接」「都要修正」，执行的引用链接对齐：

| 文件 | 对齐内容 |
|---|---|
| OV `AGENTS/AGENTS.md` 第 5/81/115 行 | `wiki/decisions/` → `decisions/`；`design/37-41` → `decisions/`；`design/ca-ov-topic-submit.md` → `architecture/` |
| `testing/INDEX.md` | 3 处 `../design/` → `../architecture/` + 维护约定文字 |
| `testing/overview.md` | 3 处 `../design/` → `../architecture/` |
| `topic_manager.py` | `design/decision-points-wiki.md` → `decisions/` |
| 根 `INDEX.md` | 决策 40/41 补 `decisions/` 前缀、补齐 42/43 行、`design/decision-points/` → `decisions/` |

**验证**：修改后全部链接指向存在目标（WebDAV GET 200 / PROPFIND 命中），AGENTS/testing 引用零断链。

## 5. 教训（写入 skill 防再犯）

1. **用户指示含「镜像」时先分清删谁**：「本地 docs/wiki 的镜像——应该删除」= 删 OV `wiki/`（本地副本的镜像）。不要反向理解成「删本地 docs/wiki」或「OV wiki/ 是权威」。
2. **权威源判定看 AGENTS.md**：AGENTS.md 第 86 行「完整项目文档（…Wiki 知识库）在 OpenViking」指 design/ + AGENTS/ 等权威文档；OV `wiki/` 是镜像，与权威文档是两回事。
3. **删除资源用 `mcp__openviking__forget`**（带 recursive），`viking_forget` 只接受 memory 文件——反复用错会浪费时间。

## 6. 相关资源

- OV 权威：`viking://resources/projects/context-assembler/`（design/ + AGENTS/ + changelog）
- 本地工作副本：`plugins/ca_assembler/docs/wiki/`
- 平台坑：skill `ca-reality-graph-model` → `references/ov-resource-sync-pitfalls.md`
