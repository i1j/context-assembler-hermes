"""决策 44 测试：ca.meta_marker — provider 推断 / usage 归一 / 元数据提取。"""
from types import SimpleNamespace

from ca.meta_marker import (
    infer_provider,
    extract_usage_tokens,
    extract_reasoning_text,
    extract_response_meta,
)


class TestInferProvider:
    def test_explicit_provider_wins(self):
        assert infer_provider("deepseek", "gpt-4o", "https://api.openai.com") == "deepseek"

    def test_base_url_match(self):
        assert infer_provider("", "some-model", "https://api.deepseek.com/v1") == "deepseek"

    def test_model_match(self):
        assert infer_provider("", "gpt-4o-mini", "") == "openai"

    def test_unknown(self):
        assert infer_provider("", "mystery-model", "https://example.invalid") == "unknown"


class TestExtractUsageTokens:
    def test_canonical_usage_summary(self):
        """Hermes 实际 usage summary：prompt_tokens/output_tokens（无 completion_tokens）。"""
        prompt, completion = extract_usage_tokens({
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_tokens": 5, "cache_write_tokens": 2,
            "prompt_tokens": 17, "total_tokens": 37,
        })
        assert prompt == 17
        assert completion == 20

    def test_legacy_dict(self):
        prompt, completion = extract_usage_tokens({"prompt_tokens": 100, "completion_tokens": 50})
        assert (prompt, completion) == (100, 50)

    def test_namespace(self):
        usage = SimpleNamespace(prompt_tokens=30, output_tokens=40)
        prompt, completion = extract_usage_tokens(usage)
        assert (prompt, completion) == (30, 40)

    def test_none(self):
        assert extract_usage_tokens(None) == (None, None)


class TestExtractReasoningText:
    def test_provider_data_reasoning_content_first(self):
        """reasoning_content 与顶层 reasoning 双源去重合并（不互相覆盖）。"""
        msg = SimpleNamespace(reasoning="归一化推理", provider_data={"reasoning_content": "原始推理"}, content="表面")
        assert extract_reasoning_text(msg) == "原始推理\n\n归一化推理"

    def test_top_level_reasoning_fallback(self):
        msg = SimpleNamespace(reasoning="归一化推理", provider_data={}, content="表面")
        assert extract_reasoning_text(msg) == "归一化推理"

    def test_reasoning_details_summary(self):
        msg = SimpleNamespace(reasoning=None,
                              provider_data={"reasoning_details": [
                                  {"type": "reasoning.summary", "summary": "步骤一"},
                                  {"type": "reasoning.summary", "summary": "步骤二"},
                              ]},
                              content="表面")
        assert extract_reasoning_text(msg) == "步骤一\n\n步骤二"

    def test_codex_and_anthropic_containers(self):
        msg = SimpleNamespace(reasoning=None,
                              provider_data={
                                  "codex_reasoning_items": [{"text": "codex think"}],
                                  "anthropic_content_blocks": [{"type": "thinking", "thinking": "claude think"}],
                              },
                              content="表面")
        text = extract_reasoning_text(msg)
        assert "codex think" in text
        assert "claude think" in text

    def test_inline_think_tags_last_resort(self):
        msg = SimpleNamespace(reasoning=None, provider_data={},
                              content="<think>先想一下</think>答案")
        assert extract_reasoning_text(msg) == "先想一下"

    def test_no_content_fallback(self):
        msg = SimpleNamespace(reasoning=None, provider_data={}, content="表面")
        assert extract_reasoning_text(msg) == ""


class TestExtractResponseMeta:
    def test_full_meta(self):
        msg = SimpleNamespace(reasoning="想一下", content="回复",
                              provider_data={}, tool_calls=[])
        meta = extract_response_meta({
            "api_request_id": "r1", "provider": "deepseek", "model": "deepseek-v4",
            "base_url": "https://api.deepseek.com", "api_mode": "chat_completions",
            "api_call_count": 1, "api_duration": 1.25, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "output_tokens": 20},
        }, msg)
        assert meta["request_id"] == "r1"
        assert meta["provider"] == "deepseek"
        assert meta["reasoning_chars"] == 3
        assert meta["text_chars"] == 2
        assert meta["usage"]["output_tokens"] == 20
        assert meta["finish_kind"] == "stop"
        assert meta["duration_ms"] == 1250
