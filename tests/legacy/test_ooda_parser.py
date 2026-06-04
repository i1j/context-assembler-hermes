"""TC-C-005: 容错 JSON 解析成功率 ≥ 95%"""
import os
import pytest
from ca.post_process import robust_json_parse

MALFORMED_DIR = os.path.join(os.path.dirname(__file__), "data", "malformed_json")


@pytest.fixture(scope="module")
def malformed_samples():
    samples = []
    if not os.path.isdir(MALFORMED_DIR):
        return samples
    for fname in sorted(os.listdir(MALFORMED_DIR)):
        fpath = os.path.join(MALFORMED_DIR, fname)
        if os.path.isfile(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                samples.append(f.read())
    return samples


def test_robust_json_parse_success_rate(malformed_samples):
    """验证 robust_json_parse 能从畸形 JSON 恢复出有效 dict。

    成功率 = 能恢复出有效 dict 的样本比例。
    core_change 字段只在含固定 key 的 JSON 样本中才有，
    纯语法畸形样本（如缺括号）恢复后无 core_change 也视为成功。
    """
    assert len(malformed_samples) >= 100, f"需要至少 100 个样本，当前 {len(malformed_samples)}"
    success = 0
    for text in malformed_samples:
        parsed, _ = robust_json_parse(text)
        if isinstance(parsed, dict):
            success += 1
    rate = success / len(malformed_samples)
    assert rate >= 0.95, f"解析成功率 {rate:.1%} < 95%"
