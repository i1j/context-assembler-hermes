#!/usr/bin/env python3
"""
ContextAssembler 测试数据生成工具
存放位置: tests/data/generate.py
运行方式: python tests/data/generate.py
"""
import os, json, random

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MALFORMED_DIR = os.path.join(BASE_DIR, "malformed_json")
DIALOGUES_DIR = os.path.join(BASE_DIR, "dialogues")
REF_DIR = os.path.join(BASE_DIR, "reference_summaries", "v1.0")

NUM_SHORT, NUM_MEDIUM, NUM_LONG = 15, 20, 15
NUM_DIALOGUES = NUM_SHORT + NUM_MEDIUM + NUM_LONG

TEMPLATES = [
    ("你好，我想问一下这个产品怎么用？", "您好，产品使用说明在包装内，也可以查看官网视频教程。"),
    ("我今天收到货了，但是颜色和图片有点不一样。", "非常抱歉，您可以申请退换货，我们会尽快处理。"),
    ("请问可以开发票吗？", "可以的，电子发票会在确认收货后自动发送。"),
    ("我的订单状态怎么还是待发货？", "通常会在24小时内发货，请耐心等待。"),
    ("这个套餐和另一个有什么区别？", "主要区别在于流量额度和通话时长。"),
    ("能帮我取消订单吗？", "可以的，请问需要取消的订单号是多少？"),
    ("我刚刚付了款，但是显示未支付。", "可能是网络延迟，稍后会自动更新。"),
    ("有没有优惠券可以领？", "目前有满199减30的活动。"),
    ("你们的售后政策是怎样的？", "支持7天无理由退货，一年质保。"),
    ("我要投诉快递员态度很差。", "非常抱歉，我们会反馈给相关部门处理。"),
]

SUMMARY_PREFIX = "用户咨询了"
SUMMARY_SUFFIX = "等问题，客服进行了相应回复。"


def generate_malformed_samples(count=120):
    samples = []
    for i in range(15):
        s = '{"key": "value"'; samples.append(s + "," if i % 2 else s)
    for i in range(10):
        s = '"key": "value"}'; samples.append('key": "value"}' if i % 2 else s)
    for i in range(10):
        s = '{"key": "value",}'; samples.append('{"key": "value", "list": [1,2,]}' if i % 3 == 0 else s)
    for i in range(10):
        s = '{"key": "value'; samples.append('{"key": "value, "other": "test"}' if i % 2 else s)
    for i in range(10):
        s = '{key: "value"}'; samples.append('{key: "value", "other": test}' if i % 2 else s)
    for i in range(10):
        s = "{'key': 'value'}"; samples.append("{'key': 'value', 'list': [1,2]}" if i % 2 else s)
    for i in range(10):
        s = '{"key": [1, 2'; samples.append('{"key": [1, 2, "missing]"}}' if i % 2 else s)
    for i in range(10):
        s = '{"key": "value"} extra'; samples.append('{"key": "value"}]' if i % 2 else s)
    for i in range(10):
        s = '{"key": 12.34.56}'; samples.append('{"key": 12e}' if i % 2 else s)
    for i in range(10):
        s = '{"outer": {"inner": "value"'; samples.append('{"outer": {"inner": "value"}, "missing"}' if i % 2 else s)
    for i in range(5):
        samples.append("" if i == 0 else ("   " if i == 1 else "\n"))
    for i in range(10):
        s = "核心摘要：测试\n资源与观察：\n- 资源1\n事实与约束："
        samples.append(s if i % 2 else s + "\n- 截断")
    return samples


def generate_simulated_data(num=50):
    dialogues, summaries = [], []
    for _ in range(num):
        rounds = random.randint(2, 8)
        turns = []
        for _ in range(rounds):
            u, a = random.choice(TEMPLATES)
            turns.append(f"用户：{u}")
            turns.append(f"客服：{a}")
        dialogue_text = "\n".join(turns)
        topics = random.sample(["产品使用", "订单状态", "发票", "投诉", "退换货", "优惠政策", "售后服务"], k=min(3, rounds))
        summary = SUMMARY_PREFIX + "、".join(topics) + SUMMARY_SUFFIX
        dialogues.append(dialogue_text)
        summaries.append(summary)
    return dialogues, summaries


def write_dialogues(dialogues):
    short = [d for d in dialogues if len(d) < 500]
    medium = [d for d in dialogues if 500 <= len(d) < 1500]
    long = [d for d in dialogues if len(d) >= 1500]
    while len(short) < NUM_SHORT: short.append("占位短对话。")
    while len(medium) < NUM_MEDIUM: medium.append("占位中对话。")
    while len(long) < NUM_LONG: long.append("占位长对话。")
    short, medium, long = short[:NUM_SHORT], medium[:NUM_MEDIUM], long[:NUM_LONG]
    with open(os.path.join(DIALOGUES_DIR, "short_dialogues.json"), "w", encoding="utf-8") as f:
        json.dump(short, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DIALOGUES_DIR, "medium_dialogues.json"), "w", encoding="utf-8") as f:
        json.dump(medium, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DIALOGUES_DIR, "long_dialogues.json"), "w", encoding="utf-8") as f:
        json.dump(long, f, ensure_ascii=False, indent=2)
    print(f"对话写入: short={len(short)}, medium={len(medium)}, long={len(long)}")


def write_references(summaries):
    summaries = summaries[:NUM_DIALOGUES]
    while len(summaries) < NUM_DIALOGUES:
        summaries.append("占位摘要。")
    for i, text in enumerate(summaries, 1):
        with open(os.path.join(REF_DIR, f"summary_{i:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    print(f"参考摘要写入: {len(summaries)} 个")


def main():
    os.makedirs(MALFORMED_DIR, exist_ok=True)
    os.makedirs(DIALOGUES_DIR, exist_ok=True)
    os.makedirs(REF_DIR, exist_ok=True)

    samples = generate_malformed_samples(120)
    for i, text in enumerate(samples):
        with open(os.path.join(MALFORMED_DIR, f"malformed_{i:03d}.json"), "w", encoding="utf-8") as f:
            f.write(text)
    print(f"畸形JSON: {len(samples)} 个")

    dialogues, summaries = generate_simulated_data(NUM_DIALOGUES)
    write_dialogues(dialogues)
    write_references(summaries)
    print("✅ 测试数据生成完成。")


if __name__ == "__main__":
    main()
