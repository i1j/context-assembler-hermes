# 观察 001: SubdirectoryHintTracker 重复文档注入

## 现象

Hermes 内置的 `SubdirectoryHintTracker`（`agent/subdirectory_hints.py`）在访问文件时自动扫描路径及祖先目录，发现 `AGENTS.md` / `CLAUDE.md` / `.cursorrules` 后自动注入上下文。同一文件通过不同路径被多次触发，导致重复内容占据上下文窗口。

### 触发机制

每次 `read_file` 或 `terminal` 访问一个路径 → Hermes 检查该路径及其父目录链 → 有匹配文件则注入 ctx。

### 本次触发链

1. `read_file(ca_assembler/tests/agents.md)` → 发现 `ca_assembler/tests/` + 祖先链，注入测试指南
2. `read_file(projects/context-assembler/docs/AGENTS.md)` → 发现 `projects/` + 祖先链，注入设计文档
3. `read_file(ca/config.py)` → 再次触发 `ca_assembler/ca/` 层扫描

### 发现的重复

| 文档 | 加载次数 | 来源路径 |
|------|---------|---------|
| `AGENTS.md` (Hermes Agent 开发指南) | 2 | `.hermes/hermes-agent/AGENTS.md` + `projects/context-assembler/.hermes/hermes-agent/AGENTS.md` |
| `tests/agents.md` (测试指南) | 2 | `ca_assembler/tests/agents.md` + `projects/context-assembler/tests/agents.md` |

`ca_assembler/tests/agents.md` 和 `projects/context-assembler/tests/agents.md` 是同一个物理文件（CA 插件内嵌的测试目录 vs 项目根目录），路径不同被当成两个文档各自注入，内容一致，白费约 9.5KB。

### 影响

- 152K tokens 中有约 100K+ 是重复文档
- 对话历史 + 工具调用被压缩到剩余空间
- 可能导致 LLM 注意力分散或响应质量下降

### 控制方式

访问文件前可以先 `cd` 到一个不触发扫描的目录，避免跨路径跳跃触发多次。

### 建议修复方向

`SubdirectoryHintTracker` 应做基于内容哈希或路径规范化的去重，避免同一文件通过不同路径被多次注入。

## 发现时间

2026-05-25
