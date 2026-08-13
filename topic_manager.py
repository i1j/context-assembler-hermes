"""plugins/ca_assembler/topic_manager.py — 话题管理模块 (v5.10)。

设计决策: TP-001 (话题分割), TP-002 (话题定级)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-话题拣选重构-v460-已实现
  - Jaccard 增量分割 + 强制短语 + 水位压力
  - 半径定级: ACT/REL/FAR
  - TopicGradeManager: 切换检测 + 首次 embedding 定级 + 冻结缓存

职责：
  - 增量话题分割（Jaccard + 强制短语）
  - 话题切换检测
  - 切换时基于 query embedding 的 topic-aware 定级
  - 缓存 topic→grade 映射，切换间冻结（稳定 prompt caching）
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from ca.config import Config
from ca.grade import Grade, TopicGrade

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Jaccard 文本相似度
# ═══════════════════════════════════════════════════════════════

# 中文 CJK 字符（包括全角标点）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]")
_WORD_SPLIT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+")


def _jaccard_text(text_a: str, text_b: str) -> float:
    """计算两段文本的 Jaccard 相似度。

    特征集构成：
      - 中文 CJK 单字符（实义字）
      - CJK 二元组（每两个相邻实义字）
      - 英文/数字词（含下划线分隔的 token）
    """
    cjk_a = set(_CJK_RE.findall(text_a))
    cjk_b = set(_CJK_RE.findall(text_b))

    # CJK 二元组
    chars_a = [c for c in text_a if _CJK_RE.match(c)]
    chars_b = [c for c in text_b if _CJK_RE.match(c)]
    bigram_a = set(chars_a[i] + chars_a[i + 1] for i in range(len(chars_a) - 1))
    bigram_b = set(chars_b[i] + chars_b[i + 1] for i in range(len(chars_b) - 1))

    # 英文/数字词
    tokens_a = set(_WORD_SPLIT_RE.findall(text_a.lower()))
    tokens_b = set(_WORD_SPLIT_RE.findall(text_b.lower()))

    set_a = cjk_a | bigram_a | tokens_a
    set_b = cjk_b | bigram_b | tokens_b

    if not set_a and not set_b:
        return 0.0  # 两段都空 → 不相似（无信息做判断）

    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


# ═══════════════════════════════════════════════════════════════
# Fct JSON 语义值提取（TP-001 已知缺陷族修复，2026-07-31）
# ═══════════════════════════════════════════════════════════════

# Fct JSON 中与话题无关的元数据键：值不参与 Jaccard。
#  - stage_tag: 固定状态集（已实施/计划/探讨…），跨话题恒定
#  - ooda: 固定 4 标签（现象与问题/背景与约束/决策与方案/后续行动），跨话题恒定
#  - _assemble_status / _truncated: 装配元数据
_FCT_METADATA_KEYS: frozenset = frozenset({
    "stage_tag", "ooda", "_assemble_status", "_truncated",
})


def _extract_fct_semantic_text(fct_json: str) -> str:
    """从 Fct JSON 提取语义文本（仅 value，剥键名 + 跳过元数据）。

    背景（TP-001 已知缺陷族，与"Jaccard 反例铁则"同源）：
    Fct JSON 公共键名（core_change/changes/stage_tag…）与元数据值
    （stage_tag 状态、_assemble_status 数字）对所有话题恒定，直接
    tokenize 会把无关话题的 Jaccard 抬过 ENTRY 阈值——
    例：{"core_change":"数据库设计"} vs {"core_change":"Python优化"}
    共享 core_change 键名 token → j=0.0714 ≥ 0.04 → 虚假延续。
    仅剥离键名不够（_assemble_status:0 的数字 0 仍共享，j≈0.074），
    必须同时跳过元数据键与非字符串 value。

    非 JSON 文本（原始用户消息 / 旧格式 / 损坏 JSON）原样返回。
    """
    if not fct_json:
        return ""
    try:
        obj = json.loads(fct_json)
    except (ValueError, TypeError):
        return fct_json
    if not isinstance(obj, (dict, list)):
        return fct_json  # 顶层标量（如 "123"）→ 非 Fct JSON 形状，原样

    parts: List[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, dict):
            for key, val in v.items():
                if key in _FCT_METADATA_KEYS:
                    continue
                _walk(val)
        elif isinstance(v, list):
            for item in v:
                _walk(item)
        elif isinstance(v, str):
            s = v.strip()
            if s:
                parts.append(s)
        # 非字符串 value（数字/布尔/None）→ 装配元数据，跳过

    _walk(obj)
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════
# 强制话题分割短语
# ═══════════════════════════════════════════════════════════════

_FORCED_SPLIT_PATTERNS: List[str] = [
    "换话题", "聊点别的", "另一个",
    "说回", "下一个问题", "还有一个问题",
    "再问一个", "别提", "不管",
    "换个话题", "不说这个",
    "回到正题", "说重点",
    "topic switch", "switching gears",
    "shift topics", "moving on",
    "change of subject",
]


def _scan_forced_split_phrases(user_msg: str) -> bool:
    """检查 user 消息是否包含强制话题分割短语。"""
    msg_lower = user_msg.lower()
    for phrase in _FORCED_SPLIT_PATTERNS:
        if phrase.lower() in msg_lower:
            return True
    return False


def _is_confirmatory_turn(user_msg: str, ca_rows: List) -> bool:
    """判断该轮是否为纯确认/简短消息（不应分割为新话题）。

    ≤5 字符的消息视为确认/延续（经多 session 实测，5 字阈值
    平均减少 40% 单轮话题，且不会过度合并）：
    - 短消息的 Jaccard 特征集太小（4 字仅 7 个特征），
      与累积文本（100+ 特征）做 Jaccard 匹配几乎不可能过 0.04，
      导致大量本属延续的短消息被错误切分成新话题。
    - 例："如何"、"好"、"改"、"继续"、"效果如何"——这些都在
      实际追踪当前话题，不是新话题。
    - 强制分割短语已在 _scan_forced_split_phrases 前置拦截。
    """
    cleaned = user_msg.strip().lower()
    if not cleaned:
        return True  # 空消息 → 延续
    if len(cleaned) <= 5:
        return True  # 短消息（≤5字）→ 延续
    return False





# ═══════════════════════════════════════════════════════════════
# 形心计算
# ═══════════════════════════════════════════════════════════════


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """余弦相似度。"""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _compute_centroid(vectors: List[List[float]]) -> Optional[List[float]]:
    """计算一组向量的形心（均值向量 + 归一化）。"""
    if not vectors:
        return None
    dim = len(vectors[0])
    centroid = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            centroid[i] += v[i]
    n = len(vectors)
    centroid = [c / n for c in centroid]
    norm = math.sqrt(sum(c * c for c in centroid))
    if norm == 0:
        return None
    return [c / norm for c in centroid]


# ═══════════════════════════════════════════════════════════════
# 话题定级（移植自 ca/__init__.py 同名方法）
# ═══════════════════════════════════════════════════════════════


def _grade_topics_by_radius(
    topic_data: Dict[int, Dict],
    q_emb: Optional[List[float]],
    retrieved_topics: Set[int],
) -> Dict[int, TopicGrade]:
    """按半径 r 对话题三级定级。

    Args:
        topic_data: {topic_id: {centroid, max_intra, nearest_centroid_dist, is_bg, ...}}
        q_emb: 触发切换的 user 消息 embedding
        retrieved_topics: 检索升级话题集合（当前不用，保留接口兼容）

    Returns:
        topic_grades: {topic_id → TopicGrade.ACT | TopicGrade.REL | TopicGrade.FAR}
    """
    topic_grades: Dict[int, TopicGrade] = {}

    if q_emb is None:
        for tid, td in topic_data.items():
            topic_grades[tid] = TopicGrade(Config.TOPIC_BG_LEVEL) if td.get("is_bg") else TopicGrade.REL
        return topic_grades

    for topic_id, td in topic_data.items():
        if td.get("is_bg"):
            topic_grades[topic_id] = TopicGrade(Config.TOPIC_BG_LEVEL)
            continue
        if td.get("centroid") is None:
            topic_grades[topic_id] = TopicGrade.REL
            continue

        sim = _cosine_similarity(q_emb, td["centroid"])
        d = 1.0 - sim

        r = td.get("max_intra", 0.05)
        nearest = td.get("nearest_centroid_dist", 0.0)
        if nearest > 0:
            r = min(r, nearest / Config.TOPIC_RADIUS_WEIGHT) if r > 0 else nearest / Config.TOPIC_RADIUS_WEIGHT
        if r <= 0:
            r = 0.05  # 最小半径保护

        if d <= r / 2.0:
            topic_grades[topic_id] = TopicGrade.ACT
        elif d <= r:
            topic_grades[topic_id] = TopicGrade.REL
        elif topic_id in retrieved_topics:
            topic_grades[topic_id] = TopicGrade.REL
        else:
            topic_grades[topic_id] = TopicGrade.FAR

        logger.debug("[CA_topic] topic %d: d=%.4f r=%.4f → %s", topic_id, d, r, topic_grades[topic_id])

    return topic_grades


# ═══════════════════════════════════════════════════════════════
# TopicGradeManager
# ═══════════════════════════════════════════════════════════════


class TopicGradeManager:
    """话题等级管理器。

    设计决策: TP-001 (话题分割), TP-002 (话题定级)
      viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-话题拣选重构-v460-已实现
      switch → grade_on_switch embedding 定级 → 冻结直到下次 switch

    职责：
      1. 增量话题分割——每次 pre_llm_call 检查新 turn 是否延续或切换话题
      2. 话题切换时——embed 用户消息、计算旧话题形心、按半径定级
      3. 缓存 topic→grade 映射——切换间冻结，保证 prompt caching 稳定

    用法：
        mgr = TopicGradeManager(store, embed_client)

        # 每次 pre_llm_call 调用一次 detect()
        switched = mgr.detect(turn, ca_rows, user_msg)

        # 如果 switched=True，调用 grade_on_switch()
        if switched:
            q_emb = embed_client.embed(user_msg)
            mgr.grade_on_switch(q_emb, user_msg)

        # 查询特定 turn 的等级（A-stage 流程替换时用）
        grade = mgr.get_turn_grade(turn_num)
    """

    def __init__(self, store: Any, embed_client: Any, session_id: str = '',
                 cache: Any = None) -> None:
        self._store = store
        self._embed_client = embed_client
        # v7.1 (2026-08-08, BUG-08 根治): 复用 F-stage 算好的 Fct 语义 embedding，
        # 切换路径 _compute_centroids 免重算（0 embed 网络调用）。
        self._cache = cache
        # session_id 优先用显式参数，回退到 store.session_id（动态读取）
        self._explicit_session_id: str = session_id

        # 增量话题分割：turn → topic_id
        self._turn_to_topic: Dict[int, int] = {}

        # topic 元数据
        self._topic_data: Dict[int, Dict] = {}

        # 当前话题
        self._current_topic_id: Optional[int] = None

        # 缓存等级 {topic_id → TopicGrade}
        self._topic_grades: Dict[int, TopicGrade] = {}

        # 上次切换发生的 turn
        self._switch_turn: int = 0

        # 已处理的最大 turn（增量标记）
        self._last_processed_turn: int = 0

        # 话题 id 自增
        self._next_topic_id: int = 1

        # 刚处理完的 turn 列表（供 detect 内部使用）
        self._processed_ca_rows: List[Dict] = []

        # 每个 topic 的累积 Fct 文本（Jaccard 增量匹配用，仅限当前话题）
        self._topic_text_profiles: Dict[int, str] = {}

    # ── 公共接口 ──

    def detect(self, turn: int, ca_rows: List[Dict], user_msg: str, total_tokens: int = 0) -> bool:
        """增量话题分割 + 切换检测。

        只在 turn > _last_processed_turn 时跑增量逻辑。
        返回 True 表示发生了话题切换。

        Args:
            turn: 当前对话轮号
            ca_rows: get_turn_ca_rows() 返回的该轮数据
            user_msg: 当前轮的 user 消息原文
            total_tokens: conv_hist 总 Token 估算（用于水位压力切割）
        """
        if turn <= self._last_processed_turn:
            return False

        self._processed_ca_rows = ca_rows

        old_topic = self._current_topic_id
        new_topic = self._assign_topic(turn, ca_rows, user_msg, total_tokens)
        self._current_topic_id = new_topic
        self._last_processed_turn = turn

        if new_topic is None:
            return False

        if old_topic is not None and new_topic != old_topic:
            logger.info("[CA_topic] topic switch: turn %d → topic %d (was topic %s)",
                       turn, new_topic, old_topic)
            return True

        return False

    def grade_on_switch(self, q_emb: Optional[List[float]], user_msg: str) -> None:
        """话题切换时：计算旧话题形心 + 定级 + 冻结。

        Args:
            q_emb: 切换触发时 user 消息的 embedding
            user_msg: 原始 user 消息（仅用于日志）
        """
        switch_turn = self._last_processed_turn

        # 每次都从 _turn_to_topic 重建 _topic_data（确保新话题不漏）
        self._init_topic_data()

        # 为每个旧话题计算形心（基于成员 turn 的 Fct embedding）
        self._compute_centroids()

        # 定级（基于到 q_emb 的距离）
        self._topic_grades = _grade_topics_by_radius(
            self._topic_data, q_emb, set()
        )

        # 新话题强制 ACT（最详细等级）
        if self._current_topic_id is not None:
            self._topic_grades[self._current_topic_id] = TopicGrade.ACT

        self._switch_turn = switch_turn

        logger.info("[CA_topic] grade_on_switch at turn %d: %s",
                    switch_turn,
                    {f"T{k}": v for k, v in self._topic_grades.items()})

    def get_turn_grade(self, turn_num: int) -> TopicGrade:
        """返回指定 turn 的话题等级。

        先在 topic_grades 缓存中查 topic→grade，再查 turn→topic。
        如果都没有，返回 TopicGrade.ACT（保守——完整保留）。
        """
        if turn_num <= 0:
            return TopicGrade.ACT

        topic_id = self._turn_to_topic.get(turn_num)
        if topic_id is None:
            return TopicGrade.ACT

        return self._topic_grades.get(topic_id, TopicGrade.ACT)

    def get_topic_grades(self) -> Dict[int, TopicGrade]:
        """返回当前缓存的 {topic_id → grade}。"""
        return dict(self._topic_grades)

    def get_current_topic_id(self) -> Optional[int]:
        """返回当前话题 id。"""
        return self._current_topic_id

    def reset(self) -> None:
        """重置所有状态（/new 或 /reset 时调用）。"""
        self._turn_to_topic.clear()
        self._topic_data.clear()
        self._topic_grades.clear()
        self._topic_text_profiles.clear()
        self._current_topic_id = None
        self._switch_turn = 0
        self._last_processed_turn = 0
        self._next_topic_id = 1

    # ── 内部方法 ──

    def _assign_topic(self, turn: int, ca_rows: List, user_msg: str, total_tokens: int = 0) -> Optional[int]:
        """增量话题分配 —— Jaccard + 水位压力与当前话题累积 Fct 文本匹配。

        策略：
          1. 强制短语 → 新话题
          2. 纯确认/简短消息 → 延续当前话题
          3. 第一个 turn → 话题 1
          4. 当前轮 Fct 与_current_topic累积文本做 Jaccard + 水位压力扣减
             - ≥ CHAIN → 同话题，Fct 追加到累积文本
             - ≥ ENTRY → 弱匹配，同话题，Fct 追加
             - 否则 → 新话题

        Args:
            turn: 当前对话轮号
            ca_rows: get_turn_ca_rows() 返回的该轮数据
            user_msg: 当前轮的 user 消息原文
            total_tokens: conv_hist 总 Token 估算（水位压力用）
        """
        if turn <= 0 or not user_msg:
            return None

        # 1. 强制短语 → 新话题
        if _scan_forced_split_phrases(user_msg):
            tid = self._next_topic_id
            self._next_topic_id += 1
            self._turn_to_topic[turn] = tid
            self._topic_text_profiles[tid] = self._extract_turn_fct(ca_rows) or ""
            logger.debug("[CA_topic] forced split: turn %d → new topic %d", turn, tid)
            return tid

        # 2. 纯确认/简短消息 → 延续当前话题（不分新话题）
        #    特征: user_msg ≤3 字符 且 Fct 为空或极短
        if self._current_topic_id is not None and _is_confirmatory_turn(user_msg, ca_rows):
            self._turn_to_topic[turn] = self._current_topic_id
            logger.debug("[CA_topic] confirmatory turn %d → continue T%d", turn, self._current_topic_id)
            return self._current_topic_id

        # 3. 第一个 turn → 话题 1
        if not self._turn_to_topic:
            self._turn_to_topic[turn] = 1
            self._next_topic_id = max(self._next_topic_id, 2)
            self._topic_text_profiles[1] = self._extract_turn_fct(ca_rows) or ""
            return 1

        # 3. Jaccard + 水位压力与当前话题累积文本匹配
        curr_fct = self._extract_turn_fct(ca_rows) or user_msg
        cur_tid = self._current_topic_id

        if cur_tid is not None and cur_tid in self._topic_text_profiles:
            profile = self._topic_text_profiles[cur_tid]
            j = _jaccard_text(curr_fct, profile)

            # 水位压力：conv_hist 总 token 驱动 Jaccard 柔性扣减
            j = self._apply_water_pressure(j, total_tokens)

            if j >= Config.TOPIC_JACCARD_CHAIN:
                # 强匹配 → 延续
                self._turn_to_topic[turn] = cur_tid
                self._topic_text_profiles[cur_tid] += " " + curr_fct
                logger.debug("[CA_topic] jaccard chain: turn %d → T%d (j=%.4f)", turn, cur_tid, j)
                return cur_tid
            elif j >= Config.TOPIC_JACCARD_ENTRY:
                # 弱匹配 → 延续
                self._turn_to_topic[turn] = cur_tid
                self._topic_text_profiles[cur_tid] += " " + curr_fct
                logger.debug("[CA_topic] jaccard entry: turn %d → T%d (j=%.4f)", turn, cur_tid, j)
                return cur_tid
            else:
                logger.debug("[CA_topic] jaccard miss: turn %d vs T%d (j=%.4f < %.2f)",
                            turn, cur_tid, j, Config.TOPIC_JACCARD_ENTRY)

        # 4. 不匹配 → 新话题
        tid = self._next_topic_id
        self._next_topic_id += 1
        self._turn_to_topic[turn] = tid
        self._topic_text_profiles[tid] = curr_fct
        logger.debug("[CA_topic] new topic: turn %d → T%d", turn, tid)
        return tid

    def _apply_water_pressure(self, raw_j: float, total_tokens: int) -> float:
        """水位压力：conv_hist 总 token → 柔性扣减 Jaccard。

        窗口由 ACCUMULATED_SPLIT_START~TOPIC_PEAK_TOKEN 定义。

        Args:
            raw_j: Jaccard 原始匹配值
            total_tokens: conv_hist 总 Token 估算

        Returns:
            扣减后的 effective_j
        """
        peak = Config.TOPIC_PEAK_TOKEN
        start = Config.ACCUMULATED_SPLIT_START
        penalty = Config.JACCARD_PENALTY_MAX

        if total_tokens <= start:
            return raw_j
        if total_tokens >= peak:
            return raw_j - penalty

        progress = (total_tokens - start) / (peak - start)
        return raw_j - progress * penalty

    def _extract_turn_fct(self, ca_rows: List) -> Optional[str]:
        """从 ca_rows 中提取该轮的代表性 Fct 文本（用于 Jaccard 匹配）。

        优选 user 行（seq=0）的 Fct，其次 assistant fin 行，最后任意非空 Fct。
        返回前经 _extract_fct_semantic_text 归一化：JSON Fct 仅保留语义 value
        （剥键名 + 跳过元数据），消除跨话题恒定的 JSON 键名对 Jaccard 的污染
        （TP-001 已知缺陷族："数据库设计" vs "Python优化" 原始 JSON
        j=0.0714 ≥ ENTRY → 虚假延续）。非 JSON 文本原样返回。
        """
        if not ca_rows:
            return None
        fin_fct = None
        any_fct = None
        for row in ca_rows:
            fct = row[5] if len(row) > 5 else None  # Fct
            if not fct:
                continue
            any_fct = fct
            if row[0] == 0:  # user 行
                return _extract_fct_semantic_text(fct)
            if row[1] == "assistant" and row[2] == "stop":  # fin 行
                fin_fct = fct
        if not (fin_fct or any_fct):
            logger.debug("[CA] _extract_turn_fct: no Fct found in %d rows", len(ca_rows))
        text = fin_fct or any_fct
        return _extract_fct_semantic_text(text) if text else None

    def _init_topic_data(self) -> None:
        """根据现有 turn→topic 映射重建 topic_data（先清空，防增量遗漏）。"""
        self._topic_data.clear()
        for turn, tid in self._turn_to_topic.items():
            if tid not in self._topic_data:
                self._topic_data[tid] = {
                    "centroid": None,
                    "max_intra": 0.05,
                    "nearest_centroid_dist": 0.0,
                    "is_bg": False,
                    "turns": [],
                    "embeddings": [],
                }
            self._topic_data[tid]["turns"].append(turn)

    def _session_id_resolved(self) -> str:
        """返回 _explicit_session_id（优先）或 store.session_id（动态回退）。"""
        if self._explicit_session_id:
            return self._explicit_session_id
        return getattr(self._store, 'session_id', '')

    def _compute_centroids(self) -> None:
        """为每个 topic 计算形心。

        遍历每个 topic 包含的 turn，对每行的 Fct 做 embed，
        取均值作为 topic 形心。
        """
        if not self._store:
            return

        from ca.store import get_turn_ca_rows

        for tid, td in self._topic_data.items():
            if td.get("is_bg"):
                td["centroid"] = None
                continue

            vectors: List[List[float]] = []
            for turn in td.get("turns", []):
                try:
                    ca_rows = get_turn_ca_rows(self._store, self._session_id_resolved(), turn)
                except (AttributeError, sqlite3.Error, TypeError) as e:
                    logger.warning("[CA] _compute_centroids: get_turn_ca_rows failed for turn %d (topic %d): %s", turn, tid, e)
                    continue
                if not ca_rows:
                    continue
                # 找 user 行（seq=0）的 Fct 做 embed
                fct_text = None
                for row in ca_rows:
                    if row[0] == 0:  # seq=0 → user 行
                        fct_text = row[5]  # Fct
                        break
                if not fct_text:
                    logger.debug("[CA] _compute_centroids: turn=%d topic=%d no user Fct, skipped from centroid", turn, tid)
                    continue
                # v7.1 (2026-08-08, BUG-08 根治): 优先复用 F-stage 算好的语义 embedding
                # （cache.semantic_fct_embeddings，与 _extract_fct_semantic_text 输入一致），
                # 切换路径免重算；cache 缺失（旧 turn/异常）→ 回退现有重算路径。
                cached_vec = None
                if self._cache is not None:
                    try:
                        cached_vec = self._cache.semantic_fct_embeddings.get(turn)
                    except Exception:
                        cached_vec = None
                if cached_vec is not None:
                    vec = cached_vec
                else:
                    try:
                        # 剥 Fct JSON 键名 + 跳过元数据（TP-001 同源缺陷族：公共键名抬高
                        # 无关话题的 centroid 相似度 → 半径定级失真；与 _extract_turn_fct 一致）
                        vec = self._embed_client.embed(_extract_fct_semantic_text(fct_text))
                    except Exception as e:
                        # 覆盖 ConnectionError/TimeoutError/urllib3 HTTPError 等
                        # 嵌入服务异常（BUG-02）：跳过该 turn，不中断 hook
                        logger.warning("[CA] _compute_centroids: embed failed for turn %d (topic %d): %s", turn, tid, e)
                        continue
                if vec:
                    vectors.append(vec)

            if vectors:
                td["centroid"] = _compute_centroid(vectors)
                if td["centroid"] is None:
                    continue

                # 计算内部最大距离（max_intra）
                max_d = 0.0
                for v in vectors:
                    sim = _cosine_similarity(v, td["centroid"])
                    d = 1.0 - sim
                    max_d = max(max_d, d)
                td["max_intra"] = max_d if max_d > 0 else 0.05

            # 计算最近形心距离
            nearest = float("inf")
            for other_tid, other_td in self._topic_data.items():
                if other_tid == tid or other_td.get("centroid") is None or td.get("centroid") is None:
                    continue
                sim = _cosine_similarity(td["centroid"], other_td["centroid"])
                d = 1.0 - sim
                nearest = min(nearest, d)
            td["nearest_centroid_dist"] = nearest if nearest < float("inf") else 0.0
