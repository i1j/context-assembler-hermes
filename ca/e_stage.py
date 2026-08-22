"""ca/e_stage.py — E-stage 写即落盘 + 代码级摘要 (v5.10)

设计决策: Fct 摘要重构 (PDD 哲学)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-l1-摘要重构-v470-pdd-哲学
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
from .store import (
    read_turn_thinking_rows_v1,
    read_turn_tool_rows_v1,
    read_turn_user_question_v1,
    write_llm_call_v1,
    write_think_card_v1,
    write_turn_v5,
)
from .tool_summarizer import ToolSummarizer
from .blocks import AGENT_REPLY, THINKING, TOOL_CALL_REQUEST, TOOL_CALL_RESULT, USER_MESSAGE
from .meta_marker import extract_request_meta, extract_response_meta
from .think_collect import (
    classify_card_kind,
    has_tool_error_signal,
    make_think_card,
)

logger = logging.getLogger(__name__)


def _normalize_arguments_json(arguments: Any) -> str:
    """将 tool_calls[].function.arguments 归一化为合法 JSON string。

    契约（fix-task-20260816 §3.1）：
      - 合法 JSON string → 原样保留
      - 空串 / 非法 JSON string → "{}"
      - None → "{}"
      - 其他非 str → json.dumps(arg, ensure_ascii=False)
      - 不可序列化（object/set/bytes/循环引用）或 NaN/Infinity → "{}"
    """
    if isinstance(arguments, str):
        if not arguments.strip():
            return "{}"
        try:
            json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return "{}"
        return arguments
    if arguments is None:
        return "{}"
    try:
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return "{}"


def _safe_json_dump(value: Any) -> str:
    """json.dumps 的保守包装：不可序列化 / NaN → "{}"。"""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return "{}"


def _safe_tool_content(result: Any) -> str:
    """将工具 result 转为字符串，且不丢 0/False/[] 等假值。

    字符串原样；None → ""；可 JSON 序列化 → JSON string；
    不可序列化（bytes/set/对象）→ str(result) 兜底，保证 tool 行落库。
    """
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        try:
            return str(result)
        except Exception:
            return ""


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
            tc_args = _normalize_arguments_json(getattr(tc, "arguments", None))
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
            role="assistant", elm_text=thought,
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
                role="tool", elm_text="",
                tool_name=tc_def.get("function", {}).get("name", ""),
                tool_call_id=tc_id,
                status="pending",
                written_at=time.time(),
            )
            self._tool_seq_map[tc_id] = (turn, tool_seq)

        self._seq_counter[turn] += len(tool_defs)
        logger.info("[CA_v5] _on_api_response: wrote %d tool placeholders", len(tool_defs))

    # ═══════════════════════════════════════════════════════════════
    # 决策 44 — E 阶段 v7：近源细颗粒度 + llm_calls + think_trace
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _code_summary_for(text: str) -> Tuple[str, str]:
        """thought/content 的代码级 Fct/Hdl（零 LLM，保守兜底）。"""
        fct_text = ""
        try:
            fct_text = ToolSummarizer.generate_group_summary(text)
        except Exception:
            pass
        if not fct_text.strip():
            fct_text = "空"
        hdl_text = ""
        try:
            raw = (text or "").strip()
            for prefix in ToolSummarizer._TRANSITION_PREFIXES:
                if raw.startswith(prefix):
                    raw = raw[len(prefix):].strip()
                    break
            hdl_text = _safe_truncate(raw, max_len=60) if raw else "空"
        except Exception:
            hdl_text = "空"
        return fct_text, hdl_text or "空"

    def _on_pre_api_request_v7(self, **kwargs: Any) -> None:
        """pre_api_request：记录请求侧元数据（写 llm_calls 时合并）。

        另：补全工具轮中途插入的 user 消息（Hermes active-turn redirect）。

        用户在模型工具执行循环中发新消息时，Hermes 经 _apply_active_turn_redirect
        将其 append 为请求中的真实 user 消息（conversation_loop.py:377-380），
        但 post_api_request 只写 assistant/tool 行，该 user 消息从未落盘
        turn_stream —— select_context 从 turn_stream 重建时丢失，
        表现为「模型遗忘刚刚的对话」（2026-08-22 实锤：turn 3 中途插入
        "我们"/"梳理我们自己建的数据库" 两条 user 消息缺失，turn 编号 3→6 跳变）。

        修复：pre_api_request 是每次 API 调用前触发的 hook（含工具轮中途），
        kwargs.request_messages 是本次实际发送的完整消息列表。对比 turn_stream
        中已有的 user 行（内容级去重，跨 turn），把 request_messages 中新增的
        user 消息补写为独立 user 行（seq 追加到 turn 尾部）。turn_stream 因此
        与 Hermes 实际对话一致，select_context 重建不再丢消息。
        """
        kwargs = kwargs or {}
        self._pending_request_meta = extract_request_meta(kwargs)

        # ── 补全工具轮中途插入的 user 消息（bugfix 2026-08-22）──
        try:
            request_messages = kwargs.get("request_messages") or []
            if not isinstance(request_messages, list) or not request_messages:
                return
            turn = self._current_turn
            if turn <= 0:
                return  # 首轮 user 已由 pre_llm_call 写 seq=0，无需补全
            # 1) 收集 request_messages 中全部 user 文本（保序去重）
            #    区分历史消息 vs 本轮中途插入：以 turn_stream 内容比对为准，
            #    已存在的 user 内容（任何 turn）跳过；未记录的补写到当前 turn。
            seen_texts: List[str] = []
            for m in request_messages:
                if not isinstance(m, dict):
                    continue
                if m.get("role") != "user":
                    continue
                content = m.get("content")
                if isinstance(content, list):
                    # content parts（多模态/结构化）→ 拼接文本
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            parts.append(str(part.get("text", "")))
                        else:
                            parts.append(str(part))
                    content = "".join(parts)
                text = str(content or "").strip()
                if text and text not in seen_texts:
                    seen_texts.append(text)
            if not seen_texts:
                return
            # 2) 读 turn_stream 全部 user 行内容（内容级，跨 turn 比对）
            try:
                from .store import read_turn_stream_all
                rows = read_turn_stream_all(self.store, self._session_id)
                existing_texts = {
                    (r.get("Elm") or "").strip()
                    for r in rows if r.get("role") == "user"
                }
            except Exception:
                return
            # 3) 补写缺失的 user 行（seq 追加到 turn 尾部）
            seq = self._seq_counter.get(turn, 0)
            wrote = 0
            for text in seen_texts:
                if text in existing_texts:
                    continue
                seq += 1
                write_turn_v5(
                    self.store, self._session_id, turn, seq,
                    role="user", elm_text=text,
                    fct_text=text,
                    hdl_text=text[:100],
                    biz_category=None,
                    block_type=USER_MESSAGE,
                    ooda_stage="orient",
                    written_at=time.time(),
                )
                existing_texts.add(text)
                wrote += 1
                logger.info(
                    "[CA_v7] pre_api_request: backfilled mid-turn user msg turn=%d seq=%d: %.60s",
                    turn, seq, text,
                )
            if wrote:
                self._seq_counter[turn] = seq
        except Exception as exc:
            logger.warning("[CA_v7] pre_api_request user backfill failed: %s", exc, exc_info=True)

    def _on_api_request_error_v7(self, **kwargs: Any) -> None:
        """api_request_error：每次失败尝试写一条 failed llm_calls。"""
        kwargs = kwargs or {}
        request_id = kwargs.get("api_request_id", "") or (
            f"{self._session_id}:api:{kwargs.get('api_call_count', 0)}")
        error_obj = kwargs.get("error") or {}
        failure = {
            "type": error_obj.get("type", ""),
            "message": error_obj.get("message", ""),
            "status_code": kwargs.get("status_code"),
            "retry_count": kwargs.get("retry_count"),
            "reason": kwargs.get("reason"),
        }
        write_llm_call_v1(
            self.store, self._session_id, request_id,
            request_seq=kwargs.get("api_call_count"),
            turn=self._current_turn,
            step=kwargs.get("api_call_count"),
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            base_url=kwargs.get("base_url"),
            api_mode=kwargs.get("api_mode"),
            finish_kind="error",
            duration_ms=int(kwargs.get("api_duration", 0) * 1000)
            if isinstance(kwargs.get("api_duration"), (int, float)) else None,
            failure_json=json.dumps(failure, ensure_ascii=False),
            status="failed",
        )

    def _on_api_response_v7(
        self,
        *,
        api_request_id: str = "",
        assistant_message: Any = None,
        api_call_count: int = 0,
        turn_index: int = 0,
        finish_reason: str = "stop",
        usage: Optional[Dict] = None,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        api_mode: str = "",
        api_duration: Optional[float] = None,
        message_count: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """决策 44：每 API 调用拆块写盘 + llm_calls + decision 思考卡。

        - reasoning → THINKING 行（decide）；content → AGENT_REPLY 行（decide）；
        - 工具占位 → tool_call_request 行（act）；
        - 纯对话 stop 不写 turn_stream（fin 由 post_llm_call 写），但仍写 llm_calls。
        """
        turn = turn_index
        meta = extract_response_meta({
            "api_request_id": api_request_id,
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_mode": api_mode,
            "api_call_count": api_call_count,
            "api_duration": api_duration,
            "finish_reason": finish_reason,
            "usage": usage,
            "message_count": message_count,
        }, assistant_message)
        request_id = meta["request_id"] or (
            f"{self._session_id}:api:{api_call_count or 1}")
        reasoning_text = meta["reasoning_text"]
        content_text = meta["content_text"]
        tool_defs = meta["tool_calls"]
        has_tool_turn = bool(tool_defs) or finish_reason == "tool_calls"

        # 保存最近一次 API 元数据：post_llm_call fin 行回填用
        self._last_api_meta = meta
        self._last_api_meta["request_id"] = request_id

        # 流式 pending 计数由插件层合并进 kwargs（乱序安全：max 合并）
        pending = kwargs.get("stream_pending") or {}
        stream_reasoning = int(pending.get("reasoning_chars") or 0)
        stream_text = int(pending.get("text_chars") or 0)
        stream_chunks = int(pending.get("chunk_count") or 0)

        seq = self._seq_counter.get(turn, 0)
        first_seq = seq + 1

        # ── 拆块写 turn_stream ──
        if has_tool_turn or reasoning_text:
            if reasoning_text:
                block_type, elm = THINKING, reasoning_text
            elif content_text:
                block_type, elm = AGENT_REPLY, content_text
            else:
                block_type, elm = TOOL_CALL_REQUEST, ""
            seq += 1
            fct_text, hdl_text = self._code_summary_for(elm)
            # 无工具轮单独出现 reasoning 时用合成 finish_reason，避免 A-stage
            # 把 THINKING 行误判为 fin（stop 是 fin 判定锚）。
            row_finish_reason = finish_reason if has_tool_turn else "reasoning"
            write_turn_v5(
                self.store, self._session_id, turn, seq,
                role="assistant", elm_text=elm,
                tool_calls_json=json.dumps(tool_defs, ensure_ascii=False)
                if tool_defs else None,
                finish_reason=row_finish_reason,
                usage_prompt_tokens=meta["usage_prompt_tokens"],
                usage_completion_tokens=meta["usage_completion_tokens"],
                block_type=block_type,
                ooda_stage="decide" if block_type in (THINKING, AGENT_REPLY) else "act",
                request_id=request_id,
                provider=meta["provider"],
                model=meta["model"],
                reasoning_chars=len(reasoning_text) if block_type == THINKING else 0,
                text_chars=len(content_text) if block_type == AGENT_REPLY else 0,
                fct_text=fct_text,
                hdl_text=hdl_text,
                written_at=time.time(),
            )
            self._seq_counter[turn] = seq
            thinking_seq = seq if block_type == THINKING else None

            # reasoning 与 content 同响应 → 分离第二块（EFLR 边界规则）
            if reasoning_text and content_text:
                seq += 1
                fct2, hdl2 = self._code_summary_for(content_text)
                write_turn_v5(
                    self.store, self._session_id, turn, seq,
                    role="assistant", elm_text=content_text,
                    finish_reason=row_finish_reason,
                    block_type=AGENT_REPLY,
                    ooda_stage="decide",
                    request_id=request_id,
                    provider=meta["provider"],
                    model=meta["model"],
                    text_chars=len(content_text),
                    fct_text=fct2,
                    hdl_text=hdl2,
                    written_at=time.time(),
                )
                self._seq_counter[turn] = seq
        else:
            thinking_seq = None

        # 工具占位（tool_call_request / act）
        for i, tc_def in enumerate(tool_defs, start=1):
            tool_seq = self._seq_counter[turn] + i
            tc_id = tc_def["id"]
            write_turn_v5(
                self.store, self._session_id, turn, tool_seq,
                role="tool", elm_text="",
                tool_name=tc_def.get("function", {}).get("name", ""),
                tool_call_id=tc_id,
                status="pending",
                block_type=TOOL_CALL_REQUEST,
                ooda_stage="act",
                request_id=request_id,
                provider=meta["provider"],
                model=meta["model"],
                written_at=time.time(),
            )
            self._tool_seq_map[tc_id] = (turn, tool_seq)
        if tool_defs:
            self._seq_counter[turn] += len(tool_defs)

        # ── llm_calls（每次 API 调用一行；stream 计数 max 合并）──
        pending_req = getattr(self, "_pending_request_meta", None) or {}
        input_chars = int(pending_req.get("input_chars") or kwargs.get("input_chars") or 0)
        usage_json = json.dumps(meta["usage"], ensure_ascii=False, allow_nan=False) \
            if meta["usage"] else None
        write_llm_call_v1(
            self.store, self._session_id, request_id,
            request_seq=api_call_count or pending_req.get("api_call_count"),
            turn=turn,
            step=api_call_count or pending_req.get("api_call_count"),
            seq=first_seq,
            provider=meta["provider"],
            model=meta["model"],
            base_url=meta["base_url"],
            api_mode=meta["api_mode"],
            messages_count=message_count if message_count is not None
            else pending_req.get("messages_count"),
            input_chars=input_chars,
            reasoning_chars=max(meta["reasoning_chars"], stream_reasoning),
            text_chars=max(meta["text_chars"], stream_text),
            chunk_count=stream_chunks,
            tool_calls_json=meta["tool_calls_json"] if tool_defs else None,
            usage_json=usage_json,
            finish_kind=finish_reason,
            duration_ms=meta["duration_ms"],
            status="completed",
        )

        # ── 思考卡：decision / orient（决策 44 增补：事务首 think 零门槛）──
        if reasoning_text and thinking_seq is not None:
            is_first_think = False
            try:
                existing = read_turn_thinking_rows_v1(
                    self.store, self._session_id, turn)
                is_first_think = not any(r.get("seq") and r["seq"] < thinking_seq
                                         for r in existing)
            except Exception:
                is_first_think = False
            question = read_turn_user_question_v1(self.store, self._session_id, turn)
            if tool_defs:
                card = make_think_card(
                    session_id=self._session_id,
                    turn=turn,
                    seq=thinking_seq,
                    reasoning_text=reasoning_text,
                    tool_calls=tool_defs,
                    card_kind="decision",
                    question_text=question,
                    step=api_call_count or pending_req.get("api_call_count"),
                )
                write_think_card_v1(self.store, card)
                logger.info("[CA_v7] think decision card written turn=%d seq=%d",
                            turn, thinking_seq)
            elif is_first_think:
                # 提问后首段 think（无工具）→ orient 卡，不套 800 字门槛。
                # 该段 think 是事务划分线索，宁可多存，不可丢。
                card = make_think_card(
                    session_id=self._session_id,
                    turn=turn,
                    seq=thinking_seq,
                    reasoning_text=reasoning_text,
                    tool_calls=[],
                    card_kind="orient",
                    question_text=question,
                    step=api_call_count or pending_req.get("api_call_count"),
                )
                write_think_card_v1(self.store, card)
                logger.info("[CA_v7] think orient card written turn=%d seq=%d",
                            turn, thinking_seq)

    def _on_final_response_v7(
        self,
        *,
        session_id: str = "",
        turn_index: int = 0,
        assistant_response: str = "",
    ) -> None:
        """post_llm_call：写 fin 行（agent_reply/decide/is_fin=1）+ conclusion 思考卡。"""
        turn = turn_index
        meta = getattr(self, "_last_api_meta", None) or {}
        seq = self._seq_counter.get(turn, 0) + 1
        self._seq_counter[turn] = seq
        write_turn_v5(
            self.store, session_id or self._session_id, turn, seq,
            role="assistant", elm_text=assistant_response,
            finish_reason="stop",
            usage_prompt_tokens=meta.get("usage_prompt_tokens"),
            usage_completion_tokens=meta.get("usage_completion_tokens"),
            block_type=AGENT_REPLY,
            ooda_stage="decide",
            request_id=meta.get("request_id"),
            provider=meta.get("provider"),
            model=meta.get("model"),
            text_chars=len(assistant_response),
            is_fin=1,
            written_at=time.time(),
        )
        logger.info("[CA_v7] _on_final_response: wrote fin turn=%d seq=%d", turn, seq)

        # ── conclusion 思考卡（DSH K0 门槛）──
        try:
            thinking_rows = read_turn_thinking_rows_v1(self.store, self._session_id, turn)
            tool_rows = read_turn_tool_rows_v1(self.store, self._session_id, turn)
            tool_error = has_tool_error_signal(tool_rows, turn)
            question = read_turn_user_question_v1(self.store, self._session_id, turn)
            for row in thinking_rows:
                reasoning_text = row.get("Elm") or ""
                if not reasoning_text:
                    continue
                kind = classify_card_kind(
                    raw_len=len(reasoning_text),
                    tool_calls=[],
                    is_fin=True,
                    tool_error=tool_error,
                    reasoning_text=reasoning_text,
                )
                if kind == "conclusion":
                    card = make_think_card(
                        session_id=self._session_id,
                        turn=turn,
                        seq=seq,
                        reasoning_text=reasoning_text,
                        tool_calls=[],
                        card_kind="conclusion",
                        question_text=question,
                    )
                    write_think_card_v1(self.store, card)
                    logger.info("[CA_v7] think conclusion card written turn=%d seq=%d",
                                turn, seq)
        except Exception as exc:
            logger.warning("[CA_v7] conclusion think card failed: %s", exc)

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

        content = _safe_tool_content(result)

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
        if not fct_str.strip():
            fct_str = "空"
        if not hdl_text.strip():
            hdl_text = "空"
        if "空" in (fct_str, hdl_text):
            logger.debug("[CA_v5] _on_post_tool_call: empty Fct/Hdl, using '空' fallback turn=%d seq=%d", turn, seq)

        # 保留占位行已写入的近源元数据（provider/model 不回退为 NULL）
        placeholder_meta = (None, None, None)
        try:
            cur = self.store.conn.execute(
                "SELECT request_id, provider, model FROM turn_stream "
                "WHERE session_id=? AND turn=? AND seq=?",
                (self._session_id, turn, seq),
            )
            placeholder_meta = cur.fetchone() or (None, None, None)
        except Exception:
            pass
        row_request_id = api_request_id or placeholder_meta[0]
        row_provider = placeholder_meta[1]
        row_model = placeholder_meta[2]

        write_turn_v5(
            self.store, self._session_id, turn, seq,
            role="tool", elm_text=content,
            tool_name=tool_name, tool_call_id=tool_call_id,
            args_json=_safe_json_dump(args) if args is not None else None,
            status=status, duration_ms=duration_ms,
            block_type=TOOL_CALL_RESULT,
            ooda_stage="observe",
            request_id=row_request_id,
            provider=row_provider,
            model=row_model,
            result_chars=len(content),
            error_text=(error_message or error_type) if status not in (None, "ok") else None,
            fct_text=fct_str, hdl_text=hdl_text,
            written_at=time.time(),
        )
        logger.debug("[CA_v5] _on_post_tool_call: wrote %s turn=%d seq=%d status=%s",
                     tool_name, turn, seq, status)

    def _update_fct_v5(self, session_id: str, turn_index: int, fin_seq: int,
                       fct_text: str, hdl_text: str) -> bool:
        """v5: 写特定 fin 行 (turn, fin_seq) 的 Fct/Hdl 列。"""
        from .store import update_fin_fct_v5
        return update_fin_fct_v5(self.store, session_id, turn_index, fin_seq, fct_text, hdl_text)
