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

    should_compress 返回 False（压缩由 pre_llm_call hook 驱动），
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
        """返回 False — 组装由 pre_llm_call hook 每轮驱动。

        CA 不通过 Hermes compress_context() 路径触发压缩。
        但支持手动 /compress 走 compress() 路径。
        """
        return False

    def compress(
        self,
        messages: list,
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> list:
        """手动 /compress 回退路径。

        复用 v5.10 的 _simple_mutation_mode_v5 逻辑（turn_stream + topic_grade），
        但将 FAR 话题行整行删除而非替换为"略"。

        返回新消息列表（比原始短，FAR 话题的行被删除）。
        """
        plugin = self._get_plugin()
        if not plugin or plugin._engine_errored or not plugin._engine:
            return messages

        # 安全检查：如果已组装过且未 force，直接返回
        last_len = getattr(self, '_last_compress_msg_len', 0)
        if not force and len(messages) <= last_len:
            return messages

        # 复用 v5.10 尾区保护逻辑
        tail_boundary = 0
        _user_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                _user_count += 1
                if _user_count >= 2:
                    tail_boundary = i
                    break

        # 通过 plugin 的 _simple_mutation_mode_v5 做内容替换
        # 然后手动删 row：FAR 级的 thought/tool 行从 messages 中移除
        from ca.store import get_turn_ca_rows
        store = plugin._engine.store
        sid = plugin._session_id

        # Phase 1: 确定每个 turn 的 topic_grade
        topic_mgr = plugin._topic_mgr

        # Phase 2: 扫描需删除的 conv_idx
        # 只有 FAR 排在 tail 保护区之外的 thought/tool 才删除
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
            if role not in ("assistant", "tool"):
                continue
            # thought/tool 行
            if topic_mgr:
                grade = topic_mgr.get_turn_grade(current_turn + 1)
            else:
                continue
            # FAR → 删除整行
            if grade == TopicGrade.FAR:
                drop_indices.add(i)
                # 如果是 tool 行，一并删除其前的 thought 行
                if role == "tool":
                    # 向前找同一 turn 的 assistant(tool_calls) 行
                    for j in range(i - 1, -1, -1):
                        if j in drop_indices:
                            continue
                        if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                            # 检查是否是同一个 turn
                            t_turn = -1
                            for k in range(j + 1):
                                if messages[k].get("role") == "user":
                                    t_turn += 1
                            if t_turn == current_turn:
                                drop_indices.add(j)
                                break
                            break
                        break

        if not drop_indices:
            return messages

        # Phase 3: 构建新消息列表（跳过 drop_indices 中的索引）
        new_messages = [m for i, m in enumerate(messages) if i not in drop_indices]

        self._last_compress_msg_len = len(messages)
        self.compression_count += 1
        return new_messages

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

        设计决策: C-010 (A-stage 解耦), TP-002 (话题等级 → Grade 映射)
          viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-a-stage-同步上下文组装
          turn_stream 查 → 角色队列匹配 → 话题等级(ACT/REL/FAR)驱动替换

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
            if i >= tail_boundary:
                continue

            if role == "user":
                row_type = "user"
            elif role == "assistant" and not msg.get("tool_calls"):
                row_type = "fin"
            elif role == "assistant":
                row_type = "thought"
            elif role == "tool":
                row_type = "tool"
            else:
                continue

            turn_rows.setdefault(current_turn, []).append((i, row_type))

        # Phase 1.5: 每轮只保留最后一个 "fin" 为真 fin
        # Hermes 在做 conv_hist 时，含 content+tool_calls 的 assistant 行被丢掉 tool_calls，
        # 使 Phase 1 误判为 fin。真 fin 必然是每轮最后一个无 tool_calls 的 assistant 行。
        for _t, _rows in turn_rows.items():
            _fin_positions = [_p for _p, (_ci, _rt) in enumerate(_rows) if _rt == "fin"]
            if len(_fin_positions) > 1:
                for _p in _fin_positions[:-1]:
                    _rows[_p] = (_rows[_p][0], "thought")

        # Phase 2: 逐 turn 做角色队列匹配
        replaced = 0
        skipped = 0

        # 话题等级 → 行等级映射
        # user/fin: 不降级，直接取话题等级
        # thought/tool: 降一级
        user_fin_map = {
            TopicGrade.ACT: Grade.ELM,   # 原文保留
            TopicGrade.REL: Grade.FCT,   # 完整摘要
            TopicGrade.FAR: Grade.HDL,   # Hdl[:150]
        }
        thought_tool_map = {
            TopicGrade.ACT: Grade.FCT,   # 完整摘要
            TopicGrade.REL: Grade.HDL,   # Hdl[:150]
            TopicGrade.FAR: None,         # 清空
        }

        for turn_num, rows in turn_rows.items():
            if turn_num < 0:  # turn 1（turn_num=0）现在可处理了
                continue

            # 1. 先确定 topic_grade
            topic_grade = self._topic_mgr.get_turn_grade(turn_num + 1) if self._topic_mgr else TopicGrade.ACT

            # 2. 算各 row_type 的映射等级
            thought_grade = thought_tool_map.get(topic_grade)  # FCT / HDL / None(清空)
            user_grade = user_fin_map.get(topic_grade)          # ELM / FCT / HDL

            # 3. 读 DB（现在统一拉 7 列：seq, role, finish, tc_json, content, Fct, Hdl）
            ca_rows = get_turn_ca_rows(store, sid, turn_num + 1)
            if not ca_rows:
                skipped += sum(1 for _, t in rows)
                continue

            # 【日志 B】逐 turn 输出 grade
            logger.info("[CA_v5_mutate] turn=%d topic_grade=%s thought_grade=%s user_grade=%s topic_mgr_exist=%s n_fin=%d n_thought=%d n_tool=%d",
                       turn_num + 1, topic_grade, thought_grade, user_grade, self._topic_mgr is not None,
                       len([r for r in ca_rows if r[1] == "assistant" and r[2] == "stop"]),
                       len([r for r in ca_rows if r[1] == "assistant" and r[2] != "stop"]),
                       len([r for r in ca_rows if r[1] == "tool"]))

            # 4. 按需选列填充队列
            ca_users: list = []
            ca_fins: list = []
            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role, finish_reason, tc_json, content, fct, hdl in ca_rows:
                if role == "user":
                    if user_grade == Grade.FCT and fct:
                        ca_users.append(fct)
                    elif user_grade == Grade.FCT:
                        logger.warning("[CA_v5] turn=%d user Fct is empty/NULL for grade FCT, falling back to Elm", turn_num + 1)
                    elif user_grade == Grade.HDL:
                        ca_users.append((hdl or "")[:150])
                        if not hdl:
                            logger.warning("[CA_v5] turn=%d user Hdl is empty/NULL for grade HDL, content will be empty", turn_num + 1)
                    # ELM: 原文在 conv 中，不填队列
                elif role == "assistant" and finish_reason == "stop":
                    if user_grade == Grade.FCT and fct:
                        ca_fins.append(fct)
                    elif user_grade == Grade.FCT:
                        logger.warning("[CA_v5] turn=%d fin Fct is empty/NULL for grade FCT, falling back to Elm", turn_num + 1)
                    elif user_grade == Grade.HDL:
                        ca_fins.append((hdl or "")[:150])
                        if not hdl:
                            logger.warning("[CA_v5] turn=%d fin Hdl is empty/NULL for grade HDL, content will be empty", turn_num + 1)
                    # ELM: 原文保留
                elif role == "assistant":
                    if thought_grade == Grade.FCT and fct:
                        ca_thoughts.append(fct)
                    elif thought_grade == Grade.FCT:
                        logger.warning("[CA_v5] turn=%d thought Fct is empty/NULL for grade FCT, falling back to Elm", turn_num + 1)
                    elif thought_grade == Grade.HDL:
                        ca_thoughts.append((hdl or "")[:150])
                        if not hdl:
                            logger.warning("[CA_v5] turn=%d thought Hdl is empty/NULL for grade HDL, content will be empty", turn_num + 1)
                    # None(FAR): 清空，不填队列
                elif role == "tool":
                    if thought_grade == Grade.FCT and fct:
                        ca_tools.append(fct)
                    elif thought_grade == Grade.FCT:
                        logger.warning("[CA_v5] turn=%d tool Fct is empty/NULL for grade FCT, falling back to Elm", turn_num + 1)
                    elif thought_grade == Grade.HDL:
                        ca_tools.append((hdl or "")[:150])
                        if not hdl:
                            logger.warning("[CA_v5] turn=%d tool Hdl is empty/NULL for grade HDL, content will be empty", turn_num + 1)
                    # None(FAR): 清空，不填队列

            # 5. 替换（直接 pop，无 if/else 选择列）
            ui, fi, ti, tj = 0, 0, 0, 0
            for conv_idx, row_type in rows:

                if row_type == "user":
                    if user_grade == Grade.ELM:
                        pass  # 原文保留
                    elif ui < len(ca_users):
                        conversation_history[conv_idx]["content"] = ca_users[ui]
                        ui += 1
                    replaced += 1

                elif row_type == "fin":
                    if user_grade == Grade.ELM:
                        # 原文保留，但清理 reasoning/tool_calls
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    elif fi < len(ca_fins):
                        conversation_history[conv_idx]["content"] = ca_fins[fi]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        fi += 1
                    else:
                        # 队列耗尽: 原文保留
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    replaced += 1

                elif row_type == "thought":
                    if thought_grade is None:  # FAR → 略
                        conversation_history[conv_idx]["content"] = "略"
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    elif ti < len(ca_thoughts):
                        conversation_history[conv_idx]["content"] = ca_thoughts[ti]
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                        ti += 1
                    # 队列耗尽: 保留原文，至少清理
                    else:
                        conversation_history[conv_idx].pop("reasoning_content", None)
                        conversation_history[conv_idx].pop("tool_calls", None)
                    replaced += 1

                elif row_type == "tool":
                    if thought_grade is None:  # FAR → 略
                        conversation_history[conv_idx]["content"] = "略"
                    elif tj < len(ca_tools):
                        conversation_history[conv_idx]["content"] = ca_tools[tj]
                        tj += 1
                    # 队列耗尽: 保留原文
                    replaced += 1

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

        设计决策: TP-001 (话题分割), C-010 (A-stage 解耦)
          viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-话题拣选重构-v460-已实现
          前提：话题未切换，_A_stable_cache 有效。用于高话题稳定性场景减少全量替换开销。
        """
        if not conversation_history or not self._engine:
            return None

        # 保存快照（供 post_llm_call 还原 Elm）
        self._saved_history_snapshot = [{**m} for m in conversation_history]

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

            if role == "assistant":
                # A-stage 已 pop 所有 thought 行的 tool_calls，无法用 msg.get("tool_calls")
                # 区分 thought/fin。改为检查下一条消息是否为 tool 行。
                next_msg = conversation_history[i + 1] if i + 1 < len(conversation_history) else None
                if next_msg and next_msg.get("role") == "tool":
                    row_type = "thought"
                else:
                    row_type = "fin"
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
            user_fct = next((r[5] for r in ca_rows if r[1] == "user"), None)
            if not user_fct:
                logger.debug("[CA_v5] incremental: turn=%d user Fct empty/NULL, marking pending (skipped=%d)",
                             turn_num, sum(1 for _, t in rows if t != "fin"))
                turn_pending = True
                skipped += sum(1 for _, t in rows if t != "fin")
                continue

            # 角色队列构建（只取有 Fct 的行）
            ca_thoughts: list = []
            ca_tools: list = []
            for _seq, role_, finish_reason, tc_json, content, fct, hdl in ca_rows:
                if role_ == "user":
                    continue
                if role_ == "assistant" and finish_reason == "stop":
                    continue
                if fct is None:
                    logger.debug("[CA_v5] incremental: turn=%d row role=%s Fct is None, marking pending",
                                 turn_num, role_)
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

    @staticmethod
    def _estimate_conv_tokens(conv_hist: list) -> int:
        """字符粗估 conv_hist 总 token（含 tool/assistant/reasoning 全部内容）。"""
        total_chars = 0
        for msg in conv_hist:
            total_chars += len(msg.get("content", "") or "")
            for tc in msg.get("tool_calls", []) or []:
                total_chars += len(tc.get("function", {}).get("arguments", ""))
        # CJK ~1.2, 工具/代码 ~3, 综合 1.5 chars/tok
        return int(total_chars // 1.5)

    def pre_llm_call_v5(self, **kwargs: Any) -> Optional[str]:
        """v5 A-stage: 话题检测 → topic-aware 替换。"""
        conversation_history = kwargs.get("conversation_history", [])
        if not isinstance(conversation_history, list) or not conversation_history:
            return None

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
                # 【日志 A】话题定级后 dump
                try:
                    tg = self._topic_mgr.get_topic_grades()
                    t1g = self._topic_mgr.get_turn_grade(1)
                    td = getattr(self._topic_mgr, "_topic_data", {})
                    _dump = {
                        "turn": turn,
                        "topic_grades": {str(k): str(v) for k, v in tg.items()},
                        "turn_1_grade": str(t1g),
                        "topic_data": {str(k): {"turns": v.get("turns",[]), "has_centroid": v.get("centroid") is not None, "max_intra": v.get("max_intra")}
                                      for k, v in td.items()},
                        "topic_mgr_ok": self._topic_mgr is not None,
                        "engine_ok": self._engine is not None,
                    }
                    logger.info("[CA_v5_grade] grades=%s turn1_grade=%s topic_data_has_centroid=%s",
                               {str(k): str(v) for k, v in tg.items()}, t1g,
                               {str(k): v.get("centroid") is not None for k, v in td.items()})
                    with open("/tmp/ca_topic_grades.jsonl", "a") as _f:
                        _f.write(json.dumps(_dump, ensure_ascii=False) + "\n")
                except Exception as exc:
                    logger.warning("[CA_v5] topic grades dump failed: %s", exc)

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
                # 还原所有被 mutation 修改的字段
                for _key in ("content", "reasoning_content", "tool_calls"):
                    if _key in orig_dict:
                        ch[_key] = orig_dict[_key]
                    else:
                        ch.pop(_key, None)
            self._saved_history_snapshot = None

        engine.process_turn_f_stage(turn)
        logger.info("[CA_v5] post_llm_call: process_turn_f_stage called for turn %d", turn)


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
            role='user', content=user_message,
            fct_text=user_message,
            hdl_text=user_message[:100],
            biz_category='bg_review',
            written_at=time.time(),
        )
        logger.info("[CA_v5] pre_llm_call: wrote seq 0 (bg) turn=%d", turn)
        return None

    write_turn_v5(
        engine.store, session_id, turn, seq=0,
        role='user', content=user_message,
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
