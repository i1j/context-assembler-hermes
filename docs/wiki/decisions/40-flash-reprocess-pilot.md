# 40：flash 全链路重跑 pilot 任务书（winker → tester）

> 状态：✅ **tester 全量完成（2026-08-05，最终版）**——129 sessions/470 块/857 strand 全链路跑通，独立 DB（tester/ca_cache/flash_pilot.db）零污染生产库。
> **最终版验证结论（第 3 轮构建，字段/归并综合最优）**：
> ① strand：hdl ≤30 字规范率 99.4%（≥99% 门槛 ✓）；ooda 四组完整率 38.7%（宁缺勿错基线）；query_text 500 截断生效
> ② reality：**179 reality**（单 strand 47%、avg 4.7、**max=39**）；字段完整 178/179（1 条 goals 空 = 完结态如实报告）；**0 孤儿 reality**
> ③ 注入快照（决策 39）：**448/448 块全命中、0 空注入**
> ④ **归属合理性人工评估（41 条全量复评：38 大成员 + 10 随机）：37 明确合理 + 4 边缘 = 90-100% ≥ 80% 门槛 ✓**；边缘问题已处理（2026-08-05）：S273→reality 29（thought 注入归 Fct 机制）、S311/S312→138（连接池归连接池稳定性）、S434/S435→60（graphify 归图谱重构）、reality 110 改名「CA压缩边界拷贝管道重构与原子替换」+ 详情重生成（原 graphify 名称错配）；S571 原评估误判纠正（内容=agents.md index 评估，属 reality 131 同工作线，不移）；reality 128 补全（既有内容层漏 hdl）——**处理后字段完整 179/179（100%）**，s2r↔source 全一致，备份 flash_pilot.db.bak_pre_merge
> ⑤ **全库手工精炼轮（2026-08-05，179→140 reality，原则=宁并不分[用户纠正]）**：
> **第一批（宁分不并取向，19 组/57 strand）**：无实质内容桶 R71+R73；双写调研 R20→22；
> Fct/Hdl 设计 R33→32；话题切换降级 R43→27、水位选型 R9→27；graphify R139+R144→60；
> PG/Redis 清理 R120+123→121；merge 语义化 R167→164、旧 wiki R168→166；主题重跑
> R173+176+177→174；话题检测 R146→145；MCP 健康/列表/清单 R93+109+111→117；插件 git
> R94→105 + S337→39；Fct 事务 R141+142+165→137；对话历史 R36→106；REL 格式 R51→30；
> 空值兜底 R98→59；agents R131→24。
> **第二批（宁并不分纠正，13 组/71 strand，边界全部倾向合并）**：内容级分割 R116→164、
> 合并流水线 R163→164、wiki 合并 R166→164（**R164=55 成员**：S740-800 merge 机制大工作线）；
> 图模型 R178+R179 合并改名「CA图模型与theme注入归并机制设计」（38 成员）；graphify MCP
> R92→117（18）；Hermes Studio API R97→78（8）；配置恢复 R115→39、重启生效 R125→39（21）；
> SKILL.md R50→35（2）；20260619 文档整理 R2+R5+R6→R3（5，同 session 连续链）；
> 设计协议 R37→27（34）。**不并**（确非同一工作线）：数据清空 R7、Bug 排查 R8、独立单 strand。
> 两批共 32 组/128 strand 移动、25 目标详情重生成（内容层 0 失败）、备份
> .bak_pre_refine/.bak_pre_refine2。**结果：140 reality、字段完整 140/140（100%）、
> 0 孤儿、s2r↔source 140/140、单 strand 44%（61 个独立工作线）、avg 6.0、max=55（R164）**
> **第三批（宁并不分深挖，29 组/31 strand，140→111）**：单 strand 碎片全部按
> 承接对象并入——文档整理 R4+R72→3；话题检测 R17+R54→145；git 历史 R25→105；
> memory 清理 R26→119；Hdl/turn_stream R31+R114→29（42）；FAR R63→79；压缩引擎
> R66→110；CA 插件安装/注册 R70+R113→21；graphify 家族 R82+83+87+88+89→60（35）；
> 无实质桶 R100→71；技能库 R101→19；消息计数 R124→22（32）；引擎 TTL R133→134；
> CA/OV 整合 R136→80；话题摘要 R74+R140→150（36）；追溯表 R148→149；连接池 R153→138；
> 系统稳定 R161→75；theme 机制 R172→178（39）；历史验证 R99→59。**保留 33 个单 strand**
> （确认无承接对象：数据清空 R7、Bug 排查 R8、Replace 语义 R12、turn_plan R14、
> C-stage 核查 R15、代码冗余 R16、委派 R23、A-stage R61、废弃代码 R68、变量名 R69、
> OV 状态 R104、会话链接 R107、上下文来源 R108、调试技能 R118、配置压缩 R122、
> action R128、文件上传 R129、线程 R132、会话管理 R152、新领域 R156-160、v7.0 R162、
> gateway R169、环境 R170、复制 winker R175 等）。**结果：111 reality、字段完整
> 111/111（100%）、0 孤儿、s2r↔source 111/111、单 strand 30%（33 个）、avg 7.5、
> max=55（R164）、R29=42/R178=39/R150=36/R60=35**。备份 .bak_pre_refine3
> ⚠️ ~~已知坑（重跑 reality 构建必读）：persist 把 discarded strand 的 status 固化 → load_strands 过滤（WHERE status='completed'）→ 每轮重跑少 N 条（三轮累计 857→835，22 discarded）~~ **已修（2026-08-05 收尾）**：load_strands 改全量读取（丢弃判定是构建输出不是输入过滤）；flash_pilot_verify sample_for_review 改随机抽样（seed=2026 固定，原 rows[:k] 只取前 N 个 reality 代表性不足）——TDD +4（503 passed），副本已同步。flash 归并粒度随机波动（同输入 3 轮：230/200/179 reality，max 34/85/39）——字段完整 ≥97% + max ≤40 为佳，必要时重跑取优
> 工程改动（2026-08-05 全量阶段，TDD +7 单测，499 passed）：
> ① strand：857 全 completed、0 失败；hdl ≤30 字规范率 99.4%（852/857，≥99% 门槛 ✓）；ooda 四组完整率 **38.3%**（pilot 32%，宁缺勿错基线，用户已接受）；query_text 500 截断生效（max=500/avg 57.7）
> ② reality：**230 reality**（flash 粒度偏细，pilot 21/金标准 25；tester 金标准 50——宁分不并方向安全），单 strand 53%、avg 3.7、max 34；字段完整 228/230（2 条 goals 空 = 工作线完结的如实报告，非结构缺陷）
> ③ 注入快照（决策 39）：**458/458 块全命中、0 空注入**（全库视角含自块）
> ④ 成本：strand 阶段 ~26 分钟（857 strand）；reality 两阶段 ~12 分钟/轮（43 批 × 决策层+内容层）
> ⚠️ 归属抽样 8 条待人工评估（≥80% 门槛）；reality@3「旧版文档归并与模块评审」13 成员偏宽（文档工程大类，同领域多工作线边界案例）
> 工程改动（2026-08-05 全量阶段，TDD +7 单测，499 passed）：
> ① **refine 两阶段重构**（根治输出截断）：决策层（归属映射 s2r/discarded/affected，输出极小）+ 内容层（affected 分批 ≤10 生成详情）——flash 全量输出是行为铁律（无视增量指令），单调用模式累积 ~200 reality 必超 max_tokens=8192 截断（实测批次 16/212 reality 失败）
> ② **向量粗筛候选 ref**（embed_batch）：全量紧凑 ref 在 500 reality 规模 ~90K 字符超 context → 每 strand vs reality name+hdl 云 top-30 候选；embed 失败尾部裁剪降级
> ③ **过度归并修复**（172→34 成员）：compact ref 加 goals（承接判定锚）+ 决策层规则强化（member_count≥6 只接受明确承接 goals 的 strand）——实测 reality 28 吞 172 strand/43 sessions（同领域不同工作线揉杂）→ 修复后 max=34 且为单一工作线多阶段
> ④ **--inject-only**（命令 3 省时等价物）：复用 DB 现有 reality 只做注入快照，不重跑 stream_build（避免 reality 结果随机漂移 + 省 15 分钟）
> ⑤ **dump 现场**：解析失败（含重试）保存原始输出 reality_fail_*.txt（任务书纪律 5 落地）
> ⑥ timeline 深度=1 为一次性构建预期（标注非缺陷）；key_facts 截断锚点优先（enforce_section_limits 已接）
> 前置状态：pilot 完成（2026-08-05）——winker 12 session/46 块/59 strand 全链路跑通，独立 DB（winker/ca_cache/flash_pilot.db）零污染生产库。
> 验证结论：
> ① strand：hdl 规范率 100% ✓；ooda 四组完整率 32% —— **用户决策（08-05）：接受 flash 如实报告空组（宁缺勿错优先），32% 为行为基线，非硬门槛**（flash 严格只写有内容的组，4B 是填满式 91%）
> ② reality：21 realities（金标准 25，粒度随 flash 随机性波动 3~41），字段完整 100%，单 strand 19%（4B 现状 87% 过碎 → 大幅改善），金标准名称语义匹配 98%（40/41）
> ③ 归属抽样：6/6 合理（reality@1 合并偏宽：GGUF 插件+硬链接+工作流重构可拆）
> ④ 成本：单块 avg 2.36s → 全量 tester 505 块 ≈ 29 分钟，可行
> 工程产物：ca/cloud_llm.py + ca/flash_reprocess.py + reprocess --llm flash + scripts/flash_build_reality.py + scripts/flash_pilot_verify.py（28 单测全绿）
> 遗留：① ~~reality 粒度随机性（3~41 波动）~~ **已评估（2026-08-05）**：temperature 非主因（0.2: 44/28/37/21/23；0.0: 45+1 parse fail）——flash 固有随机性，接受（reality 宁分不并、碎片由 theme 层共现聚合，过碎可逆、错并不可逆；成员云表征在单 strand reality 下退化为单样本仍可用）；② ~~test_plugin.py recall 测试既有失败~~ **已修（2026-08-05）**：v7 后 session-start recall 走 `ca.inject.pick_injection_themes`，测试仍 mock v6 的 `query_themes_by_semantics` → 改 mock `pick_injection_themes`（3 用例，40 passed）；③ ~~sync-ca.sh 同步~~ **已完成（2026-08-05）**：winker/sysadmin 副本已同步（含 cloud_llm/flash_reprocess/inject）；④ ~~key_facts 超限节流~~ **已完成（2026-08-05）**：flash reality 写入接 enforce_section_limits 锚点优先截断（TDD +2，30 测试全绿）
> 质量评估 + 改进措施（2026-08-05）：评估=数据可用（结构完整/内容纯净/金标准事实 24/25 可查证/超长输入处理正常/丢弃判定正确）；**flash ooda 密度 4.0/strand vs 4B 6.6（低 40%）——用户裁定非缺陷**（ooda 全流程，未走到后段缺失正常；补密度 prompt 诱发幻觉，否决）。措施：① 不加 reality 拆分校验（flash 难分即粘连=同一 reality）；② 不加密度 prompt（否决）；③ query_text 截断 QUERY_TEXT_MAX=500（落库+云构建双端，TDD +3）；④ ~~跨块语义去重~~ **[自我纠正撤销（2026-08-05）]**：S40/S41 是同一工作线两阶段（T2「待清理」→ T3「已清理」），cos 高相似恰是承接延续证据、timeline 演变素材——语义相似≠重复，跨块 strand 由 reality 层聚合判定承接，strand 层去重越权且丢失演进信息（已删代码/测试/参数）；⑤ 验证脚本标注 timeline 一次性构建 vs 增量；⑥ reality prompt 成员>4 生成期自检（工作承接关系判定，非主题相似）。718→716 测试通过（-2 去重测试），副本已同步。
> 背景：tester 4B 数据质量审计确认硬天花板（金标准元数据缺 36/50、created_at 失真 95% 批量写入、9% ooda 缺组）——方案验证数据不可靠 → 用 deepseek flash（云端）按新标准全链路重放对话（切块→注入判断→strand 生成→reality 一步到位）
> 前置决策：决策 37（reality 模型）/ 38（图模型+S 模型+空注入）/ 39（成员云表征+镜像匹配）

