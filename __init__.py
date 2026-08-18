"""plugins/ca_assembler/__init__.py — Hermes 插件适配 (v5.10)

设计决策: P-001 (Plugin 层职责分离)
  viking://resources/projects/context-assembler/decisions/decision-points-wiki.md#toc-plugin-适配-amp-断路器
  - 注册 8 hooks (5 生命周期 + 3 工具轮 hook)
  - pre_llm_call_v5: E-stage 写入 + topic 检测 + recall 注入
  - post_llm_call_v5: E-stage final 写入 + F-stage 触发

适配 A‑stage 解耦：select_context 从 turn_stream DB 重建 conv_history；
pre_llm_call 返回上下文文本注入 user message。
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
from ca.blocks import USER_MESSAGE
from ca.config import Config
from ca.grade import Grade, TopicGrade
from ca.refinement import IdleRefinementDaemon
from .topic_manager import TopicGradeManager

# ── ContextEngine ABC 壳 ──
try:
    from agent.context_engine import ContextEngine
except ImportError:
    ContextEngine = object  # fallback: duck-typing

# ── bg_review 检测（当前轮类型识别，用于跳过 A-stage 组装）──
try:
    from tools.skill_provenance import get_current_write_origin
except ImportError:
    def get_current_write_origin() -> str:
        return "unknown"

logger = logging.getLogger(__name__)
# 日志同时写到 /tmp/ca_assembler.log 以便诊断
if not any(isinstance(h, logging.FileHandler) and h.baseFilename == "/tmp/ca_assembler.log"
           for h in logger.handlers):
    _log_handler = logging.FileHandler("/tmp/ca_assembler.log", encoding="utf-8")
    _log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _log_handler.setLevel(logging.DEBUG)
    logger.addHandler(_log_handler)
    logger.setLevel(logging.DEBUG)

# ── 模块级引擎注册表（session_id → plugin 实例）──
_engines: Dict[str, "CAContextAssemblerPlugin"] = {}
_engines_lock = threading.Lock()

# ── 断路器状态（文件持久化，每 PID 独立文件）──
_FAILURE_THRESHOLD = 3
_RETRY_AFTER_SECONDS = 3600  # 1 小时冷却


def _state_file_path() -> Path:
    """返回当前进程的断路器状态文件路径。"""
    try:
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
    except ImportError:
        hermes_home = str(Path.home() / ".hermes")
    return Path(hermes_home) / f".ca_assembler_state_{os.getpid()}.json"


def _read_state() -> dict:
    """读取断路器状态，不存在时返回默认值。"""
    path = _state_file_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"failures": data.get("failures", 0), "retry_after": data.get("retry_after")}
    except (OSError, json.JSONDecodeError):
        pass
    return {"failures": 0, "retry_after": None}


def _pid_exists(pid: int) -> bool:
    """检查 PID 是否存活（POSIX 通过 /proc）。"""
    if pid <= 0:
        return False
    if os.name == "posix":
        return os.path.isdir(f"/proc/{pid}")
    return False


def _cleanup_stale_state_files() -> None:
    """清理已死进程的断路器状态文件。"""
    state_path = _state_file_path()
    state_dir = state_path.parent
    try:
        if not state_dir.exists():
            return
    except OSError:
        return
    pattern = re.compile(r"^\.ca_assembler_state_(\d+)\.json$")
    try:
        for entry in state_dir.iterdir():
            if not entry.is_file():
                continue
            m = pattern.match(entry.name)
            if m:
                pid = int(m.group(1))
                if pid != os.getpid() and not _pid_exists(pid):
                    try:
                        entry.unlink()
                    except OSError:
                        pass
    except OSError:
        pass


def _write_state(data: dict) -> None:
    """写入断路器状态，写入前清理 stale 文件。"""
    try:
        _cleanup_stale_state_files()
    except Exception:
        pass  # 清理失败不影响写入
    path = _state_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("[CA] breaker state write failed: %s", exc)


def _record_success() -> None:
    """记录一次成功，重置断路器。"""
    old = _read_state()
    old["failures"] = 0
    old["retry_after"] = None
    _write_state(old)


def _record_failure() -> None:
    """记录一次失败，超阈值时启动冷却。"""
    old = _read_state()
    old["failures"] = old.get("failures", 0) + 1
    if old["failures"] >= _FAILURE_THRESHOLD:
        retry_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=_RETRY_AFTER_SECONDS)
        old["retry_after"] = retry_at.isoformat()
    _write_state(old)


def is_available() -> bool:
    """断路器是否允许操作（失败 < 阈值 或 冷却已过）。"""
    state = _read_state()
    if state["failures"] < _FAILURE_THRESHOLD:
        return True
    retry_after = state.get("retry_after")
    if not retry_after:
        # 旧文件无 retry_after 时视为冷却有效
        return False
    try:
        if isinstance(retry_after, str):
            retry_at = datetime.datetime.fromisoformat(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            if now >= retry_at:
                _record_success()  # 冷却结束，自动恢复
                return True
        return False
    except (ValueError, TypeError):
        return False


def register(ctx) -> None:
    """注册 CA 插件 hooks 和 ContextEngine ABC。

    - 13 个 hooks（决策 44：8 旧 + pre_api_request/api_request_error/on_stream_*）。
    - CE 壳已注册，select_context 驱动 A-stage（方向 B）：每轮从 turn_stream DB
      重建 conv_history；should_compress 恒 False，阻断 Hermes 前检压缩路径。
    - 注册使用 1 参签名 ctx.register_context_engine(_ce_engine)（Hermes plugins.py:1898）。
    """
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call_v5)
    ctx.register_hook("post_llm_call",    _on_post_llm_call_v5)
    ctx.register_hook("post_api_request", _on_post_api_request_v5)
    ctx.register_hook("pre_tool_call",    _on_pre_tool_call_v5)
    ctx.register_hook("post_tool_call",   _on_post_tool_call_v5)
    # 决策 44：更接近云端 LLM 响应的数据源头
    ctx.register_hook("pre_api_request",  _on_pre_api_request_v7)
    ctx.register_hook("api_request_error", _on_api_request_error_v7)
    ctx.register_hook("on_stream_start",  _on_stream_start_v7)
    ctx.register_hook("on_stream_delta",  _on_stream_delta_v7)
    ctx.register_hook("on_stream_end",    _on_stream_end_v7)
    # ── CE 壳注册（1 参签名，Hermes plugins.py:1898）──
    ctx.register_context_engine(_ce_engine)


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



# ── PR2: 工具轮数据采集钩子 ──




# ── 插件类 ──

class CAContextEngine(ContextEngine):
    """ContextEngine ABC 壳 — CA 的 context engine 注册形态。

    select_context 每轮从 turn_stream DB 重建 conv_history（方向 B），
    should_compress 恒 False 以阻断 Hermes 前检压缩路径，
    compress() 作为手动 /compress 回退路径，复用 v5.10 数据管道
    （turn_stream + topic_grade + Fct/Hdl/Elm）构建消息列表。

    作为 singleton 与 _engines 注册表协同工作：
      CE 的 on_session_start 记录 session_id，
      compress() 通过 session_id 查找对应的 CAContextAssemblerPlugin 实例。
    """

    def __init__(self) -> None:
        self._session_id: str = ""
        # ContextEngine protocol fields
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.threshold_tokens: int = 0
        self.context_length: int = 0
        self.compression_count: int = 0
        self.protect_first_n: int = 3
        self.protect_last_n: int = 6
        self.threshold_percent: float = 0.75

    # -- ContextEngine: identity ------------------------------------------

    @property
    def name(self) -> str:
        return "ca_assembler"

    # -- ContextEngine: core ----------------------------------------------

    def update_from_response(self, usage: dict) -> None:
        """记录 token 用量（仅监控，不触发压缩）。"""
        self.last_prompt_tokens = usage.get("prompt_tokens", 0) or 0
        self.last_completion_tokens = usage.get("completion_tokens", 0) or 0
        self.last_total_tokens = usage.get("total_tokens", 0) or 0

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """恒 False — 阻断 Hermes 前检压缩路径（turn_context.py:930）。

        方向 B A-stage 由 select_context 每轮驱动（conversation_loop.py:2054），
        compress() 仅保留手动 /compress 回退路径。
        """
        return False

    def select_context(
        self,
        request_messages: list,
        *,
        conversation_messages: list = None,
        incoming_message: dict = None,
        budget_tokens: int = 0,
    ) -> Optional[list]:
        """每轮选择上下文：从 turn_stream DB 重建 conv_history（方向 B）。

        返回精确语义：
          - bg 轮 / store 空 → None（Hermes fail-open 原样返回）
          - plugin 缺失 / errored / 内部异常 / 非法返回（空列表 / 非 dict 元素 /
            缺 role 的畸形 dict，如 [{"bad":1}]）→ request_messages（原样，fail-open）
          - 正常 → 重建列表（当前轮 user content 经 R10 替换保留注入；
            R10 取 request_messages 中最后一条 user，工具轮 user 不在尾部仍替换）
          - 重建结果无任何 role=="user" 消息 → request_messages
            （fail-open，防 bg-only DB 等无 user 重建结果丢当前用户）
        错误 = 内部异常全部捕获（fail-open）；入参形状（request_messages 为 dict 列表）由 Hermes 契约保证。
        （bg 检测函数调用异常除外——`get_current_write_origin()` 运行期异常仅捕获 ImportError 上抛，与 pre_llm_call 同源形态，保持同构不改实现）
        """
        # ① bg 轮检测（与 pre_llm_call 同源）
        try:
            from tools.skill_provenance import get_current_write_origin
            if get_current_write_origin() == "background_review":
                return None
        except ImportError:
            pass

        # ② plugin 查找
        plugin = self._get_plugin()
        if not plugin or plugin._engine_errored or not plugin._engine:
            logger.warning("[CA] select_context: plugin unavailable; falling back to request_messages")
            return request_messages

        # ③ store 判空（plugin 可用才可判空）
        try:
            from ca.store import read_turn_stream_all
            rows = read_turn_stream_all(plugin._engine.store, self._session_id)
        except Exception as exc:
            logger.warning("[CA] select_context: store read failed: %s", exc, exc_info=True)
            return request_messages
        if not rows:
            return None

        # ④ 保留 Hermes 首条 system 消息
        system_msg = request_messages[0] if (
            request_messages and request_messages[0].get("role") == "system"
        ) else None

        # ⑤ 从 turn_stream DB 重建 conv_history
        try:
            new_conv = plugin._engine._build_conv_history_v6(
                plugin._topic_mgr,
                system_message=system_msg,
            )
        except Exception as exc:
            logger.warning("[CA] select_context: build failed: %s", exc, exc_info=True)
            return request_messages

        # ⑥ 非法返回校验（强于 Hermes 侧 isinstance dict 校验）
        if (not new_conv
                or not all(isinstance(m, dict) and m.get("role") for m in new_conv)
                or not any(m.get("role") == "user" for m in new_conv)):
            logger.warning("[CA] select_context: invalid build result; falling back to request_messages")
            return request_messages

        # ⑦ R10 注入替换：用 request_messages 中最后一条 user 的 content
        # 替换重建结果中的最后一条 user（工具轮 user 不在 request 尾部仍替换）
        last_user = None
        for m in reversed(request_messages):
            if m.get("role") == "user":
                last_user = m
                break
        if last_user is not None:
            for m in reversed(new_conv):
                if m.get("role") == "user":
                    m["content"] = last_user.get("content", "")
                    break

        # ⑧ 返回重建列表
        return new_conv

    def compress(
        self,
        messages: list,
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> list:
        """CE 管线入口（v6）：从 turn_stream DB 重建 conv_history（方向 B）。

        不再修改 Hermes 传来的 messages；改用 _build_conv_history_v6
        从 CA 自己的 turn_stream 数据库构造优化后的 conv_history。
        """
        plugin = self._get_plugin()
        if not plugin or plugin._engine_errored or not plugin._engine:
            return messages

        # 守卫：如果消息未增长跳过
        last_len = getattr(self, '_last_compress_msg_len', 0)
        if not force and len(messages) <= last_len:
            return messages

        # 从 Hermes 第一条保留 system 消息（如有）
        system_msg = messages[0] if (messages and messages[0].get("role") == "system") else None

        # v6：从 turn_stream DB 重建 conv_history
        topic_mgr = plugin._topic_mgr
        try:
            new_conv = plugin._engine._build_conv_history_v6(
                topic_mgr,
                system_message=system_msg,
            )
        except Exception as exc:
            logger.warning("[CA] compress: build failed: %s", exc, exc_info=True)
            return messages

        # 与 select_context 同构的 fail-open 守卫：手动 /compress 也不能用
        # 空列表 / 畸形元素 / 无 user 的重建结果替换真实消息。
        if (not new_conv
                or not all(isinstance(m, dict) and m.get("role") for m in new_conv)
                or not any(m.get("role") == "user" for m in new_conv)):
            logger.warning("[CA] compress: invalid build result; keeping original messages")
            return messages

        self._last_compress_msg_len = len(messages)
        self.compression_count += 1
        return new_conv

    # -- ContextEngine: lifecycle -----------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """记录当前 session_id。插件实例由 _on_session_start hook 创建。"""
        self._session_id = session_id

    def on_session_end(self, session_id: str = "", messages: list = None) -> None:
        """透传到插件实例（清理 store 资源）并从注册表移除。

        Hermes 的插件 hook on_session_end 每轮都会触发（不能用于清账），
        而 ContextEngine.on_session_end 只在真实会话边界触发 —— 这里才是
        正确的 `_engines` 移除点，防止长跑 gateway 会话轮转后注册表泄漏。

        ⚠️ bg-review fork 例外（2026-08-18 实证）：Hermes 的 background_review
        fork 与主 agent 共享 session_id（background_review.py L1097），fork
        关闭时 shutdown_memory_provider → 本方法会被调用（background_review.py
        L1219 → run_agent.py L4302）。若无条件清理，主 agent 的 plugin 被
        误删，select_context 永久 fallback（A-stage 失效）。fork 运行在名为
        "bg-review" 的线程（run_agent.py L1856），此处按线程名跳过清理。
        """
        # bg-review fork 关闭路径：跳过清理，保护主 agent 的 plugin
        if threading.current_thread().name.startswith("bg-review"):
            logger.info(
                "[CA] on_session_end: bg-review fork teardown (thread=%s); "
                "skipping plugin cleanup for session=%s",
                threading.current_thread().name, session_id or self._session_id,
            )
            return
        plugin = self._get_plugin()
        if plugin:
            try:
                plugin.on_session_end()
            except Exception as exc:
                logger.warning("[CA] on_session_end plugin cleanup failed: %s", exc)
        sid = session_id or self._session_id
        if sid:
            with _engines_lock:
                _engines.pop(sid, None)

    def on_session_reset(self) -> None:
        """重置 CE 状态（不触及 plugin 实例）。"""
        self._session_id = ""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0
        # 跨会话不能复用消息长度守卫（新会话消息数 <= 旧会话时非 force
        # compress 会被误跳过）。
        self._last_compress_msg_len = 0

    # -- ContextEngine: model switch --------------------------------------

    def update_model(
        self,
        model: str = "",
        context_length: int = 0,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """更新 context_length + threshold_tokens。"""
        self.context_length = context_length
        self.threshold_tokens = int(max(context_length * self.threshold_percent, 1))

    # -- ContextEngine: status / display ---------------------------------

    def get_status(self) -> dict:
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, self.last_prompt_tokens / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }

    # -- Internal: plugin lookup ------------------------------------------

    def _get_plugin(self):
        """从 _engines 注册表中查找当前 session 的 plugin 实例。"""
        if not self._session_id:
            return None
        with _engines_lock:
            return _engines.get(self._session_id)


# CAContextEngine singleton — 由 register() 注册到 Hermes
_ce_engine = CAContextEngine()

class CAContextAssemblerPlugin:
    """CA 引擎的 Hermes 插件包装。

    每个 session 有一个独立实例，由 _on_session_start 创建。
    """

    def __init__(self) -> None:
        self._engine = None
        self._engine_errored = False
        self._session_id = ""
        self._context_length: int = Config.CONTEXT_LENGTH
        self._saved_history_snapshot: Optional[List[Dict]] = None
        self._topic_mgr: Optional[TopicGradeManager] = None
        self._pending_topic_summarize: List[Dict[str, Any]] = []  # queue of pending topics
        self._pending_backfill: Optional[Dict[str, Any]] = None    # turn 1 scan backfill
        self._last_topic_id: Optional[int] = None
        self._bg_turn: bool = False  # 当前轮是否为 bg（方向 B 跳过保护）
        self._session_recall_done: bool = False  # 首轮 recall 已注入
        self._candidate_themes: Optional[list] = None  # v6.5.3: 切换注入的候选 theme（快照进 pending）
        self._graph_lock = threading.Lock()  # v6.5.3: graph.json 并发写锁
        self._cleanup_done: bool = False         # on_session_start 清账已完成
        # 决策 44：on_stream_* 异步 worker 与 post_api_request 无顺序保证，
        # pending 只存流式计数；llm_calls 用 UPSERT max 合并。
        self._stream_pending: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._stream_lock = threading.Lock()
        # L4 空闲精炼守护线程
        self._refinement_daemon: Optional[IdleRefinementDaemon] = None

    # ── 生命周期 ──

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """创建并初始化 CA 引擎。"""
        self._engine_errored = False
        self._session_id = session_id

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
        self._topic_mgr = TopicGradeManager(
            self._engine.store,
            self._engine.embed_client,
            session_id=session_id,
            cache=getattr(self._engine, "cache", None),  # v7.1 BUG-08: 复用 Fct 语义 embedding
        )
        logger.info("CA plugin started for session %s (model=%s context_length=%d)",
                    session_id, model or "?", self._context_length)

        # 后台：清上一会话的账（补缺摘要 → L2 wiki merge → L3 graphify）
        if Config.TOPIC_SUMMARIZE_ENABLED:
            threading.Thread(
                target=self._run_session_start_cleanup,
                daemon=True,
                name="CA-Cleanup",
            ).start()

    def on_session_end(self, **kwargs: Any) -> None:
        """清理引擎资源。"""
        if self._engine:
            self._engine.wait_for_pending(timeout=Config.LLM_TIMEOUT + 10)
        session_manager.remove(self._session_id)
        self._engine = None

    def _run_session_start_cleanup(self) -> None:
        """后台线程：新会话启动时检查上一会话的未完成话题。

        用户打字间隙执行；缺 topic_id→turns 持久化映射时保守跳过补缺
        （skip 优于把多话题块错并成一条 strand）。
        """
        if not Config.TOPIC_SUMMARIZE_ENABLED:
            self._cleanup_done = True
            return
        try:
            from ca.store import (
                get_last_session_meta, get_topic_strand_status,
            )

            profile = Config.HERMES_PROFILE

            # ── 1. 补缺扫描：上次会话最后一个话题是否缺摘要 ──
            last_meta = get_last_session_meta(profile)
            if last_meta and last_meta["session_id"] != self._session_id:
                last_sid = last_meta["session_id"]
                last_tid = last_meta["last_topic_id"]
                status = get_topic_strand_status(last_sid, last_tid)
                if status != "completed":
                    # session_meta 只存 last_turn/last_topic_id，没有持久化
                    # topic_id → turns 映射；把 1..old_max_turn 全部塞进 last topic
                    # 会把多个话题块错误合并成一条 strand。保守原则（4B 超时/失败
                    # 不 assume same topic merge，skip 优于错并）：缺映射时跳过补缺，
                    # 留待 reprocess 工具用持久化数据重跑。
                    logger.warning(
                        "[CA_TOPIC_SUM] Session-start backfill skipped for session %s "
                        "topic %d: no persisted topic→turns mapping, conservative skip",
                        last_sid, last_tid,
                    )

            # ── 2. v6.5: theme merge 已在 strand 生成时同步（run_theme_merge 内含于
            #    _run_topic_summarize），此处无需批量路径。L3 graphify 待下次会话适配。──

        except Exception as exc:
            logger.warning("[CA] _run_session_start_cleanup failed: %s", exc)
        finally:
            self._cleanup_done = True
            logger.info("[CA] Session-start cleanup complete")
            # ── 3. 懒启动 L4 空闲精炼守护线程 ──
            if Config.REFINEMENT_ENABLED:
                if self._refinement_daemon is None:
                    from ca.refinement import IdleRefinementDaemon
                    self._refinement_daemon = IdleRefinementDaemon(plugin_ref=self)
                self._refinement_daemon.start()

    def on_session_reset(self) -> None:
        """重置引擎状态（/new 或 /reset 时调用）。"""
        # 如果有未提交的话题摘要，尝试提交
        if Config.TOPIC_SUMMARIZE_ENABLED:
            # 情况 A：队列中有 pending 话题
            for pending in self._pending_topic_summarize:
                self._trigger_summarize_async(pending)
            self._pending_topic_summarize.clear()

            # 情况 B：无 pending 但有当前话题（单话题块无 topic_switch）
            if self._topic_mgr and self._topic_mgr._current_topic_id is not None:
                current_id = self._topic_mgr._current_topic_id
                turn_to_topic = getattr(self._topic_mgr, "_turn_to_topic", {})
                old_turns = sorted([
                    t for t, tid in turn_to_topic.items()
                    if tid == current_id
                ])
                if old_turns:
                    logger.info("[CA_TOPIC_SUM] Flushing last topic %d on session reset (turns %s)",
                                current_id, old_turns)
                    self._trigger_summarize_async({
                        "topic_id": current_id,
                        "turns": old_turns,
                        "switch_turn": max(old_turns),
                    })

        self._pending_topic_summarize.clear()
        self._pending_backfill = None
        self._last_topic_id = None
        self._session_recall_done = False
        self._saved_history_snapshot = None
        if self._topic_mgr:
            self._topic_mgr.reset()
        if self._engine:
            self._engine.reset()
        self._engine_errored = False
        _record_success()

    # ── Hooks ──






    # ── Hooks ──
    # v5.0 — select_context 重建 + E-stage final 写入
    # ═════════════════════════════════════════════════════


    # ── 话题摘要管 ──

    def _trigger_summarize_async(self, topic_data: Dict[str, Any]) -> None:
        """启动后台线程生成话题摘要并写入 ca_topics.db。
        与 post_llm_call_v5 中的触发逻辑相同。
        """
        if not self._engine:
            logger.warning("[CA_TOPIC_SUM] No engine for topic %d, skipping", topic_data.get("topic_id"))
            return
        session_id = self._session_id
        threading.Thread(
            target=self._run_topic_summarize,
            args=(session_id, topic_data),
            daemon=True,
            name="CA-TopicSummarize",
        ).start()
        logger.info("[CA_TOPIC_SUM] Thread started for topic %d (turns=%s)",
                    topic_data.get("topic_id"), topic_data.get("turns"))

    def _run_topic_summarize(self, session_id: str, topic_data: Dict[str, Any],
                             store: Any = None) -> None:
        """后台线程执行话题摘要：等 F-stage → 读 Fct → 4B → 写 strand。

        v6.4: 摘要单元从话题块 → strand（一个话题块可写多条 strand）。
        占位逻辑移除（strand 是 INSERT-only，无 pending 占位概念）；
        hollow 判定改为块级（能识别出 strand 就不可能空洞）。

        替代旧 _fire_ov_submit 的 HTTP 上传逻辑。
        """
        topic_id = topic_data["topic_id"]
        turns = topic_data["turns"]
        switch_turn = topic_data["switch_turn"]
        logger.info("[CA_TOPIC_SUM] _run_topic_summarize: topic %d turns=%s switch=%d",
                    topic_id, turns, switch_turn)

        if not self._engine:
            logger.warning("[CA_TOPIC_SUM] No engine for topic %d, skipping", topic_id)
            return

        engine = self._engine
        try:
            engine.wait_for_pending(timeout=Config.LLM_TIMEOUT + 30)
        except Exception:
            pass  # 超时不影响——用已有 Fct 数据

        # 收集该话题块各轮的 Fct（BUG-03/D6：backfill 传入旧会话 store，
        # 否则查询 last_sid 恒空 → 空摘要兜底）
        from ca.store import collect_turn_fcts, get_turn_ca_rows
        read_store = store or engine.store
        turns_data = collect_turn_fcts(read_store, session_id, turns)
        if not turns_data:
            logger.warning("[CA_TOPIC_SUM] No Fct data for topic %d, writing empty summary", topic_id)
            turns_data = [{"turn": t, "hdl": "", "changes": [], "stage_tags": []} for t in turns]

        # 获取 topic title（从 topic_mgr 或默认）
        title = ""
        topic_mgr = self._topic_mgr
        if topic_mgr:
            try:
                td = getattr(topic_mgr, "_topic_data", {})
                topic_info = td.get(topic_id, {})
                title = topic_info.get("title", "")
            except Exception:
                pass

        profile = Config.HERMES_PROFILE

        # 4B 调用（v8: 注入预算 → 迭代提炼 + hdl 兜底）
        from ca.topic_summary import summarize_topic_chunk
        # v6.5.3: 该话题块开始时的候选 theme（快照自 pending，防异步覆盖）
        candidate_themes = topic_data.get("candidate_themes") or None
        summary = summarize_topic_chunk(
            turns_data,
            title=title,
            candidate_themes=candidate_themes,
            max_chars=Config.TOPIC_SUMMARY_MAX_CHARS,
        )

        if not summary:
            logger.warning("[CA_TOPIC_SUM] Topic %d summarization failed (4B error)",
                          topic_id)
            return

        # ── 质量检查：块级空洞不入库（v6.4: 能识别出 strand 就不可能空洞）──
        _EMPTY_TITLES = {"", "无", "无新增", "无新内容"}
        _title = summary.get("title", "")
        _changes = summary.get("changes", [])
        _key_facts = summary.get("key_facts", [])
        _strands = summary.get("strands", [])
        _has_content = bool(
            any(st.get("ooda") for st in _strands if isinstance(st, dict))
        ) or bool(_changes) or bool(_key_facts)
        _is_hollow = (not summary.get("consumable", True)) or \
                     (not _title or _title.strip() in _EMPTY_TITLES) or \
                     (not _has_content)
        if _is_hollow:
            logger.info("[CA_TOPIC_SUM] Topic %d not consumable, marking as skip "
                        "(title=%s, %d changes, %d facts)",
                        topic_id, str(_title)[:40], len(_changes), len(_key_facts))
            from ca.store import write_strand_summary
            write_strand_summary(
                session_id, topic_id, profile,
                hdl=f"skip: {_title[:60] if _title else 'empty'}",
                turns=turns,
                ooda_json="{}", changes_json="[]", key_facts_json="[]",
                status="skip",
            )
            return

        # v6.4: 写入 strand（每条独立记录）+ upsert session meta
        from ca.store import write_strand_summary, upsert_session_meta
        from ca.topic_summary import _flatten_strand_ooda as _flatten

        written = 0
        theme_ready: list[dict] = []   # v6.5: 同步归并的 strand 输入
        for st in _strands:
            if not isinstance(st, dict):
                # 4B 偶发输出畸形元素（str 非 dict）→ 跳过，不中断话题摘要
                logger.info("[CA_TOPIC_SUM] strand %r 非 dict，跳过",
                            str(st)[:40])
                continue
            st_hdl = st.get("hdl", "") or summary.get("hdl", "")
            st_turns = st.get("turns") or turns
            st_ooda = st.get("ooda") or {}
            # v7 (决策 39): 块首提问快照 → 提问云增量维护。
            # 来源 = strand.turns[0] 的 user Elm（决策 39 §六：strand.turns[0] 的 user 消息）。
            first_query = ""
            try:
                _ft = (st_turns or turns)[0]
                for _seq, _role, _fin, _tc, _elm, _fct, _hdl in get_turn_ca_rows(
                        read_store, session_id, _ft):
                    if _seq == 0 and _role == "user":
                        first_query = str(_elm or "").strip()
                        break
            except Exception:
                pass
            strand_id = write_strand_summary(
                session_id, topic_id, profile,
                hdl=st_hdl,
                turns=st_turns,
                ooda_json=json.dumps(st_ooda, ensure_ascii=False),
                changes_json=json.dumps(_flatten([st]), ensure_ascii=False),
                key_facts_json=json.dumps(summary.get("key_facts", []), ensure_ascii=False),
                status=summary.get("status", "completed"),
            )
            if strand_id is None:
                continue

            # ── L1: strand centroid（embed hdl + ooda 语义文本，v6.4 规范）──
            try:
                embed_text = " ".join(filter(None, [
                    st_hdl,
                    *[
                        item
                        for group in st_ooda.values()
                        if isinstance(group, list)
                        for item in group if isinstance(item, str)
                    ],
                ]))
                if embed_text.strip() and engine.embed_client:
                    vec = engine.embed_client.embed(embed_text[:500])
                    if vec:
                        from ca.store import update_strand_centroid
                        update_strand_centroid(
                            strand_id,
                            json.dumps(vec, ensure_ascii=False),
                        )
            except Exception as exc:
                logger.warning("[CA_TOPIC_SUM] strand %d centroid write failed: %s",
                              strand_id, exc)
            # v6.5.3 + v7: strand 归属判断（reality_ref 必须 ∈ 候选，防 4B 幻觉越界）
            valid_refs = {t.get("reality_id") for t in (candidate_themes or [])}
            st_ref = st.get("theme_ref")
            if st_ref is not None and st_ref not in valid_refs:
                logger.info("[CA_TOPIC_SUM] strand %s theme_ref=%s 越界候选，丢弃",
                            st_hdl[:20], st_ref)
                st_ref = None
            theme_ready.append({
                "hdl": st_hdl,
                "turns": st_turns,
                "topic_id": topic_id,
                "session_id": session_id,
                "strand_id": strand_id,
                "ooda": st_ooda,
                "changes": _flatten([st]),
                "key_facts": summary.get("key_facts", []),
                "theme_ref": st_ref,
                "query_text": first_query,
            })
            written += 1

        if written:
            upsert_session_meta(session_id, profile, switch_turn, topic_id)
            logger.info("[CA_TOPIC_SUM] Topic %d written (%d strands, hdl=%s)",
                        topic_id, written, summary.get("hdl", "")[:40])
        else:
            logger.warning("[CA_TOPIC_SUM] Topic %d: all strand writes failed", topic_id)

        # ── v7 (决策 41): strand 生成时同步归并到 reality（两段式：S 候选→4B 决策→4B 更新）──
        if theme_ready:
            try:
                from ca.reality import run_reality_merge
                from ca.store import get_wiki_threshold, load_all_realities
                existing = load_all_realities()
                # v6.5.3 (2026-08-02 用户确认) + v7: 归并优先级 = 本块注入的候选 reality 优先
                # （topic_data.candidate_themes = 切换时语义检索的 3 个参考 reality）→
                # 再考虑其它 reality → 最后新建。
                priority_realities = topic_data.get("candidate_themes") or None
                stats = run_reality_merge(
                    theme_ready, existing,
                    embed_client=engine.embed_client,
                    threshold=get_wiki_threshold(),
                    max_chars=Config.TOPIC_SUMMARY_MAX_CHARS,
                    profile=profile,
                    priority_realities=priority_realities,
                )
                logger.info("[CA_REALITY] sync merge: %s", stats)
                # v7: reality 生成/融合后增量同步 graphify 边
                rid_list = stats.get("reality_ids") or []
                if rid_list:
                    from ca.graphify_sync import sync_realities_to_graph
                    graph_path = (Path(__file__).resolve().parent
                                  / "graphify-out" / "graph.json")
                    with self._graph_lock:
                        sync_realities_to_graph(rid_list, graph_path)
                    # v7 (决策 38): 块级共现边记录（复合键幂等）+ 共现边入图
                    try:
                        from ca.store import (query_cooccurrences,
                                              record_block_cooccurrences)
                        record_block_cooccurrences(
                            session_id, topic_id, rid_list,
                            profile=profile)
                        edges, _, _ = query_cooccurrences(profile=profile)
                        if edges:
                            from ca.graphify_sync import \
                                sync_cooccurrences_to_graph
                            with self._graph_lock:
                                sync_cooccurrences_to_graph(edges, graph_path)
                    except Exception as exc:
                        logger.warning(
                            "[CA_REALITY] cooccurrence sync failed: %s", exc)
            except Exception as exc:
                logger.warning("[CA_REALITY] sync merge failed: %s", exc)

    @staticmethod
    def _estimate_conv_tokens(conv_hist: list) -> int:
        from ca.a_stage import AStageMixin
        return AStageMixin._estimate_conv_tokens(conv_hist)

    # ── L2 → v6.5: 话题 theme merge 已前移为 strand 生成时同步（ca/theme.py run_theme_merge）──
    # 原 _run_wiki_merge（session-start 批量路径）已移除（2026-08 用户确认默认值 2）。
    # _graphify_incremental 保留，待 themes 适配后由 theme 链路触发（下次会话）。

    def _graphify_incremental(self, theme_ids: list[int]) -> None:
        """将本次 theme create/merge 改动的 theme 增量同步到 graph.json。

        v6.5.3: 适配 themes 表（原实现读旧表 topic_wiki，v6.5 后失效）。
        节点: theme_{theme_id}；边: topic_{sid}_S{strand_id} → theme（merged_into）。
        幂等合并，跳过已存在节点/边。线程安全（_graph_lock）。
        """
        if not theme_ids:
            return
        try:
            from ca.graphify_sync import sync_themes_to_graph
            graph_path = Path(__file__).resolve().parent / "graphify-out" / "graph.json"
            with self._graph_lock:
                sync_themes_to_graph(theme_ids, graph_path)
        except Exception as exc:
            logger.warning("[CA_GRAPH] _graphify_incremental failed: %s", exc)


    def post_llm_call_v5(self, **kwargs: Any) -> None:
        """v6 orientation B: 写 asst_fin → F-stage（不再恢复快照，因 compress 不修改消息）。"""
        if self._engine_errored or not self._engine:
            return
        user_message = kwargs.get("user_message", "")
        assistant_response = kwargs.get("assistant_response", "")
        conversation_history = kwargs.get("conversation_history", [])
        if not user_message and not assistant_response:
            return

        engine = self._engine
        turn = engine._current_turn

        # 决策 44：fin 行由 E-stage v7 统一写（agent_reply/decide/is_fin=1，
        # 元数据来自最近一次 post_api_request 的 llm_calls 记录）。
        try:
            engine._on_final_response_v7(
                session_id=self._session_id,
                turn_index=turn,
                assistant_response=assistant_response,
            )
        except Exception as exc:
            logger.warning("[CA_v7] _on_final_response_v7 failed: %s", exc)
        seq = engine._seq_counter.get(turn, 0)
        logger.info("[CA_v6] post_llm_call: wrote asst_fin turn=%d seq=%d", turn, seq)

        # v6: 方向 B — compress 不修改 Hermes 消息，无需 _full_backup 或 _saved_history_snapshot 恢复
        # _full_backup / _saved_history_snapshot 已废弃，略过备份恢复逻辑

        engine.process_turn_f_stage(turn, fin_seq=seq)
        logger.info("[CA_v6] post_llm_call: process_turn_f_stage called for turn %d fin_seq %d", turn, seq)

        # 话题摘要（队列中的 pending）
        if Config.TOPIC_SUMMARIZE_ENABLED:
            if self._pending_topic_summarize:
                session_id = self._session_id
                n_pending = len(self._pending_topic_summarize)  # BUG-06: clear 前计数
                for pending in list(self._pending_topic_summarize):
                    threading.Thread(
                        target=self._run_topic_summarize,
                        args=(session_id, pending),
                        daemon=True,
                        name="CA-TopicSummarize",
                    ).start()
                self._pending_topic_summarize.clear()
                logger.info("[CA_TOPIC_SUM] Started %d summarize threads", n_pending)

        # OV 话题摘要提交（已删除，话题摘要由 _run_topic_summarize 处理）


# ═══════════════════════════════════════════════════════════════
# v5.0 Hook 分发函数（模块级，0 缩进）
# ═══════════════════════════════════════════════════════════════


def _refresh_engine(session_id: str, plugin: "CAContextAssemblerPlugin") -> bool:
    """Refresh plugin._engine from session_manager to prevent stale engine
    after SessionManager TTL cleanup (CR-009).

    After ~30min idle the shared ContextAssembler engine may be destroyed by
    SessionManager._cleanup_loop, leaving plugin._engine.store.conn closed.
    Re-acquiring from session_manager.get() returns the cached engine if alive,
    or creates a new one (fresh SQLite connection to the same DB file) if evicted.

    Returns True if engine is usable, False if refresh failed (plugin errored).
    """
    try:
        from ca import session_manager as _sm
        _hp = (Path.home() / ".hermes")
        try:
            from hermes_constants import get_hermes_home
            _hp = Path(get_hermes_home())
        except ImportError:
            pass
        _db_path = str(_hp / "ca_cache" / f"{session_id}.db")
        old_engine = plugin._engine
        plugin._engine = _sm.get(session_id, _db_path)
        # CR-009 补全：TTL 清理后 _sm.get() 会新建 engine 实例，但
        # plugin._topic_mgr 仍持有旧实例的 store/embed_client/cache（连接已关闭）。
        # 话题切换时的 _compute_centroids / Fct embedding 缓存会因此全部降级。
        # 保留 topic_mgr 的 turn→topic 状态，只重绑底层依赖。
        if plugin._engine is not old_engine:
            if plugin._topic_mgr is not None:
                plugin._topic_mgr._store = plugin._engine.store
                plugin._topic_mgr._embed_client = plugin._engine.embed_client
                plugin._topic_mgr._cache = getattr(plugin._engine, "cache", None)
            # 新引擎的 _current_turn/_seq_counter/_tool_seq_map 为空。若 TTL 驱逐
            # 发生在 turn 中途（长 LLM 调用），post_api/post_tool/post_llm 会把行写
            # 到 turn 0；从 turn_stream 恢复轮内状态，避免覆盖既有数据。
            try:
                from ca.store import read_turn_stream_all
                rows = read_turn_stream_all(plugin._engine.store, session_id)
                seq_counter: Dict[int, int] = {}
                tool_seq_map: Dict[str, tuple] = {}
                for row in rows:
                    t = int(row.get("turn") or 0)
                    s = int(row.get("seq") or 0)
                    if t not in seq_counter or s > seq_counter[t]:
                        seq_counter[t] = s
                    if (row.get("role") == "tool"
                            and row.get("status") == "pending"
                            and row.get("tool_call_id")):
                        tool_seq_map[row["tool_call_id"]] = (t, s)
                plugin._engine._current_turn = max(seq_counter) if seq_counter else 0
                plugin._engine._seq_counter = seq_counter
                plugin._engine._tool_seq_map = tool_seq_map
            except Exception as exc:
                logger.warning("[CA] _refresh_engine: state restore failed: %s", exc)
        plugin._engine_errored = False
        return True
    except Exception as exc:
        plugin._engine_errored = True
        logger.warning("[CA] Engine refresh failed for session %s: %s", session_id, exc)
        return False


def _on_pre_llm_call_v5(**kwargs: Any) -> Optional[str]:
    """v6: 写 seq 0 (user Elm) → 话题检测（mutation 由 select_context 驱动的 _build_conv_history_v6 替代）。

    方向 B（2026-06-28）：
      - bg 轮：完全跳过，不增 turn、不写 DB、不调检测
      - 非 bg 轮：写 DB seq 0 → 话题检测 → 返回（conv_history 由 select_context 从 DB 重建）
    """
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return None

    # ── bg 检测：完全跳过 ──
    try:
        from tools.skill_provenance import get_current_write_origin
        if get_current_write_origin() == "background_review":
            plugin._bg_turn = True
            logger.info("[CA_v6] pre_llm_call: bg turn skipped (no turn increment, no write)")
            return None
    except ImportError:
        pass

    plugin._bg_turn = False

    # ── Engine refresh: prevent stale engine after SessionManager TTL cleanup (CR-009) ──
    if not _refresh_engine(session_id, plugin):
        return None

    user_message = kwargs.get("user_message", "")
    conversation_history = kwargs.get("conversation_history", [])
    engine = plugin._engine

    turn = len([m for m in conversation_history if m.get("role") == "user"])
    engine._current_turn = turn
    engine._seq_counter[turn] = 0
    engine._tool_seq_map.clear()

    from ca.store import read_turn_elm_rows, write_turn_v5

    # BUG-09 防覆写：turn 由 conversation_history 的 user 数重算，重复触发
    # （重放/重试）时同一 (turn, seq=0) 会被再次写入。行不可变约束
    # （02-store「写入即不可撤销」）→ 已存在内容相同的 user Elm 则跳过写入；
    # 内容不同（新消息/引擎恢复路径）保持 REPLACE 覆盖语义（既有测试契约）。
    _duplicate_replay = False
    try:
        for _seq, _role, _elm, *_rest in read_turn_elm_rows(
                engine.store, session_id, turn):
            if (_seq == 0 and _role == "user"
                    and str(_elm or "").strip() == str(user_message).strip()):
                _duplicate_replay = True
                break
    except Exception:
        pass
    if _duplicate_replay:
        logger.info("[CA_v6] pre_llm_call: turn=%d user Elm identical, "
                    "skip rewrite (行不可变，BUG-09)", turn)
    else:
        write_turn_v5(
            engine.store, session_id, turn, seq=0,
            role='user', elm_text=user_message,
            fct_text=user_message,
            hdl_text=user_message[:100],
            biz_category=None,
            block_type=USER_MESSAGE,
            ooda_stage="orient",
            written_at=time.time(),
        )
        logger.info("[CA_v6] pre_llm_call: wrote seq 0 turn=%d", turn)

    # 话题切换时的跨会话 recall（仅 FAR 切换触发）
    recall_str: Optional[str] = None

    # ── 首轮 recall（依赖 on_session_start 清账完成 → 从 wiki 语义检索）──
    # 设计依据 CR-6（docs/current-code-state-2026-08-06.md:180-186）：
    # 清账已在建立新会话时（on_session_start）后台触发（补缺摘要→run_reality_merge
    # 写 realities），首轮 recall 读 realities 表依赖清账完成（有写-读依赖）。
    # v7.1 (2026-08-08) 实测：清账 67-120ms 内完成（无 backfill 时）→ 去掉 5s 轮询，
    # 直接检查 _cleanup_done：已完成→注入（零阻塞）；未完成（罕见 backfill）→
    # 立即跳过 recall（非阻塞，延后到下次 FAR 切换）。
    # REALITY_INJECT_ENABLED=0 时短路整个注入管道（v7.1 总闸）。
    if (turn == 1 and not plugin._session_recall_done and user_message
            and Config.TOPIC_SUMMARIZE_ENABLED and Config.REALITY_INJECT_ENABLED):
        try:
            if not plugin._cleanup_done:
                logger.warning("[CA_WIKI] session-start cleanup not done; "
                               "skipping recall injection (non-blocking)")
            else:
                # 注入拣选——embed 首条用户消息 → 提问云形心散度距离范围 → 4B 拣选（决策 41 §2.4b）
                from ca.inject import pick_injection_realities
                q_emb = engine.embed_client.embed(user_message)
                if q_emb:
                    wiki_entries = pick_injection_realities(
                        user_message, q_emb, Config.HERMES_PROFILE,
                        limit=Config.TOPIC_SUMMARY_RECALL_LIMIT,
                        exclude_session_id=session_id,
                    )
                    if wiki_entries:
                        recall_str = _format_wiki_carryover(wiki_entries)
                        entries_detail = ", ".join(
                            f"id={e.get('reality_id')}({e.get('name','')[:40]})"
                            for e in wiki_entries
                        )
                        logger.info("[CA_WIKI] Session-start wiki recall injected (%d chars, %d entries): %s",
                                    len(recall_str), len(wiki_entries), entries_detail)
                    # v6.5.3: 保存候选 theme，供后续话题块 summarize 判断归属
                    plugin._candidate_themes = wiki_entries or None
            plugin._session_recall_done = True
        except Exception as exc:
            logger.warning("[CA_WIKI] session-start recall failed: %s", exc)
            plugin._session_recall_done = True

    # ── v6：话题检测（mutation 已剥离，由 select_context 驱动的 _build_conv_history_v6 替代）──
    if plugin._topic_mgr and engine and turn > 0 and user_message:
        from ca.store import get_turn_ca_rows
        total_tokens = CAContextAssemblerPlugin._estimate_conv_tokens(conversation_history)
        ca_rows = get_turn_ca_rows(engine.store, session_id, turn)
        switched = plugin._topic_mgr.detect(turn, ca_rows, user_message, total_tokens=total_tokens)
        if switched:
            # 嵌入服务异常（连接/超时/HTTPError）→ 降级 None：grade_on_switch 的
            # 无向量分支把旧话题保守定为 REL（多保留上下文），不误判 FAR；
            # 因此本轮 FAR recall 不触发（4B 超时降级保守原则，BUG-02）。
            try:
                q_emb = engine.embed_client.embed(user_message)
            except Exception as exc:
                logger.warning("[CA_v6] embed failed on topic switch, "
                               "degrading to no-vector: %s", exc)
                q_emb = None
            plugin._topic_mgr.grade_on_switch(q_emb, user_message)
            # 打包前一个话题数据 → 本地话题摘要
            old_topic_id = plugin._last_topic_id
            plugin._last_topic_id = plugin._topic_mgr._current_topic_id
            if old_topic_id is not None and Config.TOPIC_SUMMARIZE_ENABLED and engine:
                old_turns = sorted([
                    t for t, tid in plugin._topic_mgr._turn_to_topic.items()
                    if tid == old_topic_id
                ])
                if old_turns:
                    plugin._pending_topic_summarize.append({
                        "topic_id": old_topic_id,
                        "turns": old_turns,
                        "switch_turn": turn,
                        # v6.5.3: 快照该话题块开始时的候选 theme（异步执行防覆盖）
                        "candidate_themes": plugin._candidate_themes,
                    })
                    logger.info("[CA_TOPIC_SUM] Topic %d queued for summarize (turns %s, switch at turn %d)",
                                old_topic_id, old_turns, turn)
            # ── 跨会话 recall：仅当旧话题被判定为 FAR（强断开）时才注入 ──
            # v7 (决策 38/41): 与首轮 recall 对齐，走提问云形心 4B 拣选（pick_injection_realities），
            # 替代 v6.5 余弦 top-N（query_themes_by_semantics）。
            # BUG-08 止血 (v7.1): REALITY_INJECT_ENABLED=0 时短路，FAR 切换不再阻塞用户消息路径。
            if (old_topic_id is not None and Config.TOPIC_SUMMARIZE_ENABLED
                    and Config.REALITY_INJECT_ENABLED):
                try:
                    tg = plugin._topic_mgr.get_topic_grades()
                    if tg.get(old_topic_id) == TopicGrade.FAR:
                        from ca.inject import pick_injection_realities
                        wiki_entries = pick_injection_realities(
                            user_message, q_emb, Config.HERMES_PROFILE,
                            limit=Config.TOPIC_SUMMARY_RECALL_LIMIT,
                            exclude_session_id=session_id,
                        )
                        if wiki_entries:
                            recall_str = _format_wiki_carryover(wiki_entries)
                            entries_detail = ", ".join(
                                f"id={e.get('reality_id')}({e.get('name','')[:40]})"
                                for e in wiki_entries
                            )
                            # v6.5.3: 保存候选 theme，供下一个话题块 summarize 判断归属
                            plugin._candidate_themes = wiki_entries
                            logger.info("[CA_WIKI] Injected %d chars of wiki recall (FAR): %s",
                                        len(recall_str), entries_detail)
                except Exception as exc:
                    logger.warning("[CA_WIKI] FAR wiki recall injection failed: %s", exc)
        elif plugin._topic_mgr._current_topic_id is not None:
            # 首次话题：记录 current_topic_id
            if plugin._last_topic_id is None:
                plugin._last_topic_id = plugin._topic_mgr._current_topic_id

        # 【日志】话题定级后 dump
        try:
            tg = plugin._topic_mgr.get_topic_grades()
            t1g = plugin._topic_mgr.get_turn_grade(1)
            td = getattr(plugin._topic_mgr, "_topic_data", {})
            logger.info("[CA_v6_grade] grades=%s turn1_grade=%s topic_data_has_centroid=%s",
                       {str(k): str(v) for k, v in tg.items()}, t1g,
                       {str(k): v.get("centroid") is not None for k, v in td.items()})
            _dump = {
                "turn": turn,
                "topic_grades": {str(k): str(v) for k, v in tg.items()},
                "turn_1_grade": str(t1g),
                "topic_data": {str(k): {"turns": v.get("turns",[]), "has_centroid": v.get("centroid") is not None, "max_intra": v.get("max_intra")}
                              for k, v in td.items()},
                "topic_mgr_ok": plugin._topic_mgr is not None,
                "engine_ok": engine is not None,
            }
            with open("/tmp/ca_topic_grades.jsonl", "a") as _f:
                _f.write(json.dumps(_dump, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[CA_v6] topic grades dump failed: %s", exc)

    return recall_str


def _stream_request_id(kwargs: Dict[str, Any]) -> str:
    """on_stream_* payload 无 api_request_id：按 Hermes 格式重建
    api_request_id = f"{turn_id}:api:{api_call_count}"（conversation_loop.py:2625）。"""
    turn_id = kwargs.get("turn_id", "")
    iteration = kwargs.get("iteration", kwargs.get("api_call_count", 0))
    return f"{turn_id}:api:{iteration}"


def _get_plugin(session_id: str) -> Optional["CAContextAssemblerPlugin"]:
    with _engines_lock:
        return _engines.get(session_id)


def _on_post_api_request_v5(**kwargs: Any) -> None:
    """v7: 每 API 调用拆块写入 + llm_calls + decision 思考卡（bg 跳过）。"""
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    if getattr(plugin, '_bg_turn', False):
        return
    # Engine refresh (CR-009)
    if not _refresh_engine(session_id, plugin):
        return
    # stream pending 合并（on_stream_end 可能尚未/已经到达，UPSERT 保证不丢）
    request_id = kwargs.get("api_request_id", "")
    stream_pending: Dict[str, Any] = {}
    with plugin._stream_lock:
        stream_pending = dict(plugin._stream_pending.get(session_id, {}).get(request_id, {}))
        plugin._stream_pending.get(session_id, {}).pop(request_id, None)
    try:
        plugin._engine._on_api_response_v7(
            api_request_id=request_id,
            assistant_message=kwargs.get("assistant_message"),
            api_call_count=kwargs.get("api_call_count", 0),
            turn_index=plugin._engine._current_turn,
            finish_reason=kwargs.get("finish_reason", "stop"),
            usage=kwargs.get("usage"),
            provider=kwargs.get("provider", ""),
            model=kwargs.get("model", ""),
            base_url=kwargs.get("base_url", ""),
            api_mode=kwargs.get("api_mode", ""),
            api_duration=kwargs.get("api_duration"),
            message_count=kwargs.get("message_count"),
            stream_pending=stream_pending,
        )
    except Exception as exc:
        logger.warning("[CA_v5] _on_post_api_request failed: %s", exc, exc_info=True)


def _on_pre_api_request_v7(**kwargs: Any) -> None:
    """pre_api_request：记录请求侧元数据（每 retry 一次，latest-wins）。"""
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_pre_api_request_v7(**kwargs)
    except Exception as exc:
        logger.warning("[CA_v7] _on_pre_api_request failed: %s", exc, exc_info=True)


def _on_api_request_error_v7(**kwargs: Any) -> None:
    """api_request_error：失败尝试写 failed llm_calls（cold path，fail-open）。"""
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    try:
        plugin._engine._on_api_request_error_v7(**kwargs)
    except Exception as exc:
        logger.warning("[CA_v7] _on_api_request_error failed: %s", exc, exc_info=True)


def _on_stream_start_v7(**kwargs: Any) -> None:
    """on_stream_start：重置该 request_id 的流式计数 pending（异步 worker 线程）。"""
    if not Config.CA_STREAM_OBSERVE_ENABLED:
        return
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin:
        return
    request_id = _stream_request_id(kwargs)
    with plugin._stream_lock:
        plugin._stream_pending.setdefault(session_id, {})[request_id] = {
            "reasoning_chars": 0, "text_chars": 0, "chunk_count": 0,
            "provider": kwargs.get("provider"), "model": kwargs.get("model"),
        }


def _on_stream_delta_v7(**kwargs: Any) -> None:
    """on_stream_delta：只累计内存 pending（不做逐 token 落盘；fail-open）。"""
    if not Config.CA_STREAM_OBSERVE_ENABLED:
        return
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin:
        return
    request_id = _stream_request_id(kwargs)
    kind = kwargs.get("kind", "")
    delta = kwargs.get("delta", "")
    if not isinstance(delta, str):
        return
    with plugin._stream_lock:
        bucket = plugin._stream_pending.setdefault(session_id, {}).setdefault(
            request_id, {"reasoning_chars": 0, "text_chars": 0, "chunk_count": 0})
        bucket["chunk_count"] = int(bucket.get("chunk_count") or 0) + 1
        if kind == "reasoning":
            bucket["reasoning_chars"] = int(bucket.get("reasoning_chars") or 0) + len(delta)
        elif kind == "text":
            bucket["text_chars"] = int(bucket.get("text_chars") or 0) + len(delta)


def _on_stream_end_v7(**kwargs: Any) -> None:
    """on_stream_end：把流式计数补丁一次写入 llm_calls（UPSERT，乱序安全）。"""
    if not Config.CA_STREAM_OBSERVE_ENABLED:
        return
    session_id = kwargs.get("session_id", "")
    plugin = _get_plugin(session_id)
    if not plugin or plugin._engine_errored:
        return
    request_id = _stream_request_id(kwargs)
    with plugin._stream_lock:
        bucket = plugin._stream_pending.setdefault(session_id, {}).setdefault(
            request_id, {"reasoning_chars": 0, "text_chars": 0, "chunk_count": 0})
        patch_values = dict(bucket)
    try:
        engine = plugin._engine
        if engine is None or getattr(engine, "store", None) is None:
            return
        from ca.store import patch_llm_call_stream_v1
        patch_llm_call_stream_v1(
            engine.store, session_id, request_id,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            reasoning_chars=int(patch_values.get("reasoning_chars") or 0),
            text_chars=int(patch_values.get("text_chars") or 0),
            chunk_count=int(patch_values.get("chunk_count") or 0),
            finished=kwargs.get("finished"),
            error=kwargs.get("error"),
        )
    except Exception as exc:
        logger.warning("[CA_v7] _on_stream_end patch failed: %s", exc)


def _on_post_tool_call_v5(**kwargs: Any) -> None:
    """v6: 回填 tool 行 + per-tool Fct（bg 跳过）。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    if getattr(plugin, '_bg_turn', False):
        return
    # Engine refresh (CR-009)
    if not _refresh_engine(session_id, plugin):
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
    """v6: 写 final assistant → F-stage（bg 跳过）。"""
    session_id = kwargs.get("session_id", "")
    with _engines_lock:
        plugin = _engines.get(session_id)
    if not plugin or plugin._engine_errored:
        return
    if getattr(plugin, '_bg_turn', False):
        plugin._bg_turn = False  # 清除标记
        return
    # Engine refresh (CR-009)
    if not _refresh_engine(session_id, plugin):
        return
    try:
        plugin.post_llm_call_v5(**kwargs)
    except Exception as exc:
        logger.warning("[CA_v5] _on_post_llm_call failed: %s", exc, exc_info=True)




