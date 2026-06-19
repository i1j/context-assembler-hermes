"""ca/a_stage.py — A-stage 话题注入 (v5.10)

设计决策: C-010 (A-stage 解耦), C-011 (三区模型 → 话题等级替换)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-a-stage-同步上下文组装
  职责：
  - 话题检测 → grade 驱动
  - 三级替换（Elm/Fct/Hdl 注入）
  - 增量缓存 + 尾巴保护

这些方法原本在 ca/__init__.py 中，现抽取为 AStageMixin。
|部分为 @staticmethod（无 self 依赖），其余通过 self 访问 engine 状态。
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set

from .config import Config
from .post_process import _safe_truncate
from .retrieval import cosine_similarity

logger = logging.getLogger(__name__)


class AStageMixin:
    """A-stage 混合类。由 ContextAssembler 通过多重继承引入。"""

    # ── 实例方法：依赖 self ──

    def _grade_topics_by_radius(
        self,
        turn_to_topic: Dict[int, int],
        topic_data: Dict,
        fct_embeddings: Dict[int, List[float]],
        q_emb: Optional[List[float]],
        retrieved_topics: set,
    ) -> Dict[int, str]:
        """按半径 r 对话题三级定级（转发到 topic_manager）。"""
        from topic_manager import _grade_topics_by_radius
        return _grade_topics_by_radius(topic_data, q_emb, retrieved_topics)