## 一、已拍板（用户确认，2026-08-03）

1. **切块：沿用话题检测**（topic_mgr，不引入 Fct 事务级切分）
2. **注入判断：用新标准**（决策 39：提问域成员云匹配 + 空注入宁缺勿错）——重跑时快照注入结果，产出真实注入日志
3. **reality 一步到位**：flash 直接按 reality 模型（name/hdl/current_status/timeline，决策 37）生成，不再事后云端精炼
4. **先用 winker pilot**（12 session/46 strand，小样本快验），通过后全量 tester（112 session/505 strand）

## 二、Pilot 方案

```
输入：winker 原始对话
  - /home/i1j/.hermes/profiles/winker/state.db（messages 3713 条/389 user）
  - /home/i1j/.hermes/profiles/winker/ca_cache/{session_id}.db（per-session turn_stream）
  - /home/i1j/.hermes/profiles/winker/ca_cache/ca_topics.db（现 strand/theme，仅对照）
流程：话题检测（沿用）→ 块 → flash 生成 strand（hdl+ooda+归属判断）
    → flash 按 reality 模型一步到位生成 reality
输出：独立 DB（ca_cache/flash_pilot.db），不污染 winker 生产库
验证：① strand 质量（hdl ≤30 字规范率/ooda 四组完整率，对照 4B 现数据 99%/91%）
     ② reality 结构（数量/大小分布/字段完整）——对比现金标准 exp_reality_winker_refined.json（25 reality）
     ③ 归属合理性抽样评估（≥80% 通过）
     ④ 成本（单块 flash 耗时 × 505 → 全量可行性）
```