def _on_pre_tool_call_v5(**kwargs: Any) -> None:
    """v5: 无操作（tool 占位行已在 post_api_request 写入）。"""
    pass


# ═══════════════════════════════════════════════════════════
# Wiki 块格式化（v6.5 重构：当前详细状态 + 互信息量优先级裁剪）
# ═══════════════════════════════════════════════════════════

# 注入优先级（读者 = 云端大模型，互信息量降序；数字越小越优先保留）
#   P0 title（永不裁剪）
#   P1 overview（连贯叙述 = 最快建立主题认知的载体）
#   P2 OODA 决策与方案（核心进展）
#   P3 OODA 现象与问题（当前痛点）
#   P4 OODA 背景与约束（为什么这么做）
#   P5 OODA 后续行动（下一步计划）
#   P6 key_facts（与决策/现象重叠，信息增量低）
#   P7 open_items（与后续行动重叠，信息增量最低）
_THEME_OODA_ORDER = [
    ("决策与方案", 2),
    ("现象与问题", 3),
    ("背景与约束", 4),
    ("后续行动", 5),
]

# 同级裁剪序列：先缩条数（全部→5→3→1），再缩长度（200→120→80），最后整段移除
_THEME_COUNT_LEVELS = [None, 5, 3, 1]      # None = 全部
_THEME_LEN_LEVELS = [200, 120, 80]


