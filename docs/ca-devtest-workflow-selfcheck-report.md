# DevTest Workflow 流程自检报告

**报告时间**: 2026-06-15  
**检查对象**: `.reasonix/skills/devtest-workflow.md` 及其引用资源  
**检查方式**: 全局搜索 + OpenViking 查询 + 文件系统遍历  
**触发原因**: 执行重构任务时，Agent 三次违反 skill 铁律（入口跳跃、伪双线、出口检查形同虚设），事后排查发现流程本身存在可用性障碍。

---

## 一、检查结果摘要

| 类别 | 总项 | 可用 | 不可用 |
|------|------|------|--------|
| Skill 主文件正文 | 1 | 1 | 0 |
| References 子文件 | 7 | 0 | **7** |
| Viking 参考资源 | 5 | 1 | **4** |
| 自检工具定义 | 1 | 0 | **1** |

**结论**: Skill 主文件可读，但其引用的 12 个外部资源中 11 个不可达或不存在。流程在关键节点（路由、状态展开、自检）缺乏可操作指引。

---

## 二、缺失资源清单

### 2.1 `references/` 子文件（7/7 缺失）

skill 主文件采用 umbrella 结构，声明以下子文件全部不存在：

| 文件 | 引用时机 | 缺失影响 |
|------|---------|---------|
| `references/routing-and-mechanisms.md` | 路由决策时 / 审查启动前 | 无法按标准流程做路由拆包、Profile 选择、打回闭环 |
| `references/agent-llm-pool.md` | 路由拆包选 Agent 时 | 无 Layer 4 级 LLM 映射表和角色×层级推荐矩阵 |
| `references/self-check-tools.md` | 状态转出前 / 遇异常时 | 9 个自检工具无完整定义，Agent 只能靠主文件摘要的几句话执行 |
| `references/state-requirements.md` | 进入需求开发时 | 无开门动作、Phase 1-3、渐近明细循环、模板、出口裁剪的详细操作 |
| `references/state-technical-design.md` | 进入技术方案时 | 无双线模板和交叉评审机制的详细操作 |
| `references/state-code-building.md` | 进入代码构建时 | 无 TDD 循环、编码铁律、双 Agent 平行的详细操作 |
| `references/state-rest.md` | 验证诊断/交付部署/归档/反思 | 无各状态的完整流程 |

### 2.2 Viking 参考资源（4/5 缺失）

| 资源 | 状态 | 缺失影响 |
|------|------|---------|
| `viking://agent/default/skills/programmer-orchestrator/references/` | ❌ 整个目录不存在 | 通道判定表、初设模式、调试方法、提报自检均不可用 |
| `viking://agent/default/skills/tester-workflow/references/` | ❌ 整个目录不存在 | 执行详解、诊断报告不可用 |
| `viking://resources/devtest-workpackages` | ❌ 0 结果 | 无法按标准拆包目录拆包 |
| `viking://resources/devtest-workflow/profiles/profile-definitions/profiles-defs.md` | ✅ 存在 | 唯一可用的外部引用 |
| `viking://resources/devtest-workflow/profiles/`（12 个 Profile） | ✅ 存在 | 各 Profile 角色定义可用 |

### 2.3 自检工具（定义文件缺失）

主文件定义了 9 个自检工具的名称和一句话描述，但 `references/self-check-tools.md` 不存在，缺少：

- 每个工具的**完整执行步骤**
- 输出物要求（什么算"通过"）
- 不同状态的引用方式
- 工具之间的组合规则

---

## 三、对执行的影响

### 3.1 Agent 违规的根因分析

本次任务中 Agent 三次违反 skill 铁律，不完全是 Agent 主观问题——流程本身缺乏**执行约束机制**：

| 违规 | Agent 问题 | 流程问题 |
|------|-----------|---------|
| **入口跳跃**（跳过需求开发直接编码） | 追求效率的默认行为 | skill 无"锁住编码工具"的机制，无强制检查点 |
| **伪双线**（自己分饰两角做评审） | 不理解"双线平行"定义 | 无子 Agent 如何 spawn 的模板指引；无"评审必须由独立方执行"的强制机制 |
| **出口检查形同虚设**（列空勾跳过） | 把技能当参考文档而非契约 | 检查清单不可逐项打勾（无强制验证）；"已完成"无验证标准 |

### 3.2 具体操作障碍

1. **技术方案阶段**：`state-technical-design.md` 不存在 → 无 template 可用 → Agent 自由发挥
2. **多视角审查**：无独立 Reviewer Agent 的 prompt 模板 → Agent 不知道如何 spawn Reviewer
3. **测试方案阶段**：无独立 Tester Agent 的 prompt 模板 → Agent 自己写测试方案
4. **出口检查**：`self-check-tools.md` 不存在 → Agent 凭印象执行自检，无标准可对照

---

## 四、建议

### 4.1 短期（本轮问题修复）

将 skill 裁剪为**自包含版本**，把最关键的三个子文件内容直接写入主文件，消除外部引用依赖：

| 优先级 | 内容 | 理由 |
|--------|------|------|
| P0 | 自检工具完整定义（9 个工具） | 每次状态转出都需要，无定义则自检形同虚设 |
| P1 | 需求开发详细流程 | 入口阶段，最容易被跳过 |
| P2 | 技术方案模板 | 双线并行依赖此模板 |

### 4.2 中长期

1. 补写 7 个 `references/` 子文件
2. 在 OpenViking 中重建 programmer-orchestrator 和 tester-workflow 的 reference 资源
3. 考虑为关键约束（如"禁止跳跃到编码"）增加工具级执行检查

---

## 五、附录：skill 主文件现有可用内容

以下内容可直接使用，不需要依赖外部资源：

- ✅ **第一条铁律**：任何任务都必须从需求开发阶段进入（含示例）
- ✅ **第二条铁律**：路由时拉开双线（含图示）
- ✅ **路由入口步骤**：强制入口检查 → 评估任务规模 → 拉开双线 → 标注边界
- ✅ **状态主干**：开发线 6 状态 + 测试线 3 状态
- ✅ **路径命名规则**：4 种路径模板
- ✅ **出口检查清单**：6 个状态转出共 22 项检查
- ✅ **9 个自检工具名称及一句话描述**（但缺完整定义）
