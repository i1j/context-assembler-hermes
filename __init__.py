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
from .topic_manager import TopicGradeManager

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
        self._topic_mgr: Optional[TopicGradeManager] = None
        # A-stage 增量缓存
        self._A_stable_cache: Optional[List[Dict]] = None
        self._A_cache_turns: int = 0
        self._A_cache_is_stale: bool = False

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
        self._saved_history = None
        self._saved_history_snapshot = None
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






    # ═════════════════════════════════════════════════════
    # v5.0 — A-stage 替换 + E-stage final 写入
    # ═════════════════════════════════════════════════════

    def _simple_mutation_mode_v5(self, conversation_history: list) -> Optional[str]:
        """v5: 轮内按角色类型匹配注入 Fct。

        策略（C 方案）：
          - 每个 turn 由 user 在 conv_hist 中的位置锚定。
          - 拉该 turn 在 CA 的所有行，按角色分队列：
            thought（assistant, 不含 fin）和 tool。
          - conv_hist 的 thought/tool 行依次从对应队列中取。
          - 队列用尽则保留原始 Elm，队列多余自然孤行。
          - fin 行（assistant, finish_reason='stop'）不进入任何队列。
        """
        if not conversation_history:
            return None
        self._saved_history_snapshot = [{**m} for m in conversation_history]
        self._saved_history = None
        store = self._engine.store
        sid = self._session_id

        from ca.store import get_turn_ca_rows

        # 尾巴保护：最后 2 个 user turn 不处理
        tail_boundary = 0
        _user_count = 0
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        # Phase 1: 按 turn 收集 conv_hist 中需匹配的行
        # turn 编号：0-indexed，与 DB 一致（_on_pre_llm_call_v5 写 turn 时用 len()）
        # 注意：历史代码用 0 起始遇 user 先 +1，导致所有 turn 偏移 1。
        # 当前代码修正为 -1 起始遇 user +1 后即为 0-indexed。
        turn_rows: dict = {}
        current_turn = -1
        for i, msg in enumerate(conversation_history):
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                current_turn += 1
                continue
            if i >= tail_boundary:
                continue

            if role == "assistant" and not msg.get("tool_calls"):
                row_type = "fin"
            elif role == "assistant":
                row_type = "thought"
            elif role == "tool":
                row_type = "tool"
            else:
                continue

            turn_rows.setdefault(current_turn, []).append((i, row_type))

        # Phase 2: 逐 turn 做角色队列匹配
        replaced = 0
        skipped = 0

        for turn_num, rows in turn_rows.items():
            if turn_num < 0:  # turn 1（turn_num=0）现在可处理了
                continue

            ca_rows = get_turn_ca_rows(store, sid, turn_num + 1)
            if not ca_rows:
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            # 按角色分队列：thought（排除 fin）和 tool
            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role, finish_reason, tc_json, fct in ca_rows:
                if role == "user":
                    continue
                if role == "assistant" and finish_reason == "stop":
                    continue  # fin，不放任何队列
                if role == "assistant":
                    ca_thoughts.append(fct or "")
                elif role == "tool":
                    ca_tools.append(fct or "")

            # 逐个匹配
            ti, tj = 0, 0
            grade = self._topic_mgr.get_turn_grade(turn_num + 1) if self._topic_mgr else "L1"
            for conv_idx, row_type in rows:
                if row_type == "fin":
                    continue

                if row_type == "thought":
                    if ti < len(ca_thoughts):
                        if grade == "L2":
                            # L2: 保持 Elm（保留原文），只清理冗余字段
                            conversation_history[conv_idx].pop("reasoning_content", None)
                            conversation_history[conv_idx].pop("tool_calls", None)
                            skipped += 1
                        elif grade == "L1":
                            conversation_history[conv_idx]["content"] = ca_thoughts[ti]
                            conversation_history[conv_idx].pop("reasoning_content", None)
                            conversation_history[conv_idx].pop("tool_calls", None)
                            replaced += 1
                        else:  # L0
                            conversation_history[conv_idx]["content"] = (ca_thoughts[ti] or "")[:150]
                            conversation_history[conv_idx].pop("reasoning_content", None)
                            conversation_history[conv_idx].pop("tool_calls", None)
                            replaced += 1
                        ti += 1
                    else:
                        skipped += 1
                else:  # tool
                    if tj < len(ca_tools):
                        if grade == "L2":
                            # L2: 保持 Elm，只清理冗余字段
                            conversation_history[conv_idx].pop("reasoning_content", None)
                            conversation_history[conv_idx].pop("tool_calls", None)
                            skipped += 1
                        elif grade == "L1":
                            conversation_history[conv_idx]["content"] = ca_tools[tj]
                            replaced += 1
                        else:  # L0
                            conversation_history[conv_idx]["content"] = (ca_tools[tj] or "")[:150]
                            replaced += 1
                        tj += 1
                    else:
                        skipped += 1

        last_turn = max(turn_rows, default=0)
        logger.info("[CA_v5] simple_mutation: replaced=%d + skipped=%d (tail=%d, turns=%d)",
                    replaced, skipped, tail_boundary, last_turn)

        # Debug dump
        if Config.DEBUG_MODE:
            dump_dir = os.environ.get("CA_DEBUG_DUMP", "")
            if dump_dir:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dump_path = os.path.join(dump_dir, f"ca_mutation_{sid}_{ts}.json")
                try:
                    os.makedirs(dump_dir, exist_ok=True)
                    with open(dump_path, "w", encoding="utf-8") as f:
                        json.dump(conversation_history, f, ensure_ascii=False, indent=2)
                    logger.info("[CA_v5] mutation dump written: %s (%d msgs, %d KB)",
                                dump_path, len(conversation_history),
                                os.path.getsize(dump_path) // 1024)
                except Exception as e:
                    logger.warning("[CA_v5] failed to write mutation dump: %s", e)

        # 写入稳定区缓存（供增量路径复用）
        self._A_stable_cache = [{**m} for m in conversation_history[0:tail_boundary]]
        self._A_cache_turns = sum(1 for m in self._A_stable_cache if m.get("role") == "user")
        self._A_cache_is_stale = False
        logger.debug("[CA_v5] full_mutation: cache written, stable_turns=%d", self._A_cache_turns)

        return None

    # _simple_mutation_mode_v5 别名（安全回退用）
    _full_mutation = _simple_mutation_mode_v5

    def _incremental_mutation(self, conversation_history: list) -> Optional[str]:
        """增量路径：缓存稳定区 → delta 替换 → 尾部保留。

        前提：话题未切换，_A_stable_cache 有效。
        """
        if not conversation_history or not self._engine:
            return None

        # 保存快照（供 post_llm_call 还原 Elm）
        self._saved_history_snapshot = [{**m} for m in conversation_history]
        self._saved_history = None

        store = self._engine.store
        sid = self._session_id
        cache = self._A_stable_cache
        if cache is None:
            return self._full_mutation(conversation_history)

        from ca.store import get_turn_ca_rows

        # ── Step 1: 尾部边界（与全量路径一致） ──
        tail_boundary = 0
        _user_count = 0
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        # ── Step 2: 并行扫描缓存 → 覆盖稳定区 ──
        cache_idx = 0
        need_fallback = False
        for i, msg in enumerate(conversation_history):
            if cache_idx >= len(cache):
                break
            role = msg.get("role", "")
            c_msg = cache[cache_idx]
            if role == c_msg.get("role"):
                msg["content"] = c_msg.get("content", "")
                msg.pop("reasoning_content", None)
                msg.pop("tool_calls", None)
                cache_idx += 1
            elif role == "system":
                continue  # Hermes 新插入的 system，不消耗缓存
            else:
                # 结构不一致 → 回退全量
                need_fallback = True
                break

        if need_fallback:
            logger.info("[CA_v5] cache alignment mismatch, falling back to full mutation")
            self._A_stable_cache = None
            self._A_cache_turns = 0
            return self._full_mutation(conversation_history)

        # ── Step 3: Delta 区（原尾部 → 现稳定，需查表替换） ──
        # turn 编号 0-indexed，与全量路径 Phase 1 保持一致
        turn_rows: dict = {}
        current_turn = self._A_cache_turns - 1  # cache 最后一个 user 的 DB turn，遇 user +1 后即为下一个 delta turn
        for i in range(len(cache), len(conversation_history)):
            msg = conversation_history[i]
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "user":
                current_turn += 1
                continue
            if i >= tail_boundary:
                continue

            if role == "assistant" and not msg.get("tool_calls"):
                row_type = "fin"
            elif role == "assistant":
                row_type = "thought"
            elif role == "tool":
                row_type = "tool"
            else:
                continue
            turn_rows.setdefault(current_turn, []).append((i, row_type))

        # ── Step 4: Delta 替换（含 Fct pending 防护） ──
        replaced = 0
        skipped = 0
        turn_pending = False

        for turn_num, rows in turn_rows.items():
            if turn_num <= 0:
                continue

            ca_rows = get_turn_ca_rows(store, sid, turn_num)
            if not ca_rows:
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            # ★ 整个 turn 是否 Fct pending？
            # 检查 user 行（seq=0）的 fct；若不存在，整个 turn 尚未 Fct 化 → 保留 Elm
            user_fct = next((r[4] for r in ca_rows if r[1] == "user"), None)
            if not user_fct:
                turn_pending = True
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            # 角色队列构建（只取有 Fct 的行）
            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role_, finish_reason, tc_json, fct in ca_rows:
                if role_ == "user":
                    continue
                if role_ == "assistant" and finish_reason == "stop":
                    continue
                if fct is None:
                    turn_pending = True
                    continue
                if role_ == "assistant":
                    ca_thoughts.append(fct)
                elif role_ == "tool":
                    ca_tools.append(fct)

            # 1:1 角色匹配
            ti, tj = 0, 0
            for conv_idx, row_type in rows:
                if row_type == "fin":
                    continue
                if row_type == "thought":
                    if ti < len(ca_thoughts):
                        conversation_history[conv_idx]["content"] = ca_thoughts[ti]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        replaced += 1
                        ti += 1
                    else:
                        skipped += 1
                else:  # tool
                    if tj < len(ca_tools):
                        conversation_history[conv_idx]["content"] = ca_tools[tj]
                        replaced += 1
                        tj += 1
                    else:
                        skipped += 1

        if turn_pending:
            self._A_cache_is_stale = True

        logger.info("[CA_v5] incremental_mutation: replaced=%d + skipped=%d (tail=%d, delta_turns=%d, pending=%s)",
                    replaced, skipped, tail_boundary, len(turn_rows), turn_pending)

        # ── Step 5: 写缓存 ──
        self._A_stable_cache = [{**m} for m in conversation_history[0:tail_boundary]]
        self._A_cache_turns = sum(1 for m in self._A_stable_cache if m.get("role") == "user")
        if not turn_pending:
            self._A_cache_is_stale = False
        logger.debug("[CA_v5] incremental_mutation: cache updated, stable_turns=%d",
                     self._A_cache_turns)

        # Debug dump
        if Config.DEBUG_MODE:
            dump_dir = os.environ.get("CA_DEBUG_DUMP", "")
            if dump_dir:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dump_path = os.path.join(dump_dir, f"ca_incr_mutation_{sid}_{ts}.json")
                try:
                    os.makedirs(dump_dir, exist_ok=True)
                    with open(dump_path, "w", encoding="utf-8") as f:
                        json.dump(conversation_history, f, ensure_ascii=False, indent=2)
                    logger.info("[CA_v5] incr mutation dump written: %s (%d msgs, %d KB)",
                                dump_path, len(conversation_history),
                                os.path.getsize(dump_path) // 1024)
                except Exception as e:
                    logger.warning("[CA_v5] failed to write incr mutation dump: %s", e)

        return None

    def pre_llm_call_v5(self, **kwargs: Any) -> Optional[str]:
        """v5 A-stage: 话题检测 → topic-aware 替换。"""
        conversation_history = kwargs.get("conversation_history", [])
        if not isinstance(conversation_history, list) or not conversation_history:
            return None

        # 话题检测 + 切换定级
        turn = self._engine._current_turn if self._engine else 0
        user_msg = kwargs.get("user_message", "")
        switched = False
        if self._topic_mgr and self._engine and turn > 0 and user_msg:
            from ca.store import get_turn_ca_rows
            ca_rows = get_turn_ca_rows(self._engine.store, self._session_id, turn)
            switched = self._topic_mgr.detect(turn, ca_rows, user_msg)
            if switched:
                q_emb = self._engine.embed_client.embed(user_msg)
                self._topic_mgr.grade_on_switch(q_emb, user_msg)

        # ── 缓存调度 ──
        if switched:
            self._A_stable_cache = None
            self._A_cache_is_stale = False

        if self._A_stable_cache is None:
            return self._full_mutation(conversation_history)

        if self._A_cache_is_stale:
            logger.debug("[CA_v5] cache stale (Fct pending), full mutation fallback")
            self._A_stable_cache = None
            self._A_cache_is_stale = False
            return self._full_mutation(conversation_history)

        return self._incremental_mutation(conversation_history)

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

    if bg:
        # 后台轮：代码生成 Fct，E-stage 同步写入（跳过 A-stage 和 F-stage）
        _brief = (user_message or "")[:80].strip() or "后台审查"
        fct_data = {
            "changes": [{"stage_tag": "已实施", "core_change": _brief}],
            "core_change": _brief,
            "_assemble_status": 0,
        }
        write_turn_v5(
            engine.store, session_id, turn, seq=0,
            role='user', content=user_message,
            fct_text=json.dumps(fct_data, ensure_ascii=False),
            hdl_text=_brief[:100],
            biz_category='bg_review',
            written_at=time.time(),
        )
        logger.info("[CA_v5] pre_llm_call: wrote seq 0 (bg) turn=%d fct=%s", turn, _brief)
        return None

    write_turn_v5(
        engine.store, session_id, turn, seq=0,
        role='user', content=user_message,
        biz_category=None,
        written_at=time.time(),
    )
    logger.info("[CA_v5] pre_llm_call: wrote seq 0 turn=%d", turn)

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
