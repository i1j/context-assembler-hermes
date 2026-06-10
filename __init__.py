"""
plugins/context_engine/ca_assembler/__init__.py — Hermes 插件适配 (v4.4.0)

适配 A‑stage 解耦：Hooks 路径——pre_llm_call 返回上下文文本注入 user message。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保本地 ca/ 子目录优先于 venv 的 ca.pth
_plugin_dir = Path(__file__).resolve().parent
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))

from ca import session_manager
from ca.config import Config

logger = logging.getLogger(__name__)

# ── 模块级引擎注册表（session_id → plugin 实例）──
_engines: Dict[str, "CAContextAssemblerPlugin"] = {}
_engines_lock = threading.Lock()


def register(ctx) -> None:
    """注册 CA 插件的生命周期 hooks 和工具轮数据采集 hooks。"""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    # PR2: 工具轮数据采集钩子
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)


# ── Hook 分发函数 ──

def _on_session_start(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    logger.info("[CA] _on_session_start called: session_id=%s kwargs_keys=%s", session_id, list(kwargs.keys()))
    if not session_id:
        return
    try:
        plugin = CAContextAssemblerPlugin()
        # Remove session_id from kwargs to avoid duplicate-arg error
        # since on_session_start(self, session_id, **kwargs) takes it positionally
        hook_kwargs = {k: v for k, v in kwargs.items() if k != "session_id"}
        plugin.on_session_start(session_id, **hook_kwargs)
        with _engines_lock:
            _engines[session_id] = plugin
            logger.info("[CA] _on_session_start: registered plugin for session %s (total engines: %d)", session_id, len(_engines))
    except Exception as exc:
        logger.error("[CA] _on_session_start failed for session %s: %s", session_id, exc, exc_info=True)


def _on_session_end(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    logger.info("[CA] _on_session_end called: session_id=%s (no-op — this hook fires every turn, "
                "not just at true session end; actual cleanup is atexit/gateway expiry)", session_id)
    # Hermes fires on_session_end at the end of every run_conversation(),
    # i.e. once per turn, not only when the session truly ends.  Do NOT
    # destroy the engine here — the next turn needs it.
    pass


def _on_session_reset(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    logger.info("[CA] _on_session_reset called: session_id=%s", session_id)
    try:
        with _engines_lock:
            if session_id:
                plugin = _engines.pop(session_id, None)
                instances = [plugin] if plugin else []
            else:
                # 没有 session_id 时（如全局 /reset），重置所有引擎
                instances = list(_engines.values())
                _engines.clear()
        for p in instances:
            if p is None:
                continue
            try:
                p.on_session_reset()
            except Exception:
                pass
    except Exception as exc:
        logger.error("[CA] _on_session_reset error: %s", exc)


def _on_pre_llm_call(**kwargs: Any) -> Optional[str]:
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin:
        logger.info("[CA] _on_pre_llm_call: NO plugin for session %s (total engines in registry: %d)", session_id, len(_engines))
        return None
    if plugin._engine_errored:
        logger.info("[CA] _on_pre_llm_call: plugin errored for session %s", session_id)
        return None
    logger.info("[CA] _on_pre_llm_call: session=%s calling assemble()", session_id)
    return plugin.pre_llm_call(**kwargs)


def _on_post_llm_call(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin:
        logger.info("[CA] _on_post_llm_call: NO plugin for session %s (total engines: %d)", session_id, len(_engines))
        return
    if plugin._engine_errored:
        logger.info("[CA] _on_post_llm_call: plugin errored for session %s", session_id)
        return
    logger.info("[CA] _on_post_llm_call: session=%s um=%s ar_len=%d",
                session_id,
                (kwargs.get("user_message", "") or "")[:60],
                len(kwargs.get("assistant_response", "") or ""))
    plugin.post_llm_call(**kwargs)


# ── PR2: 工具轮数据采集钩子 ──

def _on_post_api_request(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_api_response(
            api_request_id=kwargs.get("api_request_id", ""),
            assistant_message=kwargs.get("assistant_message"),
            api_call_count=kwargs.get("api_call_count", 0),
            turn_index=plugin._engine._turn_counter + 1,
            finish_reason=kwargs.get("finish_reason", "stop"),
            usage=kwargs.get("usage"),
        )
    except Exception as exc:
        logger.warning("[CA] _on_post_api_request error: %s", exc)


def _on_pre_tool_call(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_pre_tool_call(
            tool_call_id=kwargs.get("tool_call_id", ""),
            tool_name=kwargs.get("tool_name", ""),
            args=kwargs.get("args", {}),
            api_request_id=kwargs.get("api_request_id", ""),
        )
    except Exception as exc:
        logger.warning("[CA] _on_pre_tool_call error: %s", exc)


def _on_post_tool_call(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_post_tool_call(
            tool_call_id=kwargs.get("tool_call_id", ""),
            tool_name=kwargs.get("tool_name", ""),
            args=kwargs.get("args", {}),
            result=kwargs.get("result"),
            status=kwargs.get("status", "ok"),
            duration_ms=kwargs.get("duration_ms", 0),
            error_type=kwargs.get("error_type"),
            error_message=kwargs.get("error_message"),
            api_request_id=kwargs.get("api_request_id", ""),
        )
    except Exception as exc:
        logger.warning("[CA] _on_post_tool_call error: %s", exc)


# ── 断路器 ──

_STATE_FILE_PATTERN = re.compile(r"^\.ca_assembler_state_(\d+)\.json$")


def _state_file_path() -> Path:
    pid = os.getpid()
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except ImportError:
        base = Path.home() / ".hermes"
    return base / f".ca_assembler_state_{pid}.json"


def _pid_exists(pid: int) -> bool:
    """检查 PID 是否仍在运行（POSIX /proc）。"""
    return os.path.isdir(f"/proc/{pid}")


def _cleanup_stale_state_files() -> None:
    """删除不再运行的进程留下的断路器状态文件，防止无限堆积。"""
    base = _state_file_path().parent
    if not base.is_dir():
        return
    current_pid = os.getpid()
    for entry in base.iterdir():
        m = _STATE_FILE_PATTERN.match(entry.name)
        if m:
            pid = int(m.group(1))
            if pid != current_pid and not _pid_exists(pid):
                try:
                    entry.unlink()
                except OSError:
                    pass


def _read_state() -> Dict:
    try:
        with open(_state_file_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"failures": 0, "retry_after": None}


def _write_state(state: Dict) -> None:
    try:
        _cleanup_stale_state_files()
    except OSError:
        pass
    try:
        path = _state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(state, f)
    except OSError as exc:
        logger.warning("[CA] Failed to write breaker state (read-only filesystem?): %s", exc)


def _record_failure() -> None:
    state = _read_state()
    state["failures"] = state.get("failures", 0) + 1
    if state["failures"] >= 3:
        state["retry_after"] = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        ).isoformat()
    _write_state(state)


def _record_success() -> None:
    _write_state({"failures": 0, "retry_after": None})


def is_available() -> bool:
    state = _read_state()
    if state.get("retry_after"):
        try:
            retry_time = datetime.datetime.fromisoformat(state["retry_after"])
        except (ValueError, TypeError):
            # 格式不兼容（如旧版 naive 格式），视为可用
            _record_success()
            return True
        now = datetime.datetime.now(datetime.timezone.utc)
        # 兼容旧版 naive datetime（替换 now 的时区信息后比较）
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=datetime.timezone.utc)
        if now < retry_time:
            return False
        else:
            _record_success()
            return True
    return state["failures"] < 3


# ── 插件类 ──

class CAContextAssemblerPlugin:
    """CA 引擎的 Hermes 插件包装。

    每个 session 有一个独立实例，由 _on_session_start 创建。
    """

    def __init__(self) -> None:
        self._engine = None
        self._engine_errored = False
        self._session_id = ""
        self._context_length: int = Config.CONTEXT_LENGTH
        self._saved_history: Optional[Dict[int, str]] = None
        self._saved_history_snapshot: Optional[List[Dict]] = None

    # ── 生命周期 ──

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """创建并初始化 CA 引擎。"""
        self._engine_errored = False
        self._session_id = session_id

        if not is_available():
            logger.error("CAContextAssembler unavailable due to repeated failures, short-circuiting.")
            self._engine_errored = True
            return

        try:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        except ImportError:
            hermes_home = str(Path.home() / ".hermes")

        db_path = Path(hermes_home) / "ca_cache" / f"{session_id}.db"
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 只读文件系统降级：使用临时目录
            import tempfile
            tmpdir = Path(tempfile.mkdtemp(prefix="ca_cache_"))
            db_path = tmpdir / f"{session_id}.db"
            logger.warning("[CA] ca_cache dir not writable, falling back to %s", db_path)

        try:
            self._engine = session_manager.get(session_id, str(db_path))
        except Exception as exc:
            _record_failure()
            logger.error("Failed to create CA engine: %s", exc, exc_info=True)
            self._engine = None
            self._engine_errored = True
            return

        _record_success()
        model = kwargs.get("model", "")
        self._context_length = Config.context_length_for_model(model)
        self._engine.context_length = self._context_length
        logger.info("CA plugin started for session %s (model=%s context_length=%d)",
                    session_id, model or "?", self._context_length)

    def on_session_end(self, **kwargs: Any) -> None:
        """清理引擎资源。"""
        if self._engine:
            self._engine.wait_for_pending(timeout=Config.LLM_TIMEOUT + 10)
        session_manager.remove(self._session_id)
        self._engine = None

    def on_session_reset(self) -> None:
        """重置引擎状态（/new 或 /reset 时调用）。"""
        self._saved_history = None
        self._saved_history_snapshot = None
        if self._engine:
            self._engine.reset()
        self._engine_errored = False
        _record_success()

    # ── Hooks ──

    def pre_llm_call(self, **kwargs: Any) -> Optional[str]:
        """CA core entry: plan computation + 1:1 aligned context injection.

        Injection mode controlled by Config.HISTORY_INJECTION:
          replace (default) - mutate conversation_history in-place
          append           - return summary text injected into user message
          off              - no injection, data accumulation only
        """
        if self._engine_errored or not self._engine:
            return None

        user_message = kwargs.get("user_message", "")
        if not user_message:
            return None

        context_length = kwargs.get("context_length", self._context_length)

        # 在引擎汇编前保存原始 conversation_history 快照，
        # 供 _build_messages_from_plan 旁路从原始消息注入 L2 原文。
        _orig_history = kwargs.get("conversation_history", [])
        if isinstance(_orig_history, list) and _orig_history:
            self._engine._original_messages = [dict(m) for m in _orig_history]
        else:
            self._engine._original_messages = None

        try:
            result = self._engine._compute_assemble_plan(user_message, context_length)
        except Exception as exc:
            logger.warning("_compute_assemble_plan() failed: %s", exc)
            return None

        conversation_history = kwargs.get("conversation_history", [])
        if not isinstance(conversation_history, list):
            return None

        if not conversation_history:
            return None

        injection_mode = Config.HISTORY_INJECTION
        if injection_mode == "off":
            logger.info("[CA] pre_llm_call: injection disabled (off mode)")
            return None

        if injection_mode == "append":
            logger.info("[CA] pre_llm_call: append mode")
            return self._annotation_mode(result, conversation_history)

        # replace (default)
        logger.info("[CA] pre_llm_call: replace mode")
        return self._mutation_mode(result, conversation_history)

    def _annotation_mode(self, result, conversation_history: list) -> Optional[str]:
        """annotation 模式：从 outcomes 提取非 None 文本拼接返回，不碰 history。"""
        if isinstance(result, list):
            return None  # degraded
        outcomes = self._engine._build_aligned_outcomes(result.plan, conversation_history, bypass_turns=result.bypass_turns)
        parts = [o for o in outcomes if o]
        if not parts:
            return None
        return "\n\n".join(parts)

    def _mutation_mode(self, result, conversation_history: list) -> Optional[str]:
        """mutation 模式：1:1 对齐 → 替换/跳过/保留 → 快照保存。"""
        if isinstance(result, list):
            # degraded: 不做事
            self._saved_history_snapshot = None
            return None

        # 1. 构建 1:1 outcomes
        try:
            outcomes = self._engine._build_aligned_outcomes(result.plan, conversation_history, bypass_turns=result.bypass_turns)
        except Exception as exc:
            logger.warning("_build_aligned_outcomes() failed: %s", exc)
            return None

        # 2. 保存完整快照（浅拷贝消息字典）
        self._saved_history_snapshot = [{**m} for m in conversation_history]
        self._saved_history = None

        # 3. 应用 outcomes：替换 content 或移除行
        replaced = 0
        skipped = 0
        # 从后往前处理，确保移除时索引不偏移
        for i in range(len(outcomes) - 1, -1, -1):
            outcome = outcomes[i]
            if outcome is None:
                continue  # 保留原文
            if outcome == "":
                del conversation_history[i]
                skipped += 1
            else:
                conversation_history[i]["content"] = outcome
                replaced += 1

        logger.info(
            "[CA] mutation: replaced %d + skipped %d outcomes",
            replaced, skipped,
        )
        return None

    def post_llm_call(self, **kwargs: Any) -> None:
        """在 LLM 响应后处理该轮对话，构建未来上下文的摘要。

        PR2 职责分离：
        1. 先 flush_tool_buffer() 写入工具行（增量采集）
        2. 再 process_turn_async() 异步处理对话轮摘要
        """
        if self._engine_errored or not self._engine:
            logger.info("[CA] post_llm_call skipped: engine not available (errored=%s engine=%s)",
                       self._engine_errored, self._engine is not None)
            return

        user_message = kwargs.get("user_message", "")
        assistant_response = kwargs.get("assistant_response", "")
        conversation_history = kwargs.get("conversation_history", [])

        logger.info(
            "[CA] post_llm_call: session=%s um=%s ar_len=%d history=%d",
            self._session_id,
            (user_message or "")[:60],
            len(assistant_response or ""),
            len(conversation_history or []),
        )

        if not user_message and not assistant_response:
            logger.info("[CA] post_llm_call: empty um and ar, skipped")
            return

        # PR2 Step 1: flush tool buffer（增量采集的工具数据写入 store）
        try:
            rows_written = self._engine.flush_tool_buffer(user_message=user_message)
            if rows_written > 0:
                logger.info("[CA] post_llm_call: flushed %d tool rows to store", rows_written)
        except Exception as exc:
            logger.warning("[CA] post_llm_call: flush_tool_buffer error: %s", exc)

        # Restore original history from mutation mode snapshot（快照全量恢复）
        _snapshot = getattr(self, "_saved_history_snapshot", None)
        if _snapshot is not None:
            conversation_history.clear()
            conversation_history.extend({**m} for m in _snapshot)
            logger.info("[CA] post_llm_call: restored %d messages from snapshot", len(_snapshot))
            self._saved_history_snapshot = None
        else:
            # fallback: 旧版 _saved_history 恢复（仅恢复 content）
            _saved = getattr(self, "_saved_history", None)
            if _saved:
                _restored = 0
                for msg in conversation_history or []:
                    key = id(msg)
                    if key in _saved:
                        orig = _saved.pop(key)
                        if msg.get("content") != orig:
                            msg["content"] = orig
                            _restored += 1
                if _restored:
                    logger.info("[CA] post_llm_call: restored %d original message contents (legacy)", _restored)
                self._saved_history = None

        # 传副本给异步线程，避免列表被外部修改导致竞态
        history_copy = list(conversation_history) if conversation_history else []
        logger.info("[CA] post_llm_call: calling process_turn_async for session %s turn %d",
                    self._session_id, self._engine._turn_counter + 1 if self._engine else -1)
        # PR2: 不再传 messages=history_copy，_run_c_stage 不再遍历 messages 写工具行
        self._engine.process_turn_async(
            user_message, assistant_response,
            history_copy,
        )
        logger.info("[CA] post_llm_call: process_turn_async returned for session %s",
                    self._session_id)
