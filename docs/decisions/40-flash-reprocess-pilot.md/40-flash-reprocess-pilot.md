# 40：flash 全链路重跑 pilot 任务书（winker → tester）

> 状态：待执行（2026-08-03 晚，新会话启动点）
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
