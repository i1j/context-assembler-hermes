"""ca/config.py — 集中配置管理 (v5.10)

设计决策: F-001 (Config 类变量集中管理)
  viking://resources/projects/context-assembler/design/decision-points-wiki.md#toc-配置体系
  功能：
- 所有可调参数通过环境变量暴露，提供默认值。
- 支持启动校验（validate）和运行时热重载（reload）。
- 定义 _assemble_status 常量。
- 去重开关采用 Fail‑Safe 策略：非法值强制回退为 True（启用）。
- 新增工具轮与 L‑stage 相关配置项、SHUTDOWN_TIMEOUT、BM25_HIT_THRESHOLD。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

logger = logging.getLogger(__name__)

# ── PyYAML（可选）：从 settings.yaml 加载默认值 ──
_YAML_AVAILABLE = False
try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    pass


_YAML_DEFAULTS: Dict[str, Any] = {}
_settings_path: Optional[str] = None


def _load_settings_yaml() -> Dict[str, Any]:
    """从 ca/settings.yaml 加载默认配置值。

    仅类级别调用一次（import 时），后续热重载不重新读取 YAML。
    重复加载因 logger 可能尚未初始化而不可靠——热重载时保持 YAML 值不变，
    仅通过环境变量覆盖。

    Returns:
        YAML 配置字典（文件不存在或解析失败时返回空字典）
    """
    if not _YAML_AVAILABLE:
        return {}
    global _settings_path
    if _settings_path is None:
        _settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.yaml")
    try:
        with open(_settings_path, "r") as _f:
            _data = _yaml.safe_load(_f)
        if isinstance(_data, dict):
            return _data
    except Exception:
        pass  # 静默失败，使用硬编码默认值
    return {}


_YAML_DEFAULTS = _load_settings_yaml()


# _assemble_status 常量
ASSEMBLE_OK = 0
ASSEMBLE_PENDING_BACKFILL = 1
ASSEMBLE_PERMANENT_FAILURE = 2


class Config:
    DEBUG_MODE: ClassVar[bool] = os.getenv("CA_DEBUG", "0") == "1"

    DB_MAX_RETRY: ClassVar[int] = int(os.getenv("CA_DB_MAX_RETRY", "3"))
    DB_BUSY_TIMEOUT_MS: ClassVar[int] = int(os.getenv("CA_DB_BUSY_TIMEOUT", "3000"))
    DB_IDLE_TIMEOUT_SECONDS: ClassVar[int] = int(os.getenv("CA_DB_IDLE_TIMEOUT", "300"))

    CACHE_MAX_SESSIONS: ClassVar[int] = int(os.getenv("CA_CACHE_MAX_SESSIONS", "50"))
    SESSION_TTL: ClassVar[int] = int(os.getenv("CA_SESSION_TTL", "1800"))

    EMBED_BACKEND: ClassVar[str] = os.getenv("CA_EMBED_BACKEND", "ollama")
    EMBED_MODEL: ClassVar[str] = os.getenv("CA_EMBED_MODEL", "dengcao/Qwen3-Embedding-0.6B:Q8_0")
    EMBED_ENDPOINT: ClassVar[str] = os.getenv("CA_EMBED_ENDPOINT", "http://127.0.0.1:11435")
    EMBED_TIMEOUT: ClassVar[float] = float(os.getenv("CA_EMBED_TIMEOUT", "10"))
    EMBED_MAX_RETRIES: ClassVar[int] = int(os.getenv("CA_EMBED_MAX_RETRIES", "2"))
    EMBED_BATCH_PARALLEL_TIMEOUT: ClassVar[float] = float(os.getenv("CA_EMBED_BATCH_PARALLEL_TIMEOUT", "15"))

    OODA_EMBED_CACHE_MAX_SIZE: ClassVar[int] = int(os.getenv("CA_OODA_EMBED_CACHE_MAX_SIZE", "128"))
    OODA_DEDUP_THRESHOLD: ClassVar[float] = float(os.getenv("CA_OODA_DEDUP_THRESHOLD", "0.88"))
    RETRIEVAL_RRF_K: ClassVar[int] = int(os.getenv("CA_RETRIEVAL_RRF_K", "60"))

    LLM_MODEL: ClassVar[str] = os.getenv("CA_LLM_MODEL", "qwen3-4b-instruct:latest")
    LLM_ENDPOINT: ClassVar[str] = os.getenv("CA_LLM_ENDPOINT", "http://localhost:11435")
    LLM_TIMEOUT: ClassVar[float] = float(os.getenv("CA_LLM_TIMEOUT", "120"))
    LLM_MAX_RETRIES: ClassVar[int] = int(os.getenv("CA_LLM_MAX_RETRIES", "2"))
    LLM_NUM_PREDICT: ClassVar[int] = int(os.getenv("CA_LLM_NUM_PREDICT", "24768"))
    LLM_THINK: ClassVar[Optional[bool]] = None
    L1_TEMPERATURE: ClassVar[float] = float(os.getenv("CA_L1_TEMPERATURE", "0.3"))
    L1_MAX_TOKENS: ClassVar[int] = int(os.getenv("CA_L1_MAX_TOKENS", "2048"))
    # v6.4.1: 话题摘要独立 num_predict——L1_MAX_TOKENS=2048 是 F-stage 参数，
    # 多 strand 输出实测 ~2538 字符（num_predict=4096 才完整），2048 被截断
    # → JSON parse 失败 → 46% 话题走代码 fallback。摘要链路独立配置。
    TOPIC_SUMMARY_MAX_TOKENS: ClassVar[int] = int(os.getenv("CA_TOPIC_SUMMARY_MAX_TOKENS", "4096"))

    # v7 (决策 38): S 匹配分参数（merge 候选生成）
    #   alpha = 注入锚权重（语义来源，先验低）
    #   beta  = 融合锚权重（行为事实，先验高）
    #   s_r   = 排除门槛（S > R 剔除；0.5 ≈ 一跳 w≥1）
    S_ALPHA: ClassVar[float] = float(os.getenv("CA_S_ALPHA", "0.4"))
    S_BETA: ClassVar[float] = float(os.getenv("CA_S_BETA", "0.8"))
    S_R_THRESHOLD: ClassVar[float] = float(os.getenv("CA_S_R", "0.5"))

    @classmethod
    def _parse_llm_think(cls) -> Optional[bool]:
        raw = os.getenv("CA_LLM_THINK", "").strip().lower()
        if not raw:
            return None
        if raw in ("1", "true", "yes"):
            return True
        if raw in ("0", "false", "no"):
            return False
        logger.warning("Invalid CA_LLM_THINK: '%s', ignoring", raw)
        return None

    PROTECT_TAIL_TOKENS: ClassVar[int] = int(os.getenv(
        "CA_PROTECT_TAIL_TOKENS",
        str(_YAML_DEFAULTS.get("protect_tail_tokens", "10000")),
    ))

    # 工具尾区保护：只保留最近 N 个对话轮的工具原文。
    # 工具轮与对话轮功能不同——对话需要 20K token 尾区保护，
    # 但工具轮只需最近 2-3 轮的上下文即可。
    TOOL_TAIL_TURN_COUNT: ClassVar[int] = int(os.getenv("CA_TOOL_TAIL_TURN_COUNT", "2"))

    # 系统消息尾区保护：只保留最近 N 条系统消息的原文，
    # 更早的系统消息在汇编时截断为 L0 单行（≤100 字符）。
    SYSTEM_TAIL_TURN_COUNT: ClassVar[int] = int(os.getenv("CA_SYSTEM_TAIL_TURN_COUNT", "2"))
    CONTEXT_LENGTH: ClassVar[int] = int(os.getenv("CA_CONTEXT_LENGTH", "50000"))

    # 历史上下文汇编模式（v6 history injection）：
    # "replace"（默认）→ mutation 模式：pre_llm_call 原地替换 conversation_history
    #   摘要 → LLM 仅见汇编版（省 Token）→ post_llm_call 从 _saved_history 恢复
    #   原始 content。全量原文通过 save/restore 旁路安全过 LLM 调用。
    # "append"       → annotation 模式：保留 conversation_history 原文不变，
    #   摘要文本作为字符串返回，由 Hermes 注入 user message。
    # "off"          → 不注入：CA 仅做数据积累（C-stage），不碰 history
    #
    # 向后兼容：CA_HISTORY_MUTATE=1 → replace, CA_HISTORY_MUTATE=0 → append
    _RAW = os.getenv("CA_HISTORY_INJECTION", "")
    if not _RAW:
        _RAW = "replace" if os.getenv("CA_HISTORY_MUTATE", "1") == "1" else "append"
    if _RAW not in ("replace", "append", "off"):
        logger.warning("Invalid CA_HISTORY_INJECTION=%r, falling back to 'replace'", _RAW)
        _RAW = "replace"
    HISTORY_INJECTION: ClassVar[str] = _RAW

    # 压缩警戒比值：对齐 Hermes compression.threshold。
    # context_length = model_window × threshold（触发压缩的预算上限）。
    COMPRESSION_THRESHOLD: ClassVar[float] = float(os.getenv("CA_COMPRESSION_THRESHOLD", "0.50"))

    # 话题边界检测：当前轮 L1 向量与上一对话轮 L1 向量的余弦距离
    # 低于此阈值 → 新话题。范围 [0, 1]，默认 0.50。
    TOPIC_BOUNDARY_DISTANCE: ClassVar[float] = float(os.getenv("CA_TOPIC_BOUNDARY_DISTANCE", "0.50"))

    # ── 话题拣选（v4.6.0）──

    # 话题分割 Jaccard 阈值：首次合并入口（双实义轮）
    TOPIC_JACCARD_ENTRY: ClassVar[float] = float(os.getenv(
        "CA_TOPIC_JACCARD_ENTRY",
        str(_YAML_DEFAULTS.get("topic_jaccard_entry", "0.04")),
    ))
    # 话题分割 Jaccard 阈值：链内扩展
    TOPIC_JACCARD_CHAIN: ClassVar[float] = float(os.getenv(
        "CA_TOPIC_JACCARD_CHAIN",
        str(_YAML_DEFAULTS.get("topic_jaccard_chain", "0.08")),
    ))
    # 话题半径公式：最近邻形心距离的权重系数（r = min(max_intra, nearest/weight)）
    TOPIC_RADIUS_WEIGHT: ClassVar[float] = float(os.getenv("CA_TOPIC_RADIUS_WEIGHT", "2.0"))
    # 话题检索升级最大数
    TOPIC_MAX_UPGRADE: ClassVar[int] = int(os.getenv("CA_TOPIC_MAX_UPGRADE", "10"))
    # BG 类话题固定级别
    TOPIC_BG_LEVEL: ClassVar[str] = os.getenv("CA_TOPIC_BG_LEVEL", "Far")

    # ── L4 空闲精炼（v5.14）──
    # v6.5 (2026-08): schema 重构（topic_wiki → themes）后精炼轮尚未适配，
    # 默认整体停用（M2: 精炼轮本会话不深挖，留待下次会话适配+深挖）。
    # REFINEMENT_INTERNAL_REFINE 亦默认 false（M5a: internal_refine 职责已由
    # theme merge 语义融合吸收）。

    REFINEMENT_ENABLED: ClassVar[bool] = os.getenv("CA_REFINEMENT_ENABLED", "0") == "1"
    REFINEMENT_CHECK_INTERVAL: ClassVar[int] = int(
        os.getenv("CA_REFINEMENT_CHECK_INTERVAL", "120"))
    REFINEMENT_MIN_NEW_TURNS: ClassVar[int] = int(
        os.getenv("CA_REFINEMENT_MIN_NEW_TURNS", "50"))
    REFINEMENT_MAX_DURATION: ClassVar[float] = float(
        os.getenv("CA_REFINEMENT_MAX_DURATION", "300"))
    REFINEMENT_INTERNAL_REFINE: ClassVar[bool] = os.getenv(
        "CA_REFINEMENT_INTERNAL_REFINE", "0") == "1"
    REFINEMENT_CROSS_VALIDATE: ClassVar[bool] = os.getenv(
        "CA_REFINEMENT_CROSS_VALIDATE", "1") == "1"
    REFINEMENT_HEALTH_SCORE: ClassVar[bool] = os.getenv(
        "CA_REFINEMENT_HEALTH_SCORE", "1") == "1"
    REFINEMENT_GRAPHIFY_SYNC: ClassVar[bool] = os.getenv(
        "CA_REFINEMENT_GRAPHIFY_SYNC", "1") == "1"
    REFINEMENT_MAX_ENTRIES_PER_CYCLE: ClassVar[int] = int(
        os.getenv("CA_REFINEMENT_MAX_ENTRIES_PER_CYCLE", "5"))

    # 累积切割水位（Token 阈值）
    ACCUMULATED_SPLIT_START: ClassVar[int] = int(os.getenv("CA_ACCUMULATED_SPLIT_START", "5000"))
    ACCUMULATED_SPLIT_END: ClassVar[int] = int(os.getenv("CA_ACCUMULATED_SPLIT_END", "8000"))
    JACCARD_PENALTY_MAX: ClassVar[float] = float(os.getenv("CA_JACCARD_PENALTY_MAX", "0.30"))

    # 话题切割水位压力：conv_hist 总 Token 达到此值时满压（线性扣减 Jaccard 0.30）
    TOPIC_PEAK_TOKEN: ClassVar[int] = int(os.getenv(
        "CA_TOPIC_PEAK_TOKEN",
        str(_YAML_DEFAULTS.get("topic_peak_token", "20000")),
    ))

    # 已知模型上下文窗口（Hermes hook 不传 context_length，需自行查表）
    _MODEL_CONTEXT_WINDOW: ClassVar[Dict[str, int]] = {
        "deepseek-v4-flash": 1_000_000,
        "deepseek-v4-pro":   1_000_000,
        "deepseek-chat":     1_000_000,
        "deepseek-reasoner": 1_000_000,
        "deepseek-v4":       1_000_000,
        "deepseek-v3":       65536,
        "claude-3.5-sonnet": 200000,
        "claude-3-opus":     200000,
        "gpt-4o":            128000,
        "gpt-4-turbo":       128000,
    }

    @classmethod
    def context_length_for_model(cls, model_name: str) -> int:
        """返回压缩预算上限 = model_window × COMPRESSION_THRESHOLD。

        优先用 Hermes 的 get_model_context_length()（缓存命中时零开销，
        覆盖 Ollama / Anthropic / OpenRouter 等所有 provider）。
        加载失败或未知模型 → 自己的查表 → CONTEXT_LENGTH 兜底（150K）。

        调试期临时屏蔽：if False 跳过 Hermes 查表，走自有查表 + 兜底。
        恢复调试后将 if False 改为 if True 即可。
        """
        if not model_name:
            return cls.CONTEXT_LENGTH
        window: Optional[int] = None

        # 1. Hermes 运行时（同一进程，缓存命中时极快）
        try:
            from agent.model_metadata import get_model_context_length
            window = get_model_context_length(model_name, base_url="")
        except Exception:
            pass

        # 2. 自己的已知模型表（离线/单元测试时）
        if window is None:
            window = cls._MODEL_CONTEXT_WINDOW.get(model_name)
            if window is None:
                for key, val in cls._MODEL_CONTEXT_WINDOW.items():
                    if key in model_name or model_name in key:
                        window = val
                        break

        # 3. 兜底
        if window is None:
            logger.info("Unknown model '%s', using default CONTEXT_LENGTH=%d", model_name, cls.CONTEXT_LENGTH)
            window = cls.CONTEXT_LENGTH

        result = int(window * cls.COMPRESSION_THRESHOLD)
        logger.debug("context_length_for_model(%s): window=%d × threshold=%.2f = %d",
                     model_name, window, cls.COMPRESSION_THRESHOLD, result)
        return result

    TOOL_PRE_UPGRADE_COUNT: ClassVar[int] = int(os.getenv("CA_TOOL_PRE_UPGRADE_COUNT", "3"))
    TOOL_MAX_UPGRADE_K: ClassVar[int] = int(os.getenv("CA_TOOL_MAX_UPGRADE_K", "3"))
    TOOL_PRE_UPGRADE_WINDOW: ClassVar[int] = int(os.getenv("CA_TOOL_PRE_UPGRADE_WINDOW", "50"))
    TOOL_PRE_UPGRADE_WAIT_TIMEOUT: ClassVar[int] = int(os.getenv("CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT", "30"))
    TOOL_FIELD_PRIORITY_PROFILE: ClassVar[str] = os.getenv("CA_TOOL_FIELD_PRIORITY_PROFILE", "")

    SHUTDOWN_TIMEOUT: ClassVar[int] = int(os.getenv("CA_SHUTDOWN_TIMEOUT", "5"))
    BM25_HIT_THRESHOLD: ClassVar[int] = int(os.getenv("CA_BM25_HIT_THRESHOLD", "5"))

    # OpenViking 话题摘要（已删除，见上方 TOPIC_SUMMARIZE_ENABLED）

    # Hermes profile 名称（用于话题摘要的 profile 隔离）
    # v6.5.2: 运行时 gateway 只注入 HERMES_HOME 不注入 CA_HERMES_PROFILE →
    # 旧实现恒为 'default' → 所有运行时 strand/theme 标错 profile（跨 profile 混入）。
    # 推导链：CA_HERMES_PROFILE env → HERMES_HOME basename → 'default'。
    @staticmethod
    def _detect_profile() -> str:
        env_p = os.getenv("CA_HERMES_PROFILE", "").strip()
        if env_p:
            return env_p
        home = os.getenv("HERMES_HOME", "").strip()
        if home:
            return Path(home).name or "default"
        return "default"

    HERMES_PROFILE: ClassVar[str] = _detect_profile()

    # CA 话题摘要管线（取代 OV Memory Provider）
    TOPIC_SUMMARIZE_ENABLED: ClassVar[bool] = os.getenv("CA_TOPIC_SUMMARIZE_ENABLED", "1") == "1"
    TOPIC_SUMMARY_RECALL_LIMIT: ClassVar[int] = int(os.getenv("CA_TOPIC_SUMMARY_RECALL_LIMIT", "3"))
    # v8: 注入预算（字符）——话题摘要 4B 输出上限 + 迭代提炼触发阈值
    # v6.4: 2000 → 4000（多 strand 输出体积 ~3174 实测，2000 会触发 Bug 2 误杀）
    TOPIC_SUMMARY_MAX_CHARS: ClassVar[int] = int(os.getenv("CA_TOPIC_SUMMARY_MAX_CHARS", "4000"))
    # v8: 输入分批预算（字符）——Fct 输入超限时切分批提炼（记账 remaining）
    TOPIC_SUMMARY_INPUT_BUDGET: ClassVar[int] = int(os.getenv("CA_TOPIC_SUMMARY_INPUT_BUDGET", "12000"))

    @staticmethod
    def _parse_bool_env(key: str, default: bool = True) -> bool:
        raw = os.getenv(key, "").strip().lower()
        if raw in ("1", "true", "yes"):
            return True
        if raw in ("0", "false", "no"):
            return False
        if raw:
            logger.warning("Invalid value for %s: '%s', falling back to %s (safe mode)", key, raw, default)
        return default

    @classmethod
    def is_dedup_enabled(cls) -> bool:
        return cls._parse_bool_env("CA_DEDUP_ENABLED", default=True)


    @classmethod
    def validate(cls) -> None:
        errors = []

        def pos_int(name, val, min_v=1, max_v=None):
            if val < min_v:
                errors.append(f"{name} must be >= {min_v} (got {val})")
            if max_v is not None and val > max_v:
                errors.append(f"{name} must be <= {max_v} (got {val})")

        def pos_float(name, val, min_v=0.01):
            if val < min_v:
                errors.append(f"{name} must be > {min_v} (got {val})")

        pos_int("DB_MAX_RETRY", cls.DB_MAX_RETRY, max_v=10)
        pos_int("DB_BUSY_TIMEOUT_MS", cls.DB_BUSY_TIMEOUT_MS, min_v=100, max_v=30000)
        pos_int("DB_IDLE_TIMEOUT_SECONDS", cls.DB_IDLE_TIMEOUT_SECONDS, min_v=10)
        pos_int("CACHE_MAX_SESSIONS", cls.CACHE_MAX_SESSIONS, max_v=500)
        pos_int("SESSION_TTL", cls.SESSION_TTL, min_v=60)
        pos_float("EMBED_TIMEOUT", cls.EMBED_TIMEOUT)
        pos_int("EMBED_MAX_RETRIES", cls.EMBED_MAX_RETRIES, max_v=5)
        pos_float("EMBED_BATCH_PARALLEL_TIMEOUT", cls.EMBED_BATCH_PARALLEL_TIMEOUT)
        pos_int("OODA_EMBED_CACHE_MAX_SIZE", cls.OODA_EMBED_CACHE_MAX_SIZE, min_v=16)
        pos_float("OODA_DEDUP_THRESHOLD", cls.OODA_DEDUP_THRESHOLD, min_v=0.5)
        pos_int("RETRIEVAL_RRF_K", cls.RETRIEVAL_RRF_K)
        pos_float("LLM_TIMEOUT", cls.LLM_TIMEOUT)
        pos_int("LLM_MAX_RETRIES", cls.LLM_MAX_RETRIES, max_v=5)
        pos_int("LLM_NUM_PREDICT", cls.LLM_NUM_PREDICT, min_v=100)
        pos_float("L1_TEMPERATURE", cls.L1_TEMPERATURE, min_v=0.0)
        if cls.L1_TEMPERATURE > 2.0:
            errors.append(f"L1_TEMPERATURE must be <= 2.0 (got {cls.L1_TEMPERATURE})")
        pos_int("L1_MAX_TOKENS", cls.L1_MAX_TOKENS, min_v=50, max_v=4096)
        pos_int("CONTEXT_LENGTH", cls.CONTEXT_LENGTH, min_v=1000)
        pos_int("TOOL_PRE_UPGRADE_COUNT", cls.TOOL_PRE_UPGRADE_COUNT, max_v=10)
        pos_int("TOOL_MAX_UPGRADE_K", cls.TOOL_MAX_UPGRADE_K, max_v=10)
        pos_int("TOOL_PRE_UPGRADE_WINDOW", cls.TOOL_PRE_UPGRADE_WINDOW, min_v=10, max_v=200)
        pos_int("TOOL_PRE_UPGRADE_WAIT_TIMEOUT", cls.TOOL_PRE_UPGRADE_WAIT_TIMEOUT, min_v=5, max_v=120)
        pos_int("SHUTDOWN_TIMEOUT", cls.SHUTDOWN_TIMEOUT, min_v=1, max_v=30)
        pos_int("BM25_HIT_THRESHOLD", cls.BM25_HIT_THRESHOLD, min_v=1, max_v=20)
        pos_float("TOPIC_JACCARD_ENTRY", cls.TOPIC_JACCARD_ENTRY, min_v=0.01)
        pos_float("TOPIC_JACCARD_CHAIN", cls.TOPIC_JACCARD_CHAIN, min_v=0.01)
        pos_float("TOPIC_RADIUS_WEIGHT", cls.TOPIC_RADIUS_WEIGHT, min_v=1.0)
        pos_int("ACCUMULATED_SPLIT_START", cls.ACCUMULATED_SPLIT_START, min_v=1000)
        pos_int("ACCUMULATED_SPLIT_END", cls.ACCUMULATED_SPLIT_END, min_v=2000)
        pos_float("JACCARD_PENALTY_MAX", cls.JACCARD_PENALTY_MAX, min_v=0.0)
        pos_int("TOPIC_MAX_UPGRADE", cls.TOPIC_MAX_UPGRADE, max_v=20)
        if cls.TOPIC_BG_LEVEL not in ("Act", "Rel", "Far"):
            errors.append(f"TOPIC_BG_LEVEL must be Act/Rel/Far (got {cls.TOPIC_BG_LEVEL})")

        pos_int("REFINEMENT_CHECK_INTERVAL", cls.REFINEMENT_CHECK_INTERVAL, min_v=10, max_v=3600)
        pos_int("REFINEMENT_MIN_NEW_TURNS", cls.REFINEMENT_MIN_NEW_TURNS, min_v=1, max_v=1000)
        pos_float("REFINEMENT_MAX_DURATION", cls.REFINEMENT_MAX_DURATION, min_v=10)
        pos_int("REFINEMENT_MAX_ENTRIES_PER_CYCLE", cls.REFINEMENT_MAX_ENTRIES_PER_CYCLE, min_v=1, max_v=20)

        if errors:
            raise ValueError("Configuration validation failed:\n" + "\n".join(errors))

    @classmethod
    def reload(cls) -> None:
        try:
            cls.DEBUG_MODE = os.getenv("CA_DEBUG", "0") == "1"
            cls.DB_MAX_RETRY = int(os.getenv("CA_DB_MAX_RETRY", str(cls.DB_MAX_RETRY)))
            cls.DB_BUSY_TIMEOUT_MS = int(os.getenv("CA_DB_BUSY_TIMEOUT", str(cls.DB_BUSY_TIMEOUT_MS)))
            cls.DB_IDLE_TIMEOUT_SECONDS = int(os.getenv("CA_DB_IDLE_TIMEOUT", str(cls.DB_IDLE_TIMEOUT_SECONDS)))
            cls.CACHE_MAX_SESSIONS = int(os.getenv("CA_CACHE_MAX_SESSIONS", str(cls.CACHE_MAX_SESSIONS)))
            cls.SESSION_TTL = int(os.getenv("CA_SESSION_TTL", str(cls.SESSION_TTL)))
            cls.EMBED_BACKEND = os.getenv("CA_EMBED_BACKEND", cls.EMBED_BACKEND)
            cls.EMBED_MODEL = os.getenv("CA_EMBED_MODEL", cls.EMBED_MODEL)
            cls.EMBED_ENDPOINT = os.getenv("CA_EMBED_ENDPOINT", cls.EMBED_ENDPOINT)
            cls.EMBED_TIMEOUT = float(os.getenv("CA_EMBED_TIMEOUT", str(cls.EMBED_TIMEOUT)))
            cls.EMBED_MAX_RETRIES = int(os.getenv("CA_EMBED_MAX_RETRIES", str(cls.EMBED_MAX_RETRIES)))
            cls.EMBED_BATCH_PARALLEL_TIMEOUT = float(os.getenv("CA_EMBED_BATCH_PARALLEL_TIMEOUT", str(cls.EMBED_BATCH_PARALLEL_TIMEOUT)))
            cls.OODA_EMBED_CACHE_MAX_SIZE = int(os.getenv("CA_OODA_EMBED_CACHE_MAX_SIZE", str(cls.OODA_EMBED_CACHE_MAX_SIZE)))
            cls.OODA_DEDUP_THRESHOLD = float(os.getenv("CA_OODA_DEDUP_THRESHOLD", str(cls.OODA_DEDUP_THRESHOLD)))
            cls.RETRIEVAL_RRF_K = int(os.getenv("CA_RETRIEVAL_RRF_K", str(cls.RETRIEVAL_RRF_K)))
            cls.LLM_MODEL = os.getenv("CA_LLM_MODEL", cls.LLM_MODEL)
            cls.LLM_ENDPOINT = os.getenv("CA_LLM_ENDPOINT", cls.LLM_ENDPOINT)
            cls.LLM_TIMEOUT = float(os.getenv("CA_LLM_TIMEOUT", str(cls.LLM_TIMEOUT)))
            cls.LLM_MAX_RETRIES = int(os.getenv("CA_LLM_MAX_RETRIES", str(cls.LLM_MAX_RETRIES)))
            cls.LLM_NUM_PREDICT = int(os.getenv("CA_LLM_NUM_PREDICT", str(cls.LLM_NUM_PREDICT)))
            cls.S_ALPHA = float(os.getenv("CA_S_ALPHA", str(cls.S_ALPHA)))
            cls.S_BETA = float(os.getenv("CA_S_BETA", str(cls.S_BETA)))
            cls.S_R_THRESHOLD = float(os.getenv("CA_S_R", str(cls.S_R_THRESHOLD)))
            cls.LLM_THINK = cls._parse_llm_think()
            cls.L1_TEMPERATURE = float(os.getenv("CA_L1_TEMPERATURE", str(cls.L1_TEMPERATURE)))
            cls.L1_MAX_TOKENS = int(os.getenv("CA_L1_MAX_TOKENS", str(cls.L1_MAX_TOKENS)))
            cls.PROTECT_TAIL_TOKENS = int(os.getenv("CA_PROTECT_TAIL_TOKENS", str(cls.PROTECT_TAIL_TOKENS)))
            cls.TOOL_TAIL_TURN_COUNT = int(os.getenv("CA_TOOL_TAIL_TURN_COUNT", str(cls.TOOL_TAIL_TURN_COUNT)))
            cls.SYSTEM_TAIL_TURN_COUNT = int(os.getenv("CA_SYSTEM_TAIL_TURN_COUNT", str(cls.SYSTEM_TAIL_TURN_COUNT)))
            cls.TOPIC_BOUNDARY_DISTANCE = float(os.getenv("CA_TOPIC_BOUNDARY_DISTANCE", str(cls.TOPIC_BOUNDARY_DISTANCE)))
            cls.COMPRESSION_THRESHOLD = float(os.getenv("CA_COMPRESSION_THRESHOLD", str(cls.COMPRESSION_THRESHOLD)))
            cls.CONTEXT_LENGTH = int(os.getenv("CA_CONTEXT_LENGTH", str(cls.CONTEXT_LENGTH)))
            _raw = os.getenv("CA_HISTORY_INJECTION", "")
            if not _raw:
                _raw = "replace" if os.getenv("CA_HISTORY_MUTATE", "1") == "1" else "append"
            if _raw not in ("replace", "append", "off"):
                logger.warning("Invalid CA_HISTORY_INJECTION=%r in reload, falling back to 'replace'", _raw)
                _raw = "replace"
            cls.HISTORY_INJECTION = _raw
            cls.TOOL_PRE_UPGRADE_COUNT = int(os.getenv("CA_TOOL_PRE_UPGRADE_COUNT", str(cls.TOOL_PRE_UPGRADE_COUNT)))
            cls.TOOL_MAX_UPGRADE_K = int(os.getenv("CA_TOOL_MAX_UPGRADE_K", str(cls.TOOL_MAX_UPGRADE_K)))
            cls.TOOL_PRE_UPGRADE_WINDOW = int(os.getenv("CA_TOOL_PRE_UPGRADE_WINDOW", str(cls.TOOL_PRE_UPGRADE_WINDOW)))
            cls.TOOL_PRE_UPGRADE_WAIT_TIMEOUT = int(os.getenv("CA_TOOL_PRE_UPGRADE_WAIT_TIMEOUT", str(cls.TOOL_PRE_UPGRADE_WAIT_TIMEOUT)))
            cls.TOOL_FIELD_PRIORITY_PROFILE = os.getenv("CA_TOOL_FIELD_PRIORITY_PROFILE", cls.TOOL_FIELD_PRIORITY_PROFILE)
            cls.SHUTDOWN_TIMEOUT = int(os.getenv("CA_SHUTDOWN_TIMEOUT", str(cls.SHUTDOWN_TIMEOUT)))
            cls.BM25_HIT_THRESHOLD = int(os.getenv("CA_BM25_HIT_THRESHOLD", str(cls.BM25_HIT_THRESHOLD)))
            cls.HERMES_PROFILE = cls._detect_profile()
            cls.TOPIC_SUMMARIZE_ENABLED = os.getenv("CA_TOPIC_SUMMARIZE_ENABLED", "1") == "1"
            cls.TOPIC_SUMMARY_RECALL_LIMIT = int(os.getenv("CA_TOPIC_SUMMARY_RECALL_LIMIT", str(cls.TOPIC_SUMMARY_RECALL_LIMIT)))
            cls.TOPIC_SUMMARY_MAX_CHARS = int(os.getenv("CA_TOPIC_SUMMARY_MAX_CHARS", str(cls.TOPIC_SUMMARY_MAX_CHARS)))
            cls.TOPIC_SUMMARY_MAX_TOKENS = int(os.getenv("CA_TOPIC_SUMMARY_MAX_TOKENS", str(cls.TOPIC_SUMMARY_MAX_TOKENS)))
            cls.TOPIC_SUMMARY_INPUT_BUDGET = int(os.getenv("CA_TOPIC_SUMMARY_INPUT_BUDGET", str(cls.TOPIC_SUMMARY_INPUT_BUDGET)))
            cls.TOPIC_JACCARD_ENTRY = float(os.getenv(
                "CA_TOPIC_JACCARD_ENTRY",
                str(cls.TOPIC_JACCARD_ENTRY),
            ))
            cls.TOPIC_JACCARD_CHAIN = float(os.getenv(
                "CA_TOPIC_JACCARD_CHAIN",
                str(cls.TOPIC_JACCARD_CHAIN),
            ))
            cls.TOPIC_RADIUS_WEIGHT = float(os.getenv("CA_TOPIC_RADIUS_WEIGHT", str(cls.TOPIC_RADIUS_WEIGHT)))
            cls.TOPIC_MAX_UPGRADE = int(os.getenv("CA_TOPIC_MAX_UPGRADE", str(cls.TOPIC_MAX_UPGRADE)))
            cls.TOPIC_BG_LEVEL = os.getenv("CA_TOPIC_BG_LEVEL", cls.TOPIC_BG_LEVEL)
            cls.ACCUMULATED_SPLIT_START = int(os.getenv("CA_ACCUMULATED_SPLIT_START", str(cls.ACCUMULATED_SPLIT_START)))
            cls.ACCUMULATED_SPLIT_END = int(os.getenv("CA_ACCUMULATED_SPLIT_END", str(cls.ACCUMULATED_SPLIT_END)))
            cls.JACCARD_PENALTY_MAX = float(os.getenv("CA_JACCARD_PENALTY_MAX", str(cls.JACCARD_PENALTY_MAX)))

            cls.REFINEMENT_ENABLED = os.getenv("CA_REFINEMENT_ENABLED", "0") == "1"
            cls.REFINEMENT_CHECK_INTERVAL = int(os.getenv("CA_REFINEMENT_CHECK_INTERVAL", str(cls.REFINEMENT_CHECK_INTERVAL)))
            cls.REFINEMENT_MIN_NEW_TURNS = int(os.getenv("CA_REFINEMENT_MIN_NEW_TURNS", str(cls.REFINEMENT_MIN_NEW_TURNS)))
            cls.REFINEMENT_MAX_DURATION = float(os.getenv("CA_REFINEMENT_MAX_DURATION", str(cls.REFINEMENT_MAX_DURATION)))
            cls.REFINEMENT_INTERNAL_REFINE = os.getenv("CA_REFINEMENT_INTERNAL_REFINE", "0") == "1"
            cls.REFINEMENT_CROSS_VALIDATE = os.getenv("CA_REFINEMENT_CROSS_VALIDATE", "1") == "1"
            cls.REFINEMENT_HEALTH_SCORE = os.getenv("CA_REFINEMENT_HEALTH_SCORE", "1") == "1"
            cls.REFINEMENT_GRAPHIFY_SYNC = os.getenv("CA_REFINEMENT_GRAPHIFY_SYNC", "1") == "1"
            cls.REFINEMENT_MAX_ENTRIES_PER_CYCLE = int(os.getenv("CA_REFINEMENT_MAX_ENTRIES_PER_CYCLE", str(cls.REFINEMENT_MAX_ENTRIES_PER_CYCLE)))

            cls.validate()
            logger.info("Configuration reloaded and validated.")
        except ValueError as e:
            logger.critical("Configuration reload failed, keeping old values: %s", e)
