"""ca/f_stage.py — F-stage 异步摘要 (v5.10, 纯 LLM 路径)

设计决策: Fct-001~Fct-012 (Fct 摘要重构, PDD 哲学), D-033 (多话题 OODA 分治摘要)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-l1-摘要重构-v470-pdd-哲学
  - daemon 线程读取 DB Elm → 调 LLM → 解析 → 写 LLM 版 Fct/Hdl
  - 多 OODA：已知话题逐一提取 → 剩余新话题检测（docs/decisions/28-topic-summarization-v4/28-topic-summarization-v4.md）
  - 覆盖 E-stage 代码级摘要
  - 截断检测双重校验 (finish_reason + endswith)

职责：
- 仅处理需要 LLM 调用的 Fct/Hdl 生成路径
- 不再包含 bg_review 分支（已合并到 E-stage）
- F-stage 线程内读 DB Elm → 调 LLM → 解析 → 写 LLM 版 Fct/Hdl（覆盖代码级）

与 E-stage 的关系：
  E-stage 已顺手写入 seq=0 代码级 Fct（80 字符摘要）
  F-stage 检查到有 Fct 时仍可覆盖（LLM 版本更准确）
  process_turn_f_stage 在 __init__.py 中做路由决策
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import ASSEMBLE_OK, ASSEMBLE_PENDING_BACKFILL, Config
from .post_process import (
    PAIR_PATTERN,
    VALID_STATES,
    MEANINGLESS_CORE,
    clean_increment,
    parse_v1_markdown_xml,
)
from .fct_multi_affair import (
    build_fct_think_context,
    flatten_affairs_to_legacy,
    parse_fct_multi_affair,
)
from .prompts import FCT_GENERATION_PROMPT, FCT_GENERATION_PROMPT_MULTI_AFFAIR
from .store import format_previous_summary_for_prompt, read_incremental_elm, read_prev_fct

logger = logging.getLogger(__name__)


class FStageMixin:
    """F-stage 混合类。由 ContextAssembler 通过多重继承引入。

    访问的 self 属性：
      self.store          — SQLiteStore
      self.embed_client   — EmbeddingClient
      self.cache          — AssemblyCache
      self.stats          — AssembleStats
      self._session_id    — str
      self._turn_counter  — int
      self._pending_tasks — Dict[int, threading.Thread]
      self._task_lock     — threading.Lock
    """

    def _run_f_stage(self, session_id: str, turn_index: int, fin_seq: int) -> None:
        """F-stage 核心：仅 LLM 路径。

        基于增量 Elm 输入（user 行 + 上次 fin 之后到本次 fin 的内容），
        为指定 fin 行 (turn, fin_seq) 生成 Fct/Hdl。

        bg_review 分支已移除——代码级摘要已在 E-stage 的 _on_post_tool_call_v5 中完成。
        """
        start = time.monotonic()
        logger.info("[CA] _run_f_stage: START turn %d fin_seq %d (LLM path)", turn_index, fin_seq)
        dialogue_ok = False

        # ── 从 DB 读增量 Elm（user + 上次 fin 之后到 fin_seq 之间的内容） ──
        rows = read_incremental_elm(self.store, session_id, turn_index, fin_seq)
        if not rows:
            logger.warning("[CA] _run_f_stage turn %d fin_seq %d: no Elm rows found, skipping", turn_index, fin_seq)
            return

        parts: List[str] = []
        user_elm = ""
        for seq, role, content, tool_name, tool_call_id in rows:
            if role == "user":
                user_elm = content or ""
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            elif role == "tool":
                parts.append(f"Tool({tool_name}): {(content or '')[:200]}")
        elm_text = "\n".join(parts)

        # ── 决策 44 R5：优先使用事务帧（[ooda_stage|block_type] 前缀），
        # 历史行无 block_type 时自动回退 legacy 文本，保证兼容。 ──
        if Config.FCT_STRUCTURED_INPUT:
            try:
                from .blocks import format_transaction_frames
                from .store import read_incremental_elm_detailed
                detailed_rows = read_incremental_elm_detailed(
                    self.store, session_id, turn_index, fin_seq)
                if detailed_rows and any(r.get("block_type") for r in detailed_rows):
                    elm_text = format_transaction_frames(detailed_rows)
                    for r in detailed_rows:
                        if r.get("role") == "user" and r.get("Elm"):
                            user_elm = r["Elm"]
            except Exception as exc:
                logger.warning("[CA_v7] transaction frames fallback: %s", exc)

        # ── 决策 44 续：代码筛选当前 turn 的 think 卡，作为多事务划分线索。
        # 仅 orient/decision、按预算截断；无卡时 elm_text 保持旧格式。 ──
        if Config.FCT_MULTI_AFFAIR_ENABLED:
            try:
                think_context = build_fct_think_context(
                    self.store, session_id, turn_index)
                if think_context:
                    elm_text = (
                        "【首轮思考线索（代码筛选）】\n"
                        + think_context
                        + "\n\n【事务帧】\n"
                        + elm_text
                    )
            except Exception as exc:
                logger.warning("[CA_v7] fct think context failed: %s", exc)

        prev_fct = read_prev_fct(self.store, session_id, turn_index)

        # prev_fct 为空时（首轮/无历史摘要），提供格式完整但内容为空的 Fct JSON，
        # 使 format_previous_summary_for_prompt → _json_to_v1_markdown 正常走 JSON 解析路径，
        # 给 LLM 一个「历史摘要为空」的明确信号，而非跳过 LLM 或塞"无历史回顾"文字
        if not prev_fct:
            prev_fct = json.dumps(
                {"changes": [], "core_change": "",
                 "new_materials": [], "objective_facts": [],
                 "consensus": [], "todo": []},
                ensure_ascii=False,
            )

        try:
            logger.info("[CA] _run_f_stage turn %d fin_seq %d: calling LLM", turn_index, fin_seq)
            try:
                response_text, finish_reason = self._call_llm_for_fct(prev_fct, elm_text, turn_index)
            except FctTruncatedException as e:
                response_text = e.response_text
                finish_reason = "length"

            # 截断检测
            _has_stage_tag = "<stage_tag>" in response_text
            _ends_with_close = response_text.strip().endswith("</core_change>") or response_text.strip().endswith("}")
            if finish_reason == "length" or (_has_stage_tag and not _ends_with_close):
                logger.warning("[CA-METRIC] ca.fct.truncated_fallback: turn=%d fin_seq=%d, finish_reason=%s, len=%d, num_predict=%d",
                               turn_index, fin_seq, finish_reason, len(response_text), Config.L1_MAX_TOKENS)
                with self.stats._lock:
                    self.stats.truncated_fallback += 1
                partial = (response_text or "").strip()
                # 截断优先尝试多事务 JSON partial
                if Config.FCT_MULTI_AFFAIR_ENABLED:
                    partial_multi = parse_fct_multi_affair(partial)
                    if partial_multi:
                        truncated_cleaned = flatten_affairs_to_legacy(
                            partial_multi["affairs"])
                        truncated_cleaned["_assemble_status"] = ASSEMBLE_PENDING_BACKFILL
                        fct_str = json.dumps(truncated_cleaned, ensure_ascii=False)
                        hdl_text = self._extract_hdl(truncated_cleaned, turn_index) or (user_elm or "")[:150]
                        self._update_fct_v5(session_id, turn_index, fin_seq, fct_str, hdl_text)
                        logger.info("[CA] _run_f_stage turn %d fin_seq %d: multi-affair partial saved as pending",
                                    turn_index, fin_seq)
                        return
                # 再尝试已完成 <stage_tag>/<core_change> 对
                raw_pairs = PAIR_PATTERN.findall(partial)
                valid_changes = []
                for raw_state, raw_core in raw_pairs:
                    state = raw_state.strip()
                    core = raw_core.strip()
                    if state in VALID_STATES and core and core not in MEANINGLESS_CORE:
                        valid_changes.append({"stage_tag": state, "core_change": core})
                if valid_changes:
                    core_change = "；".join(c["core_change"] for c in valid_changes)
                    truncated_cleaned = {
                        "changes": valid_changes,
                        "core_change": core_change,
                        "_assemble_status": ASSEMBLE_PENDING_BACKFILL,
                    }
                else:
                    # 无有效 XML 对时：尝试 parse_v1_markdown_xml 提取叙事段
                    try:
                        fct_dict, _, _ = parse_v1_markdown_xml(partial)
                        cleaned = clean_increment(fct_dict)
                        if cleaned.get("changes") or cleaned.get("core_change", "") not in ("本轮无新内容", ""):
                            cleaned["_assemble_status"] = ASSEMBLE_PENDING_BACKFILL
                            truncated_cleaned = cleaned
                        else:
                            raise ValueError("no content extracted")
                    except Exception:
                        truncated_cleaned = {
                            "changes": [],
                            "core_change": (partial[:500] or f"阶段摘要生成中（L1_MAX_TOKENS={Config.L1_MAX_TOKENS}，未完成）"),
                            "new_materials": [],
                            "objective_facts": [],
                            "consensus": [],
                            "todo": [],
                            "_assemble_status": ASSEMBLE_PENDING_BACKFILL,
                        }
                fct_str = json.dumps(truncated_cleaned, ensure_ascii=False)
                hdl_text = (user_elm or "")[:150]
                self._update_fct_v5(session_id, turn_index, fin_seq, fct_str, hdl_text)
                logger.info("[CA] _run_f_stage turn %d fin_seq %d: truncated, saved as pending backfill", turn_index, fin_seq)
                return

            # LLM 完全失败（所有重试均返回 error）
            if finish_reason == "error":
                logger.error("[CA-METRIC] ca.fct.all_retries_failed: turn=%d fin_seq=%d, len=%d",
                             turn_index, fin_seq, len(response_text))
                with self.stats._lock:
                    self.stats.truncated_fallback += 1
                fallback = json.dumps({
                    "changes": [],
                    "core_change": user_elm or "本轮无新内容",
                    "_assemble_status": ASSEMBLE_PENDING_BACKFILL,
                    "new_materials": [], "objective_facts": [],
                    "consensus": [], "todo": [],
                }, ensure_ascii=False)
                hdl = (user_elm or "本轮无新内容")[:100]
                self._update_fct_v5(session_id, turn_index, fin_seq, fallback, hdl)
                logger.info("[CA] _run_f_stage turn %d fin_seq %d: error, saved fallback", turn_index, fin_seq)
                return

            parser_hdl = ""
            multi_parsed = None
            if Config.FCT_MULTI_AFFAIR_ENABLED:
                multi_parsed = parse_fct_multi_affair(response_text)
            if multi_parsed:
                # 决策 44 续：多事务 affairs 输出，代码扁平化为 legacy 兼容结构
                cleaned = flatten_affairs_to_legacy(multi_parsed["affairs"])
                cleaned["_assemble_status"] = ASSEMBLE_OK
                dialogue_ok = True
            else:
                fct_dict, parser_hdl, _ = parse_v1_markdown_xml(response_text)
                if not parser_hdl:
                    logger.warning("[CA-METRIC] ca.hdl.skipped_empty: turn=%d fin_seq=%d", turn_index, fin_seq)
                    with self.stats._lock:
                        self.stats.skipped_empty += 1
                cleaned = clean_increment(fct_dict)
                cleaned["_assemble_status"] = ASSEMBLE_OK
                dialogue_ok = True

            fct_str = json.dumps(cleaned, ensure_ascii=False)
            hdl_text = self._extract_hdl(cleaned, turn_index)
            # v7.1 (2026-08-08, BUG-08 根治): 直接算语义文本 embedding——
            # 剥 Fct JSON 键名（TP-001 键名污染），与 topic_manager._compute_centroids
            # 的输入一致，切换路径读 cache 免重算。不再 embed 完整 JSON：
            # fct_embeddings 仅被死代码 BM25Snapshot/retrieval.py 消费（D8），纯浪费。
            try:
                from topic_manager import _extract_fct_semantic_text
                semantic_text = _extract_fct_semantic_text(fct_str)
                semantic_fct_emb = (
                    self.embed_client.embed(semantic_text)
                    if semantic_text else None
                )
            except Exception:
                semantic_fct_emb = None
            try:
                hdl_emb = self.embed_client.embed(hdl_text)
            except Exception:
                hdl_emb = None

            self._update_fct_v5(session_id, turn_index, fin_seq, fct_str, hdl_text)
            self.cache.add_turn(turn_index, hdl_text, fct_str, hdl_emb,
                                semantic_fct_emb=semantic_fct_emb)

        except Exception as e:
            logger.error("F-stage crash turn %d fin_seq %d: %s", turn_index, fin_seq, e, exc_info=True)
            fallback = json.dumps({
                "changes": [],
                "core_change": user_elm or "本轮无新内容",
                "_assemble_status": ASSEMBLE_OK,
                "new_materials": [], "objective_facts": [],
                "consensus": [], "todo": [],
            }, ensure_ascii=False)
            hdl = (user_elm or "本轮无新内容")[:100]
            self._update_fct_v5(session_id, turn_index, fin_seq, fallback, hdl)
            self.cache.add_turn(turn_index, hdl, fallback, None, None)

        finally:
            with self._task_lock:
                self._pending_tasks.pop((turn_index, fin_seq), None)
            elapsed = time.monotonic() - start
            logger.info("[CA] _run_f_stage: FINISH turn %d fin_seq %d in %.1fs (dialogue_ok=%s)",
                       turn_index, fin_seq, elapsed, dialogue_ok)
    def _call_llm_for_fct(self, prev_fct: str, elm_text: str, turn_index: int) -> Tuple[str, str]:
        """返回 (response_text, finish_reason)。所有重试均失败时返回 ("", "error")。"""
        import urllib.request
        llm_start = time.monotonic()
        prompt_template = (
            FCT_GENERATION_PROMPT_MULTI_AFFAIR
            if Config.FCT_MULTI_AFFAIR_ENABLED
            else FCT_GENERATION_PROMPT
        )
        prompt = prompt_template.format(
            previous_summary=format_previous_summary_for_prompt(
                prev_fct  # prev_fct 已是 JSON 字符串，不能再次 json.dumps
            ),
            current_dialog=elm_text)
        req_body = {
            "model": Config.LLM_MODEL,
            "prompt": prompt, "stream": False,
            "options": {
                "num_predict": Config.L1_MAX_TOKENS,
                "temperature": Config.L1_TEMPERATURE,
            },
            "keep_alive": -1,
        }
        # Fct 是结构化提取任务，非推理任务；None=未设置→默认关闭推理
        req_body["think"] = Config.LLM_THINK if Config.LLM_THINK is not None else False
        payload = json.dumps(req_body).encode()
        response_text = ""
        finish_reason = "error"
        for attempt in range(Config.LLM_MAX_RETRIES):
            try:
                # v7.1 (2026-08-08, BUG-08 根治): 轮次摘要走 high 优先级，
                # 与注入 4B 同档，确保不被后台话题摘要（normal）阻塞。
                req = urllib.request.Request(
                    f"{Config.LLM_ENDPOINT.rstrip('/')}/api/generate",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Queue-Priority": "high",
                    },
                )
                with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
                response_text = data.get("response", "")
                finish_reason = data.get("done_reason") or data.get("finish_reason", "stop")
                # 空响应 → 视为错误，触发 fallback 路径
                if not response_text.strip():
                    finish_reason = "error"
                if attempt > 0:
                    logger.info("[CA] LLM retry %d/%d succeeded turn=%d (len=%d, reason=%s)",
                                attempt + 1, Config.LLM_MAX_RETRIES, turn_index,
                                len(response_text), finish_reason)
                break
            except Exception as e:
                logger.warning("[CA] LLM attempt %d/%d failed turn=%d: %s",
                               attempt + 1, Config.LLM_MAX_RETRIES, turn_index, e)
                time.sleep(2 ** attempt)

        elapsed_ms = int((time.monotonic() - llm_start) * 1000)
        logger.warning("[CA-METRIC] ca.fct.latency_ms: turn=%d, ms=%d", turn_index, elapsed_ms)
        with self.stats._lock:
            self.stats.fct_latency_ms += elapsed_ms

        if finish_reason == "error":
            with self.stats._lock:
                self.stats.truncated_fallback += 1
            return ("", "error")

        return (response_text, finish_reason)

    def _extract_hdl(self, fct_dict: dict, turn_index: int) -> str:
        """从 fct_dict 提取 Hdl 文本。"""
        from .post_process import _safe_truncate
        changes = fct_dict.get("changes", [])
        if changes:
            core = "；".join(c.get("core_change", "") for c in changes)
        else:
            core = fct_dict.get("core_change", "")
            if core:
                _first_low = len(core)
                for _tag in ("【计划】", "【探讨】"):
                    _idx = core.find(_tag)
                    if 0 < _idx < _first_low:
                        _first_low = _idx
                if _first_low < len(core):
                    core = core[:_first_low].rstrip()
        if not core or core in ("无", "本轮无新内容"):
            # fallback: try first consensus/new_materials item
            for _field in ("consensus", "new_materials"):
                _items = fct_dict.get(_field, [])
                if _items and _items[0].strip() not in ("", "无新增", "本轮无", "- 无新增", "- 本轮无"):
                    _safe_truncated = _safe_truncate(_items[0], 100)
                    if _safe_truncated:
                        return _safe_truncated
            logger.warning("[CA-METRIC] ca.hdl.skipped_empty: turn=%d", turn_index)
            with self.stats._lock:
                self.stats.skipped_empty += 1
            return "无"
        result = _safe_truncate(core, 100)
        return result or "无"

    def _format_fct_for_display(self, fct_text: str) -> str:
        """将对话轮 Fct JSON 格式化为可读文本。"""
        if not fct_text or not fct_text.strip():
            return fct_text
        try:
            data = json.loads(fct_text)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON → 检测是否为调试描述
            for pat in self._FCT_DEBUG_PATTERNS:
                if pat in fct_text:
                    return ""
            return fct_text
        core = data.get("core_change", "")
        if not core:
            for pat in self._FCT_DEBUG_PATTERNS:
                if pat in fct_text:
                    return ""
            return fct_text
        changes = data.get("changes", [])
        if changes:
            lines = [f"【{c['stage_tag']}】{c['core_change']}" for c in changes]
        else:
            lines = [core]
        for key in ("new_materials", "objective_facts"):
            items = data.get(key, [])
            if items:
                real_items = [
                    str(i)[:240] for i in items
                    if str(i).strip() not in ("", "无", "-", "- 无", "无有效内容")
                ]
                if real_items:
                    joined = " | ".join(real_items)
                    lines.append(f"  {joined}")
        return "\n".join(lines)

    def _is_valid_fct(self, fct_text: str) -> bool:
        """检查 Fct JSON 是否包含有效内容。"""
        if not fct_text or not fct_text.strip():
            return False
        try:
            data = json.loads(fct_text)
            changes = data.get("changes", [])
            if changes:
                return True
            core = data.get("core_change", "")
            return bool(core and core != "本轮无新内容")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False

    def _get_previous_fct(self) -> Optional[Dict]:
        """从 turn_stream 查前一轮 Fct。"""
        from .store import read_prev_fct
        raw = read_prev_fct(self.store, self._session_id, self._turn_counter)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    _FCT_DEBUG_PATTERNS = (
        "当前会话", "CA 注入", "CA插件", "CA 插件", "当前 CA",
        "此会话", "本对话", "此对话",
        "当前上下文", "当前注入", "ctx 中",
        "完全无工具", "无工具组",
    )


# 避免 f_stage.py 内循环导入 — FctTruncatedException 在单独的 exceptions.py 中
from .exceptions import FctTruncatedException