def _format_theme_section(label: str, items: list[str],
                          count_level: int, len_level: int) -> str:
    """渲染单个分段：[label] + bullet 列表（按降级状态裁剪）。"""
    max_count = _THEME_COUNT_LEVELS[count_level]
    max_len = _THEME_LEN_LEVELS[len_level]
    sel = items[:max_count] if max_count else items
    body = "\n".join(f"- {it[:max_len]}" for it in sel)
    return f"[{label}]\n{body}"


def _format_single_theme(theme: dict, max_chars: int) -> str:
    """单 theme 渲染：按优先级裁剪至预算内。P0 title 永不裁剪。"""
    title = (theme.get("title") or "").strip() or "Topic"
    sections: list[tuple[int, str, list[str]]] = []

    ov = (theme.get("overview") or "").strip()
    if ov:
        sections.append((1, "当前状态", [ov]))
    ooda = theme.get("ooda") or {}
    if isinstance(ooda, dict):
        for group, prio in _THEME_OODA_ORDER:
            items = [str(i) for i in ooda.get(group) or [] if str(i).strip()]
            if items:
                sections.append((prio, group, items))
    kf = [str(i) for i in theme.get("key_facts") or [] if str(i).strip()]
    if kf:
        sections.append((6, "结论", kf))
    oi = [str(i) for i in theme.get("open_items") or [] if str(i).strip()]
    if oi:
        sections.append((7, "待办", oi))

    if not sections:
        return ""

    # 降级状态：{prio: {"count": level(0..3), "len": level(0..2)}}
    # count: 0=全部 1=5 2=3 3=1；len: 0=200 1=120 2=80
    state = {prio: {"count": 0, "len": 0} for prio, _, _ in sections}

    def render() -> str:
        parts = [f"## {title}"]
        for prio, label, items in sorted(sections, key=lambda s: s[0]):
            st = state[prio]
            if not st.get("removed"):
                parts.append(_format_theme_section(
                    label, items, st["count"], st["len"]))
        return "\n\n".join(parts)

    def degrade(st: dict) -> bool:
        """降一级：先缩条数（→5→3→1），再缩长度（200→120→80），最后移除。"""
        if not st.get("removed"):
            if st["count"] < 3:
                st["count"] += 1
                return True
            if st["len"] < 2:
                st["len"] += 1
                return True
            st["removed"] = True
            return True
        return False

    # 贪心裁剪：超预算 → 对最低优先级（prio 最大）段逐级降级
    while True:
        out = render()
        if len(out) <= max_chars:
            return out
        degraded = False
        for prio, _, _ in sorted(sections, key=lambda s: -s[0]):
            if degrade(state[prio]):
                degraded = True
                break
        if not degraded:
            # 所有段已移除，仅剩 title
            return f"## {title}"


