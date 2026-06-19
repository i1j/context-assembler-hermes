"""ca/f_stage.py — F-stage 异步摘要 (v5.10, 纯 LLM 路径)

设计决策: L1-001~L1-012 (L1 摘要重构, PDD 哲学)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-l1-摘要重构-v470-pdd-哲学
  - daemon 线程读取 DB Elm → 调 LLM → 解析 → 写 LLM 版 Fct/Hdl
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

from .config import Config
from .post_process import clean_increment, parse_v1_markdown_xml
from .prompts import FCT_GENERATION_PROMPT
from .store import format_previous_summary_for_prompt, read_turn_elm_rows, read_prev_fct

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

    def _run_f_stage(self, session_id: str, turn_index: int) -> None:
        """F-stage 核心：仅 LLM 路径。

        bg_review 分支已移除——代码级摘要已在 E-stage 的 _on_post_tool_call_v5 中完成。
        """
        start = time.monotonic()
        logger.info("[CA] _run_f_stage: START turn %d (LLM path)", turn_index)
        dialogue_ok = False

        # ── 从 DB 读一轮 Elm ──
        rows = read_turn_elm_rows(self.store, session_id, turn_index)
        if not rows:
            logger.warning("[CA] _run_f_stage turn %d: no Elm rows found, skipping", turn_index)
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
        prev_fct = read_prev_fct(self.store, session_id, turn_index)

        try:
            logger.info("[CA] _run_f_stage turn %d: calling LLM", turn_index)
            try:
                response_text, finish_reason = self._call_llm_for_fct(prev_fct, elm_text)
            except FctTruncatedException as e:
                response_text = e.response_text
                finish_reason = "length"

            # 截断检测
            _has_stage_tag = "<stage_tag>" in response_text
            if finish_reason == "length" or (_has_stage_tag and not response_text.strip().endswith("</core_change>")):
                logger.warning("[CA-METRIC] ca.fct.truncated_fallback: turn=%d, finish_reason=%s, len=%d",
                               turn_index, finish_reason, len(response_text))
                self.stats.truncated_fallback += 1
                truncated_cleaned = {
                    "changes": [],
                    "core_change": "本轮无新内容",
                    "new_materials": [],
                    "objective_facts": [],
                    "consensus": [],
                    "todo": [],
                    "_assemble_status": 1,
                }
                fct_str = json.dumps(truncated_cleaned, ensure_ascii=False)
                self._update_fct_v5(session_id, turn_index, fct_str, "")
                logger.info("[CA] _run_f_stage turn %d: truncated, saved as pending backfill", turn_index)
                return

            fct_dict, parser_hdl, _ = parse_v1_markdown_xml(response_text)
            if not parser_hdl:
                logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", turn_index)
                self.stats.skipped_empty += 1
            cleaned = clean_increment(fct_dict)
            cleaned["_assemble_status"] = 0
            dialogue_ok = True

            # LLM 产物的写覆盖
            fct_str = json.dumps(cleaned, ensure_ascii=False)
            hdl_text = self._extract_l0(cleaned)
            try:
                fct_emb = self.embed_client.embed(fct_str)
                hdl_emb = self.embed_client.embed(hdl_text)
            except Exception:
                fct_emb = None
                hdl_emb = None

            self._update_fct_v5(session_id, turn_index, fct_str, hdl_text)
            self.cache.add_turn(turn_index, hdl_text, fct_str, hdl_emb, fct_emb)

            if dialogue_ok:
                self.stats.tool_pre_upgrade_count = 0

        except Exception as e:
            logger.error("F-stage crash turn %d: %s", turn_index, e, exc_info=True)
            fallback = json.dumps({
                "changes": [],
                "core_change": user_elm or "本轮无新内容",
                "_assemble_status": 0,
                "new_materials": [], "objective_facts": [],
                "consensus": [], "todo": [],
            }, ensure_ascii=False)
            hdl = (user_elm or "本轮无新内容")[:100]
            self._update_fct_v5(session_id, turn_index, fallback, hdl)
            self.cache.add_turn(turn_index, hdl, fallback, None, None)

        finally:
            with self._task_lock:
                self._pending_tasks.pop(turn_index, None)
            elapsed = time.monotonic() - start
            logger.info("[CA] _run_f_stage: FINISH turn %d in %.1fs (dialogue_ok=%s)",
                       turn_index, elapsed, dialogue_ok)
    def _call_llm_for_fct(self, prev_fct, elm_text) -> Tuple[str, str]:
        """返回 (response_text, finish_reason)。所有重试均失败时返回 ("", "error")。"""
        import urllib.request
        llm_start = time.monotonic()
        prompt = FCT_GENERATION_PROMPT.format(
            previous_summary=format_previous_summary_for_prompt(
                json.dumps(prev_fct, ensure_ascii=False) if prev_fct else None
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
        if Config.LLM_THINK is not None:
            req_body["think"] = Config.LLM_THINK
        payload = json.dumps(req_body).encode()
        response_text = ""
        finish_reason = "error"
        for attempt in range(Config.LLM_MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    f"{Config.LLM_ENDPOINT}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=Config.LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
                response_text = data.get("response", "")
                finish_reason = data.get("done_reason") or data.get("finish_reason", "stop")
                break
            except Exception as e:
                logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
                time.sleep(2 ** attempt)

        elapsed_ms = int((time.monotonic() - llm_start) * 1000)
        logger.warning("[CA-METRIC] ca.fct.latency_ms: turn=%d, ms=%d", self._turn_counter, elapsed_ms)
        self.stats.fct_latency_ms += elapsed_ms

        if finish_reason == "error":
            self.stats.truncated_fallback += 1
            return ("", "error")

        return (response_text, finish_reason)

    def _extract_l0(self, fct_dict) -> str:
        """从 fct_dict 提取 Hdl 文本。"""
        from .post_process import _safe_truncate
        changes = fct_dict.get("changes", [])
        if changes:
            core = "；".join(c["core_change"] for c in changes)
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
            logger.warning("[CA-METRIC] ca.l0.skipped_empty: turn=%d", self._turn_counter)
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
