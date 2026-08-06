"""cloud_llm 云端 LLM 调用层单测（任务书 40：flash 全链路重跑 pilot）。

覆盖:
  - load_api_key: 从 .env 读 DEEPSEEK_API_KEY（不硬编码，路径可注入）
  - call_cloud_llm: urllib 调用 deepseek-chat（OpenAI 兼容 /v1/chat/completions）
  - 错误处理: 超时/HTTP 错误 → None（不抛异常污染上层）
  - 宽松 JSON 解析: 围栏剥离 / 截断修复
  - 重试: 可配置重试次数
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

# ca/ 可导入
_ca_root = str(Path(__file__).resolve().parent.parent.parent)
if _ca_root not in sys.path:
    sys.path.insert(0, _ca_root)

import ca.cloud_llm as cloud_llm


class FakeResp:
    """模拟 urllib response（read() 返回 bytes，支持 with 上下文）。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_resp(text: str) -> FakeResp:
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": 123},
    }
    return FakeResp(json.dumps(body).encode())


class TestLoadApiKey(unittest.TestCase):
    def test_reads_key_from_env_file(self):
        env = Path("/tmp/__cloud_llm_test_env")
        env.write_text(
            "FOO=1\nDEEPSEEK_API_KEY=\"sk-test-123\"\nBAR=2\n", encoding="utf-8"
        )
        try:
            self.assertEqual(cloud_llm.load_api_key(env), "sk-test-123")
        finally:
            env.unlink(missing_ok=True)

    def test_missing_key_raises(self):
        env = Path("/tmp/__cloud_llm_test_env_missing")
        env.write_text("FOO=1\n", encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError):
                cloud_llm.load_api_key(env)
        finally:
            env.unlink(missing_ok=True)


class TestCallCloudLlm(unittest.TestCase):
    def test_returns_content(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=_ok_resp('{"ok": true}')
        ) as m:
            text = cloud_llm.call_cloud_llm(
                "hello", api_key="sk-test", max_tokens=128, temperature=0.2
            )
        self.assertEqual(text, '{"ok": true}')
        # 验证请求体
        req = m.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(body["model"], cloud_llm.MODEL)
        self.assertEqual(body["messages"][0]["content"], "hello")
        self.assertEqual(body["max_tokens"], 128)
        self.assertEqual(body["temperature"], 0.2)
        self.assertIn("Bearer sk-test", req.headers["Authorization"])

    def test_http_error_returns_none(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=Exception("HTTP Error 401: Unauthorized"),
        ):
            text = cloud_llm.call_cloud_llm("hi", api_key="bad")
        self.assertIsNone(text)

    def test_timeout_returns_none(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ):
            text = cloud_llm.call_cloud_llm("hi", api_key="bad")
        self.assertIsNone(text)

    def test_retries_on_failure(self):
        """失败重试：前两次异常，第三次成功。"""
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("boom")
            return _ok_resp("finally ok")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = cloud_llm.call_cloud_llm(
                "hi", api_key="k", max_retries=3
            )
        self.assertEqual(text, "finally ok")
        self.assertEqual(calls["n"], 3)

    def test_empty_response_returns_none(self):
        """200 但 choices 为空（异常响应）→ None。"""
        body = {"choices": []}
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResp(json.dumps(body).encode())
        ):
            text = cloud_llm.call_cloud_llm("hi", api_key="k")
        self.assertIsNone(text)


class TestLenientParse(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            cloud_llm.lenient_parse('{"a": 1}'), {"a": 1}
        )

    def test_fenced_json(self):
        self.assertEqual(
            cloud_llm.lenient_parse('```json\n{"a": 1}\n```'), {"a": 1}
        )

    def test_surrounding_text(self):
        self.assertEqual(
            cloud_llm.lenient_parse('思考过程...\n{"a": 1}\n后续杂音'), {"a": 1}
        )

    def test_truncated_json_returns_none(self):
        self.assertIsNone(cloud_llm.lenient_parse('{"a": 1,'))
        self.assertIsNone(cloud_llm.lenient_parse(""))
        self.assertIsNone(cloud_llm.lenient_parse(None))


class TestParseSid(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(cloud_llm.parse_sid("S7"), 7)
        self.assertEqual(cloud_llm.parse_sid("s7"), 7)
        self.assertEqual(cloud_llm.parse_sid("SS7"), 7)
        self.assertEqual(cloud_llm.parse_sid("R4"), 4)  # reality_id R 前缀
        self.assertEqual(cloud_llm.parse_sid(7), 7)
        self.assertEqual(cloud_llm.parse_sid("7"), 7)


if __name__ == "__main__":
    unittest.main()