def _format_single_reality(reality: dict, max_chars: int) -> str:
    """单 reality 渲染（决策 41）：name/hdl/current_status/timeline 摘要，预算内裁剪。"""
    name = (reality.get("name") or reality.get("hdl") or "").strip() or "Reality"
    sections: list[tuple[int, str, list[str]]] = []

    hdl = (reality.get("hdl") or "").strip()
    if hdl and hdl != name:
        sections.append((0, "状态锚点", [hdl]))
    cs = reality.get("current_status") or {}
    if isinstance(cs, dict):
        if cs.get("goals"):
            sections.append((1, "进行中目标",
                             [str(i) for i in cs["goals"] if str(i).strip()][:5]))
        if cs.get("current_state"):
            sections.append((2, "当前状态",
                             [str(i) for i in cs["current_state"] if str(i).strip()][:5]))
        if cs.get("key_facts"):
            sections.append((3, "持久事实",
                             [str(i) for i in cs["key_facts"] if str(i).strip()][:4]))
        if cs.get("context"):
            sections.append((4, "相关资源",
                             [str(i) for i in cs["context"] if str(i).strip()][:3]))
    tl = reality.get("timeline") or []
    if isinstance(tl, list):
        ovs = [str(e.get("overview", "")) for e in tl[-2:]
               if isinstance(e, dict) and str(e.get("overview", "")).strip()]
        if ovs:
            sections.append((5, "演进摘要", ovs))

    if not sections:
        return f"## {name}"

    state = {prio: {"count": 0, "len": 0} for prio, _, _ in sections}

    def render() -> str:
        parts = [f"## {name}"]
        for prio, label, items in sorted(sections, key=lambda s: s[0]):
            st = state[prio]
            if not st.get("removed"):
                parts.append(_format_theme_section(
                    label, items, st["count"], st["len"]))
        return "\n\n".join(parts)

    def degrade(st: dict) -> bool:
        if not st.get("removed"):
            if st["count"] < 3:
                st["count"] += 1
                return True
            if st["len"] < 2:
                st["len"] += 1
                return True
            st["removed"] = True
            return True
        return False

    while True:
        out = render()
        if len(out) <= max_chars:
            return out
        degraded = False
        for prio, _, _ in sorted(sections, key=lambda s: -s[0]):
            if degrade(state[prio]):
                degraded = True
                break
        if not degraded:
            return f"## {name}"


def _format_wiki_carryover(
    themes: list[dict],
    per_theme_max_chars: int = 2000,
) -> str:
    """将 reality/theme 列表格式化为 <wiki_carryover> 注入块（当前详细状态）。

    每条目独立预算（per_theme_max_chars，默认 2000 字符），
    超预算按互信息量优先级（P7→P1）逐级裁剪，title 永不裁剪。
    reality dict（含 reality_id 键）→ _format_single_reality；theme → _format_single_theme。
    一次性注入，不进对话记录表。
    """
    blocks: list[str] = []
    for t in themes:
        if isinstance(t, dict) and "reality_id" in t:
            block = _format_single_reality(t, per_theme_max_chars)
        else:
            block = _format_single_theme(t, per_theme_max_chars)
        if block:
            blocks.append(block)

    if not blocks:
        return ""
    return (
        "<wiki_carryover>\n"
        + "\n\n".join(blocks)
        + "\n</wiki_carryover>"
    )
