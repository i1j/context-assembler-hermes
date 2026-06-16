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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保本地 ca/ 子目录优先于 venv 的 ca.pth
_plugin_dir = Path(__file__).resolve().parent
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))

from ca import session_manager
from ca.config import Config

# ── bg_review 检测（当前轮类型识别，用于跳过 A-stage 组装）──
try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"

logger = logging.getLogger(__name__)

# ── 模块级引擎注册表（session_id → plugin 实例）──
_engines: Dict[str, "CAContextAssemblerPlugin"] = {}
_engines_lock = threading.Lock()


def register(ctx) -> None:
    """注册 CA 插件 v5.0 hooks。"""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call_v5)
    ctx.register_hook("post_llm_call",    _on_post_llm_call_v5)
    ctx.register_hook("post_api_request", _on_post_api_request_v5)
    ctx.register_hook("pre_tool_call",    _on_pre_tool_call_v5)
    ctx.register_hook("post_tool_call",   _on_post_tool_call_v5)


# ── Hook 分发函数 ──

def _on_session_start(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    logger.info("[CA] _on_session_start called: session_id=%s kwargs_keys=%s", session_id, list(kwargs.keys()))
    if not session_id:
        return
    try:
        plugin = CAContextAssemblerPlugin()
        # Log the session's starting threshold
        if plugin._engine:
            _t = plugin._engine._topic_jaccard_threshold
            logger.info("[CA] session_start: threshold=%.4f for session %s", _t, session_id)
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



# ── PR2: 工具轮数据采集钩子 ──




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
        # 持久化会话理想阈值
        if self._engine:
            self._engine.persist_ideal_threshold()
        self._saved_history = None
        self._saved_history_snapshot = None
        if self._engine:
            self._engine.reset()
        self._engine_errored = False
        _record_success()

    # ── Hooks ──






    # ═════════════════════════════════════════════════════
    # v5.0 — A-stage 替换 + E-stage final 写入
    # ═════════════════════════════════════════════════════

    def _simple_mutation_mode_v5(self, conversation_history: list) -> Optional[str]:
        """v5: 从 turn_stream 查 Fct 替换。无 plan 依赖。"""
        if not conversation_history:
            return None
        self._saved_history_snapshot = [{**m} for m in conversation_history]
        self._saved_history = None
        store = self._engine.store
        sid = self._session_id

        tail_boundary = 0
        _user_count = 0
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 3:
                    tail_boundary = i
                    break

        turn = 0
        seq = 0
        replaced = 0
        skipped = 0

        for i, msg in enumerate(conversation_history):
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                turn += 1
                seq = 0
                continue
            seq += 1
            if i >= tail_boundary:
                continue
            if role == "user":
                continue
            if role == "assistant" and not msg.get("tool_calls"):
                continue

            from ca.store import read_fct_v5
            l1 = read_fct_v5(store, sid, turn, seq)
            if l1:
                msg["content"] = l1
                replaced += 1
            else:
                skipped += 1

        logger.info("[CA_v5] simple_mutation: replaced %d + skipped %d (tail=%d, turns=%d)",
                    replaced, skipped, tail_boundary, turn)
        return None

    def pre_llm_call_v5(self, **kwargs: Any) -> Optional[str]:
        """v5 A-stage: 直接 _simple_mutation_mode_v5。"""
        conversation_history = kwargs.get("conversation_history", [])
        if not isinstance(conversation_history, list) or not conversation_history:
            return None
        return self._simple_mutation_mode_v5(conversation_history)

    def post_llm_call_v5(self, **kwargs: Any) -> None:
        """v5 E-stage final: 写 asst_fin → 快照恢复 → C-stage。"""
        if self._engine_errored or not self._engine:
            return
        user_message = kwargs.get("user_message", "")
        assistant_response = kwargs.get("assistant_response", "")
        conversation_history = kwargs.get("conversation_history", [])
        if not user_message and not assistant_response:
            return

        engine = self._engine
        turn = engine._current_turn
        seq = engine._seq_counter.get(turn, 0) + 1
        engine._seq_counter[turn] = seq

        from ca.store import write_turn_v5
        write_turn_v5(
            engine.store, self._session_id, turn, seq,
            role='assistant', content=assistant_response,
            finish_reason='stop',
            written_at=time.time(),
        )
        logger.info("[CA_v5] post_llm_call: wrote asst_fin turn=%d seq=%d", turn, seq)

        _snapshot = getattr(self, "_saved_history_snapshot", None)
        if _snapshot is not None:
            for i, orig_dict in enumerate(_snapshot):
                if i >= len(conversation_history):
                    break
                ch = conversation_history[i]
                orig_content = orig_dict.get("content")
                if ch.get("content") != orig_content:
                    ch["content"] = orig_content
            self._saved_history_snapshot = None

        engine.process_turn_f_stage(turn)
        logger.info("[CA_v5] post_llm_call: process_turn_f_stage called for turn %d", turn)


# ═══════════════════════════════════════════════════════════════
# v5.0 Hook 分发函数（模块级，0 缩进）
# ═══════════════════════════════════════════════════════════════

def _on_pre_llm_call_v5(**kwargs: Any) -> Optional[str]:
    """v5: 写 seq 0 (user L2) → A-stage 替换。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return None

    user_message = kwargs.get("user_message", "")
    conversation_history = kwargs.get("conversation_history", [])
    engine = plugin._engine

    turn = len([m for m in conversation_history if m.get("role") == "user"])
    engine._current_turn = turn
    engine._seq_counter[turn] = 0
    engine._tool_seq_map.clear()

    bg = False
    try:
        from tools.skill_provenance import get_current_write_origin
        bg = (get_current_write_origin() == "background_review")
    except ImportError:
        pass

    from ca.store import write_turn_v5
    write_turn_v5(
        engine.store, session_id, turn, seq=0,
        role='user', content=user_message,
        biz_category='bg_review' if bg else None,
        written_at=time.time(),
    )
    logger.info("[CA_v5] pre_llm_call: wrote seq 0 turn=%d bg=%s", turn, bg)

    if bg:
        return None

    return plugin.pre_llm_call_v5(**kwargs)


def _on_post_api_request_v5(**kwargs: Any) -> None:
    """v5: 写 thought L2 + tool 占位行。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_api_response_v5(
            api_request_id=kwargs.get("api_request_id", ""),
            assistant_message=kwargs.get("assistant_message"),
            api_call_count=kwargs.get("api_call_count", 0),
            turn_index=plugin._engine._current_turn,
            finish_reason=kwargs.get("finish_reason", "stop"),
            usage=kwargs.get("usage"),
        )
    except Exception as exc:
        logger.warning("[CA_v5] _on_post_api_request failed: %s", exc, exc_info=True)


def _on_post_tool_call_v5(**kwargs: Any) -> None:
    """v5: 回填 tool L2 + per-tool L1。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_post_tool_call_v5(
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
        logger.warning("[CA_v5] _on_post_tool_call failed: %s", exc, exc_info=True)


def _on_post_llm_call_v5(**kwargs: Any) -> None:
    """v5: 写 final assistant → snapshot 恢复 → C-stage。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin.post_llm_call_v5(**kwargs)
    except Exception as exc:
        logger.warning("[CA_v5] _on_post_llm_call failed: %s", exc, exc_info=True)




def _on_pre_tool_call_v5(**kwargs: Any) -> None:
    """v5: 无操作（tool 占位行已在 post_api_request 写入）。"""
    pass
