"""
ca/ooda_parser.py — OODA 解析器 (v4.4.0 alpha)
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False


class OODAParser:
    TITLE_ALIASES = {
        "core_change": ["核心摘要", "核心更新", "本轮摘要", "关键变化", "核心变更", "核心结论"],
        "new_materials": ["资源与观察", "资源观察", "新增资源", "文件变更", "物料清单", "代码变更",
                          "现象与问题", "现象问题", "新增现象", "新的现象"],
        "objective_facts": ["事实与约束", "客观事实", "发现与约束", "事实发现", "环境事实", "报错与参数",
                             "背景与约束", "背景约束", "约束条件", "背景"],
        "consensus": ["决策与结论", "共识与决策", "达成共识", "决定与结论", "决策结论",
                      "决策与共识", "决策与方案"],
        "todo": ["后续行动", "待办事项", "下一步", "行动项", "待办",
               "行动", "待办行动"],
    }

    def __init__(self, emb_client, threshold=None):
        self.emb = emb_client
        self.threshold = threshold or Config.OODA_DEDUP_THRESHOLD
        self._has_numpy = _NUMPY
        self._embed_cache: OrderedDict[str, List[float]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._max_cache = Config.OODA_EMBED_CACHE_MAX_SIZE

    def parse(self, ooda_text: str, previous_summary: Optional[Dict] = None) -> Dict:
        extracted, meta = self._extract_sections(ooda_text)
        if previous_summary and previous_summary.get("core_change") != "本轮无新内容":
            extracted = self._vector_dedup(extracted, previous_summary)
        return self._build_result(extracted, meta)

    def _extract_sections(self, text: str) -> Tuple[Dict, Dict]:
        alias_to_field = {}
        all_aliases = []
        for field, aliases in self.TITLE_ALIASES.items():
            for a in aliases:
                all_aliases.append(re.escape(a))
                alias_to_field[a.lower()] = field

        anchor_re = re.compile(r"(?:^|\n)\s*((?:{}))[ \t]*[:：]".format("|".join(all_aliases)),
                               re.IGNORECASE | re.MULTILINE)
        anchors = []
        for m in anchor_re.finditer(text):
            title = m.group(1).rstrip(":：").strip().lower()
            field = alias_to_field.get(title)
            if field:
                anchors.append((m.start(1), m.end(1), field))

        meta = {"sections_found": [], "sections_missing": [], "fallback_used": False}
        result = {f: ("" if f == "core_change" else []) for f in self.TITLE_ALIASES}
        for i, (start, end, field) in enumerate(anchors):
            next_start = anchors[i + 1][0] if i + 1 < len(anchors) else len(text)
            content = text[end:next_start].strip().lstrip(":：　 ")
            if field == "core_change":
                result[field] = content
            else:
                items = self._parse_list(content)
                result[field] = items[:3]
            meta["sections_found"].append(field)
        meta["sections_missing"] = [f for f in self.TITLE_ALIASES if f not in meta["sections_found"]]
        return result, meta

    def _parse_list(self, content: str) -> List[str]:
        content = content.strip()
        if content in ("无", "無", "none"):
            return []
        items = []
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith(("-", "•", "*")):
                item = line[1:].strip()
                if item and item not in ("无", "無", "none"):
                    items.append(item[:50])
        if not items:
            for line in content.split("\n"):
                line = line.strip()
                if line and line not in ("无", "無", "none"):
                    items.append(line[:50])
        return items[:3]

    def _cached_embed(self, text: str) -> List[float]:
        with self._cache_lock:
            if text in self._embed_cache:
                self._embed_cache.move_to_end(text)
                return self._embed_cache[text]
        vec = self.emb.embed(text)
        with self._cache_lock:
            self._embed_cache[text] = vec
            if len(self._embed_cache) > self._max_cache:
                self._embed_cache.popitem(last=False)
        return vec

    def _vector_dedup(self, current: Dict, previous: Dict) -> Dict:
        if not self._has_numpy:
            return self._vector_dedup_pure(current, previous)
        try:
            new_texts, text_map = [], []
            if current.get("core_change"):
                new_texts.append(current["core_change"])
                text_map.append(("core_change", -1))
            for field in ["new_materials", "objective_facts", "consensus", "todo"]:
                for idx, item in enumerate(current.get(field, [])):
                    new_texts.append(item)
                    text_map.append((field, idx))
            if not new_texts:
                return current
            prev_texts = []
            if previous.get("core_change"):
                prev_texts.append(previous["core_change"])
            for field in ["new_materials", "objective_facts", "consensus", "todo"]:
                prev_texts.extend(previous.get(field, []))
            if not prev_texts:
                return current

            new_emb = np.array([self._cached_embed(t) for t in new_texts])
            prev_emb = np.array([self._cached_embed(t) for t in prev_texts])
            if new_emb.ndim != 2 or prev_emb.ndim != 2 or new_emb.shape[1] != prev_emb.shape[1]:
                logger.warning("Embedding dimension mismatch, skip dedup")
                return current

            new_norm = np.linalg.norm(new_emb, axis=1, keepdims=True)
            prev_norm = np.linalg.norm(prev_emb, axis=1, keepdims=True)
            new_norm = np.where(new_norm == 0, 1.0, new_norm)
            prev_norm = np.where(prev_norm == 0, 1.0, prev_norm)
            sims = np.dot(new_emb / new_norm, (prev_emb / prev_norm).T)
            is_dup = sims.max(axis=1) > self.threshold

            deduped = dict(current)
            for i, (field, idx) in enumerate(text_map):
                if is_dup[i]:
                    if field == "core_change":
                        deduped["core_change"] = ""
                    else:
                        deduped[field] = [item for j, item in enumerate(deduped[field]) if j != idx]
            return deduped
        except Exception as e:
            logger.error("Vector dedup failed: %s", e)
            return current

    def _vector_dedup_pure(self, current, previous):
        new_texts, text_map = [], []
        if current.get("core_change"):
            new_texts.append(current["core_change"])
            text_map.append(("core_change", -1))
        for field in ["new_materials", "objective_facts", "consensus", "todo"]:
            for idx, item in enumerate(current.get(field, [])):
                new_texts.append(item)
                text_map.append((field, idx))
        if not new_texts:
            return current
        prev_texts = []
        if previous.get("core_change"):
            prev_texts.append(previous["core_change"])
        for field in ["new_materials", "objective_facts", "consensus", "todo"]:
            prev_texts.extend(previous.get(field, []))
        if not prev_texts:
            return current

        try:
            new_emb = [self._cached_embed(t) for t in new_texts]
            prev_emb = [self._cached_embed(t) for t in prev_texts]
        except Exception:
            return current

        dim = len(new_emb[0]) if new_emb else 0
        if any(len(v) != dim for v in new_emb) or any(len(v) != dim for v in prev_emb):
            logger.warning("Inconsistent embedding dimensions in pure-python dedup")
            return current

        deduped = dict(current)
        for i, (field, idx) in enumerate(text_map):
            max_sim = 0.0
            n1 = math.sqrt(sum(x * x for x in new_emb[i]))
            for p in prev_emb:
                n2 = math.sqrt(sum(x * x for x in p))
                if n1 == 0 or n2 == 0:
                    continue
                dot = sum(x * y for x, y in zip(new_emb[i], p))
                sim = dot / (n1 * n2)
                if sim > max_sim:
                    max_sim = sim
            if max_sim > self.threshold:
                if field == "core_change":
                    deduped["core_change"] = ""
                else:
                    deduped[field] = [item for j, item in enumerate(deduped[field]) if j != idx]
        return deduped

    def _build_result(self, extracted, meta):
        core = extracted.get("core_change", "").strip()
        if core == "本轮无新内容":
            return {"core_change": "本轮无新内容", "_parse_meta": meta}
        result = {"_parse_meta": meta}
        for field in self.TITLE_ALIASES:
            if field in meta["sections_found"]:
                result[field] = extracted[field]
        has_content = any(result.get(f) for f in self.TITLE_ALIASES if f != "core_change" or result.get("core_change"))
        if not has_content:
            return {"core_change": "本轮无新内容", "_parse_meta": meta}
        return result