## 三、工程改造点

1. **Cloud LLM 层**（新）：`ca/cloud_llm.py`——deepseek flash API 调用（OpenAI 兼容 /v1/chat/completions）
   - key：`/home/i1j/.hermes/profiles/tester/.env` 的 DEEPSEEK_API_KEY（不硬编码，从 .env 读）
   - 模型名：`deepseek-chat`（实测可用；`deepseek-v4-flash` 名 404，勿用）
   - 参考现有 `ca/inject.py` 的 urllib 调用模式
2. **reprocess 改造**：`scripts/reprocess_old_sessions.py`（633 行，框架完整）——LLM 调用 ollama→flash 可切换；输出写独立 DB
3. **reality 生成**：复用 `scripts/refine_reality_cloud.py` stream 模式思路（首批 build + 后续批 refine），换 flash + reality 模型输出（name/hdl/current_status/timeline 四段，非 theme 结构）

## 四、关键坑（务必遵守，会话教训沉淀）

1. **聚合键铁律**：topic_id 是 session 内编号（topic_manager.py:429 reset=1）——块/共现必须用 `(session_id, topic_id)` 复合键，跨 session 同名 topic 是不同块（早期实验 94% 假共现作废）
2. **自包含评估**：任何覆盖率评估必须留一/留块（质心含自身虚高 2.4 倍：0.815→0.333；检索含同块虚高：0.337→0.217；共现含当前块虚高 20%：0.82→0.675）
3. **json.load 后 dict key 全是 str**，int key 需显式转换
4. **云端返回 strand id 是 "S7" 格式**——parse 要容忍前缀（refine_reality_cloud.py 有 parse_sid）
5. **delegate_task 子代理会虚构成功报告**（声称写文件成功但文件不存在）——委派后必须自行验证句柄
6. **winker/tester 的 ca_topics.db.turn_stream 为空**——提问从 state.db messages 取（strand.turns[0] → 该 session 第 N 条 user 消息，505/505 可连，整段记录不拆单句，同块去重）
7. **create_at 是写入时间非对话时间**（tester 95% 批量写入）——时间序模拟不可靠，评估用金标准 id 映射

