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
    """注册 CA 插件的生命周期 hooks。"""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)


# ── Hook 分发函数 ──

def _on_session_start(**kwargs: Any) -> None:
    session_id = kwargs.get("session_id", "")
    logger.info("[CA] _on_session_start called: session_id=%s kwargs_keys=%s", session_id, list(kwargs.keys()))
    if not session_id:
        return
    plugin = CAContextAssemblerPlugin()
    # Remove session_id from kwargs to avoid duplicate-arg error
    # since on_session_start(self, session_id, **kwargs) takes it positionally
    hook_kwargs = {k: v for k, v in kwargs.items() if k != "session_id"}
    plugin.on_session_start(session_id, **hook_kwargs)
    with _engines_lock:
        _engines[session_id] = plugin
        logger.info("[CA] _on_session_start: registered plugin for session %s (total engines: %d)", session_id, len(_engines))


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
    _state_file_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_state_file_path(), 'w') as f:
        json.dump(state, f)


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
        db_path.parent.mkdir(parents=True, exist_ok=True)

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
        if self._engine:
            self._engine.reset()
        self._engine_errored = False
        _record_success()

    # ── Hooks ──

    def pre_llm_call(self, **kwargs: Any) -> Optional[str]:
        """调用 CA 引擎的 assemble() 完整管线，从结果中提取上下文摘要文本。

        Hermes pre_llm_call hook 的返回契约是将文本注入到 user message 中。
        assemble() 返回完整消息列表（含 Head/Middle/Tail 分层 + [~/N] 标记），
        我们提取其中的 CA 摘要部分格式化为上下文文本返回。
        """
        if self._engine_errored or not self._engine:
            return None

        user_message = kwargs.get("user_message", "")
        if not user_message:
            return None

        context_length = kwargs.get("context_length", self._context_length)

        try:
            assembled = self._engine.assemble(user_message, context_length)
        except Exception as exc:
            logger.warning("assemble() failed: %s", exc)
            return None

        # 提取 CA 注入的摘要消息（[~/N] 标记），组装为上下文文本
        parts: List[str] = []
        for msg in assembled:
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("[~/"):
                parts.append(content)

        if not parts:
            return None

        # 合并连续相同内容的 CA 摘要（剥离 [~/N/M] 前缀后比较），保留 xN 计数
        merged: List[str] = []
        count = 1
        for p in parts:
            text = p.split("] ", 1)[-1] if "] " in p else p
            if merged:
                last_text = merged[-1].split("] ", 1)[-1] if "] " in merged[-1] else merged[-1]
                if text == last_text:
                    count += 1
                    continue
                elif count > 1:
                    merged[-1] += f" ×{count}"
                    count = 1
            merged.append(p)
        if count > 1:
            merged[-1] += f" ×{count}"
        if len(merged) < len(parts):
            logger.debug("[CA] merged %d → %d consecutive identical summaries", len(parts), len(merged))

        return "\n\n".join(merged)

    def post_llm_call(self, **kwargs: Any) -> None:
        """在 LLM 响应后处理该轮对话，构建未来上下文的摘要。"""
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

        # 传副本给异步线程，避免列表被外部修改导致竞态
        history_copy = list(conversation_history) if conversation_history else []
        logger.info("[CA] post_llm_call: calling process_turn_async for session %s turn %d",
                    self._session_id, self._engine._turn_counter + 1 if self._engine else -1)
        # messages=history_copy 保留了完整对话列表（含 tool_calls），
        # _run_c_stage 用它提取工具轮 + 写 l2_text（供 A-stage 重建）。
        # 写入是累计快照模式，_rebuild_messages_from_cache 自动去重。
        self._engine.process_turn_async(
            user_message, assistant_response,
            history_copy,
            messages=history_copy,
        )
        logger.info("[CA] post_llm_call: process_turn_async returned for session %s",
                    self._session_id)
