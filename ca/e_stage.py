"""ca/e_stage.py — E-stage 写即落盘 + 代码级摘要 (v5.10)

设计决策: Fct 摘要重构 (PDD 哲学)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-l1-摘要重构-v470-pdd-哲学
  5 个写入点: post_api_request → pre_tool_call → post_tool_call → pre_llm_call → post_llm_call
  核心原则：写即落盘，不经过 buffer。turn_stream(turn, seq) 主键保证每行唯一。

职责：
- 逐事件接收 Hermes hook 调用
- 写即落盘到 turn_stream（不经过 buffer）
- 顺手计算代码级摘要（per-tool Fct）
- F-stage 负责 LLM 摘要写 fin 行（此模块仅提供 _update_fct_v5 → update_fin_fct_v5）

数据流：
  _on_api_response_v5    → write thought 行 (seq=N) + tool 占位行 + thought 代码摘要
  _on_pre_tool_call_v5   → no-op（占位已在 _on_api_response 写入）
  _on_post_tool_call_v5  → write tool 行 (seq=N+i) + per-tool Fct
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .post_process import _safe_truncate
from .store import write_turn_v5
from .tool_summarizer import ToolSummarizer

logger = logging.getLogger(__name__)


class EStageMixin:
    """E-stage 混合类。由 ContextAssembler 通过多重继承引入。

    访问的 self 属性：
      self.store              — SQLiteStore
      self._seq_counter       — Dict[int, int]  turn → max_seq
      self._tool_seq_map      — Dict[str, Tuple[int, int]]  tool_call_id → (turn, seq)
      self._session_id        — str
      self.tool_summarizer    — ToolSummarizer 实例
      self._current_turn      — int
    """

    def _on_api_response_v5(
        self,
        *,
        api_request_id: str,
        assistant_message: Any,
        api_call_count: int,
        turn_index: int,
        finish_reason: str = "tool_calls",
        usage: Optional[Dict] = None,
    ) -> None:
        """v5: 写 thought 行 + tool 占位行 + thought 代码摘要。"""
        pd = getattr(assistant_message, "provider_data", None) or {}
        thought = pd.get("reasoning_content", "") or getattr(assistant_message, "content", "") or ""
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        tool_defs = []
        for tc in tool_calls:
            tc_id = getattr(tc, "id", "") or ""
            tc_name = getattr(tc, "name", "") or ""
            tc_args = getattr(tc, "arguments", None)
            if isinstance(tc_args, str):
                try:
                    tc_args = json.loads(tc_args)
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}
            tool_defs.append({
                "id": tc_id,
                "type": getattr(tc, "type", "function"),
                "function": {"name": tc_name, "arguments": tc_args},
            })

        # 纯对话（无工具且非 tool_calls）→ 不写
        if not tool_defs and finish_reason != "tool_calls":
            return

        turn = turn_index
        seq = self._seq_counter.get(turn, 0) + 1
        self._seq_counter[turn] = seq

        # thought 代码摘要（无需 LLM）
        thought_fct = ""
        try:
            thought_fct = ToolSummarizer.generate_group_summary(thought)
        except Exception:
            pass
        if not thought_fct.strip():
            thought_fct = "空"

        # thought Hdl：去掉过渡前缀后的首句（~60 字）
        thought_hdl = ""
        try:
            raw = thought.strip()
            for prefix in ToolSummarizer._TRANSITION_PREFIXES:
                if raw.startswith(prefix):
                    raw = raw[len(prefix):].strip()
                    break
            if raw:
                thought_hdl = _safe_truncate(raw, max_len=60)
            if not thought_hdl.strip():
                thought_hdl = "空"
        except Exception:
            thought_hdl = "空"

        # usage 可能是 dict (生产) 或 SimpleNamespace (测试)，兼容两者
        if usage is not None:
            if isinstance(usage, dict):
                _prompt_tokens = usage.get("prompt_tokens")
                _completion_tokens = usage.get("completion_tokens")
            else:
                _prompt_tokens = getattr(usage, "prompt_tokens", None)
                _completion_tokens = getattr(usage, "completion_tokens", None)
        else:
            _prompt_tokens = None
            _completion_tokens = None

        write_turn_v5(
            self.store, self._session_id, turn, seq,
            role="assistant", content=thought,
            tool_calls_json=json.dumps(tool_defs, ensure_ascii=False),
            finish_reason=finish_reason,
            usage_prompt_tokens=_prompt_tokens,
            usage_completion_tokens=_completion_tokens,
            fct_text=thought_fct,
            hdl_text=thought_hdl,
            written_at=time.time(),
        )
        logger.info("[CA_v5] _on_api_response: wrote thought turn=%d seq=%d tools=%d",
                     turn, seq, len(tool_defs))

        # tool 占位行
        for i, tc_def in enumerate(tool_defs, start=1):
            tool_seq = self._seq_counter[turn] + i
            tc_id = tc_def["id"]
            write_turn_v5(
                self.store, self._session_id, turn, tool_seq,
                role="tool", content="",
                tool_name=tc_def.get("function", {}).get("name", ""),
                tool_call_id=tc_id,
                status="pending",
                written_at=time.time(),
            )
            self._tool_seq_map[tc_id] = (turn, tool_seq)

        self._seq_counter[turn] += len(tool_defs)
        logger.info("[CA_v5] _on_api_response: wrote %d tool placeholders", len(tool_defs))

    def _on_pre_tool_call_v5(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        args: Optional[Dict] = None,
        api_request_id: str = "",
    ) -> None:
        """v5: 无操作（占位行已在 _on_api_response_v5 写入）。"""
        logger.debug("[CA_v5] _on_pre_tool_call: %s (%s) — placeholder already written",
                     tool_name, tool_call_id)

    def _on_post_tool_call_v5(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        args: Optional[Dict] = None,
        result: Any = None,
        status: str = "ok",
        duration_ms: int = 0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        api_request_id: str = "",
    ) -> None:
        """v5: 回填 tool 行 + per-tool Fct。"""
        entry = self._tool_seq_map.get(tool_call_id)
        if entry:
            turn, seq = entry
        else:
            turn = self._current_turn
            seq = self._seq_counter.get(turn, 0) + 1
            self._seq_counter[turn] = seq
            logger.warning("[CA_v5] _on_post_tool_call: no placeholder for %s, created at turn=%d seq=%d",
                          tool_call_id, turn, seq)

        content = (
            result if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False) if result
            else ""
        )

        # ── per-tool Fct（代码级摘要） ──
        fct_str, hdl_text = "", ""
        try:
            tc_def = {
                "id": tool_call_id, "type": "function",
                "function": {"name": tool_name, "arguments": args or {}},
            }
            result_entry = [{"content": content, "status": status}]
            fct_dict, hdl_text = self.tool_summarizer.summarize(tc_def, result_entry)
            fct_str = json.dumps(fct_dict, ensure_ascii=False) if fct_dict else ""
        except Exception as e:
            logger.warning("[CA_v5] tool summarize failed: %s", e)
        if not fct_str.strip() and not hdl_text.strip():
            hdl_text = "空"

        write_turn_v5(
            self.store, self._session_id, turn, seq,
            role="tool", content=content,
            tool_name=tool_name, tool_call_id=tool_call_id,
            args_json=json.dumps(args) if args else None,
            status=status, duration_ms=duration_ms,
            fct_text=fct_str, hdl_text=hdl_text,
            written_at=time.time(),
        )
        logger.debug("[CA_v5] _on_post_tool_call: wrote %s turn=%d seq=%d status=%s",
                     tool_name, turn, seq, status)

    def _update_fct_v5(self, session_id: str, turn_index: int,
                       fct_text: str, hdl_text: str) -> bool:
        """v5: 写 turn_stream fin 行的 Fct/Hdl 列（assistant_fin），不碰 content。"""
        from .store import update_fin_fct_v5
        return update_fin_fct_v5(self.store, session_id, turn_index, fct_text, hdl_text)