## 五、参考文件

- 决策：`docs/wiki/decisions/37-reality-restructure.md` / `38-reality-graph-inject-merge.md` / `39-reality-member-clouds.md`
- 脚本：`scripts/reprocess_old_sessions.py`（重放框架）、`scripts/refine_reality_cloud.py`（stream 构建）、`scripts/audit_tester_data.py`（质量审计）、`scripts/exp_*`（验证实验）
- 金标准：`scripts/exp_reality_winker_refined.json`（25 reality，4B 精炼版，pilot 对照）、`exp_reality_tester_out.json`（50 reality，流式构建，元数据缺 36）

## 六、Pilot 通过门槛 → 全量 tester

- flash strand ooda 四组完整率 ≥ 91%、hdl 规范率 ≥ 99%
- reality 归属抽样 ≥ 80% 合理
- 全量成本预估可控（flash 便宜快，505 块后台批量）

## 七、新会话开场建议

1. 读本任务书 + 决策 38/39（重点：成员云表征、镜像原则、宁滥勿缺/宁分不并取向）
2. 读 `ca/inject.py`（已有 4B 拣选雏形，可参考）与 `scripts/refine_reality_cloud.py`
3. 先写 `ca/cloud_llm.py` 单测连通（deepseek-chat 返回 OK），再改造 reprocess
4. 跑 winker pilot → 验证 → 汇报
