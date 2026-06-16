"""
ca/tool_plan.py — 工具类型驱动的摘要等级策略 (v5.0)

按工具类型定义 Fct / Hdl 级下应保留的字段。
A-stage 消费此模块裁剪 ToolSummarizer 输出的完整数据。

消费链路：
  ToolSummarizer(全字段) → turn_stream.Fct(全字段)
  ↓
  A-stage read_fct_v5() → ToolPlan.filter(data, level)
  ↓
  msg["content"] = json.dumps(filtered)

等级含义：
  elm — 原始数据全量保留（ToolPlan 不参与，A-stage 直接跳过）
  fct — 事实摘要，保留结构化字段 + 工具关键输出
  hdl — 标题，只留工具名和一行 result_summary
"""

from typing import Any, Dict, Optional


class ToolPlan:
    """
    工具摘要等级策略。

    每个工具定义 Fct / Hdl 两级下允许保留的字段。
    Elm 级不进 ToolPlan（全量保留）。
    data 中未出现在白名单的字段被裁剪。
    未匹配的工具走 "default" 策略。
    """

    _STRATEGIES: Dict[str, Dict[str, list]] = {
        # ────────────────────────────────────
        # 内容型：Fct 保留结构化结果 + 工具输出
        # ────────────────────────────────────
        "read_file": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "terminal": {
            "fct": ["tool_name", "tool_args", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "execute_code": {
            "fct": ["tool_name", "tool_args", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "patch": {
            "fct": ["tool_name", "tool_args", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "skill_view": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "write_file": {
            "fct": ["tool_name", "tool_args", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },

        # ────────────────────────────────────
        # 状态型：Fct=Hdl，只留摘要，丢弃 tool_args
        # ────────────────────────────────────
        "search_files": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "memory": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "skill_manage": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "skills_list": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "todo": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "web_search": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
        "web_extract": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },

        # ────────────────────────────────────
        # 默认（未匹配的工具）
        # ────────────────────────────────────
        "default": {
            "fct": ["tool_name", "result_summary"],
            "hdl": ["tool_name", "result_summary"],
        },
    }

    @classmethod
    def filter(cls, data: dict, level: str) -> dict:
        """
        按工具策略过滤摘要数据。

        Args:
            data: ToolSummarizer 生成的完整数据
            level: "fct" | "hdl" | "elm"

        Returns:
            过滤后的 dict（elm 级原样返回）
        """
        if level == "elm":
            return data

        tool_name = data.get("tool_name", "default")
        strategy = cls._STRATEGIES.get(tool_name, cls._STRATEGIES["default"])
        allowed = strategy.get(level) or strategy.get("fct", [])

        # 补充字段：error 永远保留（tool 明显出错时）
        if data.get("error"):
            allowed = list(allowed) + ["error"]

        return {k: v for k, v in data.items() if k in allowed}

    @classmethod
    def allowed_fields(cls, tool_name: str, level: str) -> list:
        """暴露策略字段列表供测试/调试。"""
        strategy = cls._STRATEGIES.get(tool_name, cls._STRATEGIES["default"])
        return strategy.get(level) or strategy.get("fct", [])
