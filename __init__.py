"""plugins/ca_assembler/__init__.py — Hermes 插件适配 (v5.10)

设计决策: P-001 (Plugin 层职责分离)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-plugin-适配-amp-断路器
  - 注册 8 hooks (5 生命周期 + 3 工具轮 hook)
  - pre_llm_call_v5: E-stage 写入 + topic 检测 + A-stage 替换
  - post_llm_call_v5: E-stage final 写入 + F-stage 触发

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
from ca.grade import Grade, TopicGrade
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
    """注册 CA 插件 v5.0 hooks 和 ContextEngine ABC。"""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end",   _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_llm_call",     _on_pre_llm_call_v5)
    ctx.register_hook("post_llm_call",    _on_post_llm_call_v5)
    ctx.register_hook("post_api_request", _on_post_api_request_v5)
    ctx.register_hook("pre_tool_call",    _on_pre_tool_call_v5)
    ctx.register_hook("post_tool_call",   _on_post_tool_call_v5)
    # CE 壳注册 — 允许 context.engine: ca_assembler 配置选用
    ctx.register_context_engine("ca_assembler", _ce_engine)


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

    should_compress 返回 True（每轮触发 compress_context 做 FAR 行删除），
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
        """始终返回 True — 每轮都走 CE 管线做 FAR 行删除。

        由 should_compress 触发 compress_context() → compress() 路径，
        compress() 原地删行 + 设 abort 标志，阻止 session rotation。

        三个约束：
          1. should_compress 无条件 True（不论 FAR 有无、对话长短）
          2. compress 永不触发 session ID 变更（archive/rotation 全部跳过）
          3. post_llm_call 从 _full_backup 完整恢复原始数据 → state.db
        """
        # Pre-set abort 标志，使 compress_context 跳过 archive/rotation
        self._last_compress_aborted = True
        self._last_summary_error = "CA: in-place FAR deletion, no rotation"
        return True

    def compress(
        self,
        messages: list,
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> list:
        """CE 管线入口：备份 + 交 A-stage 统一处理删行与替换。

        设置 _full_backup 后委托 plugin.pre_llm_call_v5 处理，
        A-stage 负责删除 FAR thought/tool 行对并替换剩余行 content。
        配合 should_compress 预设的 abort 标志使 compress_context
        跳过 archive_and_compact 和 session rotation。
        """
        plugin = self._get_plugin()
        if not plugin or plugin._engine_errored or not plugin._engine:
            return messages

        # 守卫：如果消息未增长跳过
        last_len = getattr(self, '_last_compress_msg_len', 0)
        if not force and len(messages) <= last_len:
            return messages

        # 全量备份，供 post_llm_call 恢复 content → state.db
        plugin._full_backup = [{**m} for m in messages]

        # 交给 A-stage 统一处理（传入原始 messages 引用，可做删行+替换）
        plugin.pre_llm_call_v5(conversation_history=messages)

        # _full_backup 保留不动：post_llm_call_v5 直接用原表替换压缩后的 conv_hist，
        # 天然解决索引错位（替代逐行 idx 拷贝 + 回退 _saved_history_snapshot 的方案）。

        self._last_compress_msg_len = len(messages)
        self.compression_count += 1
        return messages

    # -- ContextEngine: lifecycle -----------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """记录当前 session_id。插件实例由 _on_session_start hook 创建。"""
        self._session_id = session_id

    def on_session_end(self, session_id: str = "", messages: list = None) -> None:
        """透传到插件实例（清理 store 资源）。"""
        plugin = self._get_plugin()
        if plugin:
            plugin.on_session_end()

    def on_session_reset(self) -> None:
        """重置 CE 状态（不触及 plugin 实例）。"""
        self._session_id = ""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

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
        self._full_backup: Optional[List[Dict]] = None
        self._topic_mgr: Optional[TopicGradeManager] = None
        self._pending_ov_submit: Optional[Dict[str, Any]] = None
        self._last_topic_id: Optional[int] = None
        # A-stage 增量缓存
        self._A_stable_cache: Optional[List[Dict]] = None
        self._A_cache_turns: int = 0
        self._A_cache_is_stale: bool = False

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
        )
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
        # 如果有未提交的 OV 话题，尝试提交
        if self._pending_ov_submit is not None and Config.OV_ENABLED:
            ov_data = self._pending_ov_submit
            self._pending_ov_submit = None
            try:
                self._fire_ov_submit(self._session_id, ov_data)
            except Exception:
                pass
        self._pending_ov_submit = None
        self._last_topic_id = None
        self._saved_history_snapshot = None
        self._full_backup = None
        self._A_stable_cache = None
        self._A_cache_turns = 0
        self._A_cache_is_stale = False
        if self._topic_mgr:
            self._topic_mgr.reset()
        if self._engine:
            self._engine.reset()
        self._engine_errored = False
        _record_success()

    # ── Hooks ──






    def _delete_far_thought_tool_rows(self, messages: list) -> None:
        """从原始消息中删除 FAR 级的 thought + tool 行对，保留 fin 行。

        仅由 CE compress() 路径调用（_full_backup 已设置），此时 messages
        是原始列表引用，可做原地 delete 操作（messages[:]=filtered）。
        fin 行（assistant, finish_reason='stop'）保留，A-stage 后续替换为 Hdl[:150]。
        user 行保留到 content 替换阶段处理。
        """
        # tail 保护区：最后 2 个 user
        tail_boundary = 0
        _user_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        drop_indices: set = set()
        current_turn = -1
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                current_turn += 1
            if i >= tail_boundary:
                continue
            # fin 行保留（A-stage 替换为 Hdl[:150]）
            if role == "assistant" and msg.get("finish_reason") == "stop":
                continue
            if role not in ("assistant", "tool"):
                continue
            # FAR thought/tool → 删除
            if self._topic_mgr:
                grade = self._topic_mgr.get_turn_grade(current_turn + 1)
                if grade == TopicGrade.FAR:
                    drop_indices.add(i)

        if not drop_indices:
            return
        messages[:] = [m for i, m in enumerate(messages) if i not in drop_indices]

    # ═════════════════════════════════════════════════════
    # v5.0 — A-stage 替换 + E-stage final 写入
    # ═════════════════════════════════════════════════════


    # ── 薄壳：委托到 engine（兼容旧测试直接调用） ──

    def _simple_mutation_mode_v5(self, conversation_history: list, topic_mgr: Any = None) -> Optional[str]:
        """委托到 engine 的 AStageMixin._simple_mutation_mode_v5。"""
        if not self._engine:
            return None
        return self._engine._simple_mutation_mode_v5(topic_mgr or self._topic_mgr, conversation_history)

    _full_mutation = _simple_mutation_mode_v5

    def _incremental_mutation(self, conversation_history: list, topic_mgr: Any = None) -> Optional[str]:
        """委托到 engine 的 AStageMixin._incremental_mutation。"""
        if not self._engine:
            return None
        return self._engine._incremental_mutation(topic_mgr or self._topic_mgr, conversation_history)

    @staticmethod
    def _estimate_conv_tokens(conv_hist: list) -> int:
        from ca.a_stage import AStageMixin
        return AStageMixin._estimate_conv_tokens(conv_hist)

    def pre_llm_call_v5(self, **kwargs: Any) -> Optional[str]:
        """v5 A-stage: 话题检测 → topic-aware 替换。"""
        conversation_history = kwargs.get("conversation_history", [])
        if not isinstance(conversation_history, list) or not conversation_history:
            return None

        # CE 路径：compress() 设置了 _full_backup → 先删 FAR thought/tool 行
        if self._full_backup is not None:
            self._delete_far_thought_tool_rows(conversation_history)

        # 话题检测 + 切换定级
        turn = self._engine._current_turn if self._engine else 0
        user_msg = kwargs.get("user_message", "")
        total_tokens = self._estimate_conv_tokens(conversation_history)
        switched = False
        if self._topic_mgr and self._engine and turn > 0 and user_msg:
            from ca.store import get_turn_ca_rows
            ca_rows = get_turn_ca_rows(self._engine.store, self._session_id, turn)
            switched = self._topic_mgr.detect(turn, ca_rows, user_msg, total_tokens=total_tokens)
            if switched:
                q_emb = self._engine.embed_client.embed(user_msg)
                self._topic_mgr.grade_on_switch(q_emb, user_msg)
                # 打包前一个话题数据 → OV 提交
                old_topic_id = self._last_topic_id
                self._last_topic_id = self._topic_mgr._current_topic_id
                if old_topic_id is not None and Config.OV_ENABLED and self._engine:
                    old_turns = sorted([
                        t for t, tid in self._topic_mgr._turn_to_topic.items()
                        if tid == old_topic_id
                    ])
                    if old_turns:
                        self._pending_ov_submit = {
                            "topic_id": old_topic_id,
                            "turns": old_turns,
                            "switch_turn": turn,
                        }
                        logger.info("[CA_OV] Topic %d packaged for submit (turns %s, switch at turn %d)",
                                    old_topic_id, old_turns, turn)
            elif self._topic_mgr and self._topic_mgr._current_topic_id is not None:
                # 首次话题：记录 current_topic_id，不触发 OV 提交
                if self._last_topic_id is None:
                    self._last_topic_id = self._topic_mgr._current_topic_id
        # 【日志 A】话题定级后 dump
                try:
                    tg = self._topic_mgr.get_topic_grades()
                    t1g = self._topic_mgr.get_turn_grade(1)
                    td = getattr(self._topic_mgr, "_topic_data", {})
                    logger.info("[CA_v5_grade] grades=%s turn1_grade=%s topic_data_has_centroid=%s",
                               {str(k): str(v) for k, v in tg.items()}, t1g,
                               {str(k): v.get("centroid") is not None for k, v in td.items()})
                    _dump = {
                        "turn": turn,
                        "topic_grades": {str(k): str(v) for k, v in tg.items()},
                        "turn_1_grade": str(t1g),
                        "topic_data": {str(k): {"turns": v.get("turns",[]), "has_centroid": v.get("centroid") is not None, "max_intra": v.get("max_intra")}
                                      for k, v in td.items()},
                        "topic_mgr_ok": self._topic_mgr is not None,
                        "engine_ok": self._engine is not None,
                    }
                    with open("/tmp/ca_topic_grades.jsonl", "a") as _f:
                        _f.write(json.dumps(_dump, ensure_ascii=False) + "\n")
                except Exception as exc:
                    logger.warning("[CA_v5] topic grades dump failed: %s", exc)

        # ── 缓存调度 ──
        if switched:
            self._engine._A_stable_cache = None
            self._engine._A_cache_is_stale = False

        if self._engine._A_stable_cache is None:
            return self._engine._full_mutation(self._topic_mgr, conversation_history)

        if self._engine._A_cache_is_stale:
            logger.debug("[CA_v5] cache stale (Fct pending), full mutation fallback")
            self._engine._A_stable_cache = None
            self._engine._A_cache_is_stale = False
            return self._engine._full_mutation(self._topic_mgr, conversation_history)

        return self._engine._incremental_mutation(self._topic_mgr, conversation_history)

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
            role='assistant', elm_text=assistant_response,
            finish_reason='stop',
            written_at=time.time(),
        )
        logger.info("[CA_v5] post_llm_call: wrote asst_fin turn=%d seq=%d", turn, seq)

        _backup = getattr(self, "_full_backup", None)
        if _backup is not None:
            # _full_backup 是 compress() 保存的完整快照（含 FAR 行），
            # 直接替换 conv_hist 整表，避免索引错位 + 完整保留原始数据供 archive
            conversation_history[:] = _backup
            self._full_backup = None
            logger.info("[CA_v5] post_llm_call: replaced conv_hist with full_backup (%d rows)",
                        len(conversation_history))
        else:
            _snapshot = getattr(self._engine, "_saved_history_snapshot", None)
            if _snapshot is not None:
                for i, orig_dict in enumerate(_snapshot):
                    if i >= len(conversation_history):
                        break
                    ch = conversation_history[i]
                    # 还原所有被 mutation 修改的字段
                    for _key in ("content", "reasoning_content", "tool_calls"):
                        if _key in orig_dict:
                            ch[_key] = orig_dict[_key]
                        else:
                            ch.pop(_key, None)
                self._engine._saved_history_snapshot = None

        engine.process_turn_f_stage(turn, fin_seq=seq)
        logger.info("[CA_v5] post_llm_call: process_turn_f_stage called for turn %d fin_seq %d", turn, seq)

        # OV 话题摘要提交（fire-and-forget）
        if self._pending_ov_submit is not None and Config.OV_ENABLED:
            ov_data = self._pending_ov_submit
            self._pending_ov_submit = None
            session_id = self._session_id
            threading.Thread(
                target=self._fire_ov_submit,
                args=(session_id, ov_data),
                daemon=True,
                name="CA-OVSubmit",
            ).start()
            logger.info("[CA_OV] Thread CA-OVSubmit started for topic %d", ov_data.get("topic_id"))

    def _fire_ov_submit(self, session_id: str, topic_data: Dict[str, Any]) -> None:
        """将话题摘要打包为 Markdown 提交到 OpenViking。

        Fire-and-forget，在 daemon 线程中执行。等待 F-stage 完成后再读 DB
        收集各轮的 Fct 摘要，通过 OV temp_upload_signed → add_resource 流程写入。
        """
        topic_id = topic_data["topic_id"]
        turns = topic_data["turns"]
        switch_turn = topic_data["switch_turn"]
        logger.info("[CA_OV] _fire_ov_submit: topic %d turns=%s switch=%d",
                    topic_id, turns, switch_turn)

        if not self._engine:
            logger.warning("[CA_OV] No engine for topic %d, skipping", topic_id)
            return

        # 等待 F-stage 完成（等待当前 turn 的 Fct 落盘）
        engine = self._engine
        try:
            engine.wait_for_pending(timeout=Config.LLM_TIMEOUT + 30)
        except Exception:
            pass  # 超时不影响——用已有 Fct 数据

        # 组装 Markdown
        lines: List[str] = []
        lines.append(f"# Topic {topic_id}")
        lines.append("")
        lines.append(f"话题从 Turn {turns[0]} 延续至 Turn {turns[-1]}，在 Turn {switch_turn} 切换。")
        lines.append("")

        store = engine.store
        if store:
            for t in sorted(turns):
                try:
                    from ca.store import get_turn_ca_rows
                    rows = get_turn_ca_rows(store, session_id, t)
                except Exception:
                    rows = []
                user_text = ""
                fct_text = ""
                for row in rows:
                    seq, role, _, _, elm_text, Fct, _ = row
                    if role == "user":
                        user_text = elm_text or ""
                    if role == "assistant" and Fct:
                        fct_text = Fct

                lines.append(f"### Turn {t}")
                lines.append(f"**User:** {(user_text or '(empty)')[:200]}")
                if fct_text:
                    try:
                        fct = json.loads(fct_text)
                        core = fct.get("core_change", "")
                        changes = fct.get("changes", [])
                        if changes:
                            summary = "；".join(
                                c.get("core_change", "") for c in changes
                            )
                        else:
                            summary = core
                        lines.append(f"**Summary:** {summary or '(empty)'}")
                    except (json.JSONDecodeError, TypeError):
                        lines.append(f"**Summary:** {fct_text[:200]}")
                else:
                    lines.append("**Summary:** (pending)")
                lines.append("")

        body = "\n".join(lines)

        # 写入临时文件 → OV 上传
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        )
        tmp.write(body)
        tmp_path = tmp.name
        tmp.close()

        try:
            ts = int(time.time())
            filename = f"topic_{topic_id}_{ts}.md"
            target_uri = (
                Config.OV_TOPIC_DIR_PREFIX
                .replace("{ov_user}", Config.OV_USER)
                .replace("{ov_session}", session_id)
            )
            # to 为目标目录，文件名为 temp 文件名。资源最终路径为 target_uri/filename

            # 读取文件内容准备上传
            import uuid
            with open(tmp_path, "rb") as f:
                file_data = f.read()

            import http.client
            endpoint_host = Config.OV_ENDPOINT.rstrip("/").replace("http://", "").replace("https://", "")
            endpoint_port = 1933
            if ":" in endpoint_host:
                parts = endpoint_host.split(":")
                endpoint_host = parts[0]
                endpoint_port = int(parts[1])

            # Step 1: temp_upload (multipart)
            boundary = uuid.uuid4().hex
            mp_header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: text/markdown; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
            mp_footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
            mp_body = mp_header + file_data + mp_footer

            conn = http.client.HTTPConnection(endpoint_host, endpoint_port, timeout=30)
            conn.request(
                "POST", "/api/v1/resources/temp_upload",
                body=mp_body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            resp = conn.getresponse()
            result = json.loads(resp.read())
            conn.close()

            if result.get("status") != "ok":
                logger.error("[CA_OV] temp_upload failed for topic %d: %s",
                             topic_id, result.get("error", result))
                return

            temp_id = result["result"]["temp_file_id"]

            # Step 2: add_resource
            import urllib.request
            add_req = urllib.request.Request(
                f"{Config.OV_ENDPOINT.rstrip('/')}/api/v1/resources",
                data=json.dumps({"temp_file_id": temp_id, "to": target_uri}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(add_req, timeout=60) as resp2:
                result2 = json.loads(resp2.read())

            if result2.get("status") == "ok":
                resource_path = target_uri.rstrip("/") + f"/{filename}"
                logger.info("[CA_OV] Topic %d submitted → %s", topic_id, resource_path)
            else:
                logger.error("[CA_OV] add_resource failed for topic %d: %s",
                             topic_id, result2.get("error", result2))
        except Exception as exc:
            logger.error("[CA_OV] Submit failed for topic %d: %s",
                         topic_id, exc, exc_info=True)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
# v5.0 Hook 分发函数（模块级，0 缩进）
# ═══════════════════════════════════════════════════════════════

def _on_pre_llm_call_v5(**kwargs: Any) -> Optional[str]:
    """v5: 写 seq 0 (user Elm) → A-stage 替换。"""
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

    if bg:
        # 后台轮：user 行 Fct=Elm（Elm 拷贝），Hdl=Elm[:100]
        write_turn_v5(
            engine.store, session_id, turn, seq=0,
            role='user', elm_text=user_message,
            fct_text=user_message,
            hdl_text=user_message[:100],
            biz_category='bg_review',
            written_at=time.time(),
        )
        logger.info("[CA_v5] pre_llm_call: wrote seq 0 (bg) turn=%d", turn)
        return None

    write_turn_v5(
        engine.store, session_id, turn, seq=0,
        role='user', elm_text=user_message,
        fct_text=user_message,
        hdl_text=user_message[:100],
        biz_category=None,
        written_at=time.time(),
    )
    logger.info("[CA_v5] pre_llm_call: wrote seq 0 turn=%d", turn)

    return plugin.pre_llm_call_v5(**kwargs)


def _on_post_api_request_v5(**kwargs: Any) -> None:
    """v5: 写 thought 行 + tool 占位行。"""
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
    """v5: 回填 tool 行 + per-tool Fct。"""
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
