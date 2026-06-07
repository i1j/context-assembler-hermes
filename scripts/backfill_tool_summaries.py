"""
CA 旧数据回填脚本
用途：对 DB 中所有 raw-JSON 格式的工具轮 L0 重新运行 ToolSummarizer，更新为结构化摘要
触发条件：修复 arguments JSON→dict 适配层后，执行一次
"""

import sqlite3
import json
import re
import sys
import os
from pathlib import Path

CA_CACHE_DIR = os.path.expanduser(
    "~/.hermes/profiles/tester/ca_cache"
)

# 确保 ca 包可导入
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))  # scripts/
_PLUGIN_DIR = os.path.dirname(_PLUGIN_DIR)  # ca_assembler/
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from ca.tool_summarizer import ToolSummarizer


def is_raw_json_l0(l0_text, tool_name):
    """判断一条 L0 是否是 raw JSON 格式（需要回填）"""
    if not l0_text:
        return True
    prefix = f"{tool_name}: "
    if l0_text.startswith(prefix):
        body = l0_text[len(prefix):]
        if body.startswith("{"):
            return True  # raw JSON
        if body in ("失败", "无返回数据", ""):
            return True  # 来自回退路径
        return False
    return True


def backfill_db(db_path):
    print(f"\n=== {Path(db_path).name} ===")
    
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    # 检测表结构——旧版 DB 没有 tool_sub_index 列
    cur.execute("PRAGMA table_info(turn_cache)")
    cols = {row[1] for row in cur.fetchall()}
    has_sub_index = "tool_sub_index" in cols
    has_l2_text = "l2_text" in cols
    
    if not has_l2_text:
        print("  no l2_text column (old schema)")
        conn.close()
        return
    
    if has_sub_index:
        cur.execute("""
            SELECT rowid, turn_index, tool_sub_index, l2_text, l0_text, l1_text
            FROM turn_cache
            WHERE turn_type='tool'
            ORDER BY turn_index, tool_sub_index
        """)
    else:
        cur.execute("""
            SELECT rowid, turn_index, 0 as tool_sub_index, l2_text, l0_text, l1_text
            FROM turn_cache
            WHERE turn_type='tool'
            ORDER BY turn_index
        """)
    rows = cur.fetchall()
    
    if not rows:
        print("  no tool turns")
        conn.close()
        return
    
    summarizer = ToolSummarizer()
    
    fixed = 0
    skipped_struct = 0
    skipped_no_change = 0
    for rowid, turn_idx, sub_idx, l2_text, old_l0, old_l1 in rows:
        try:
            msgs = json.loads(l2_text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msgs, list):
            continue
        
        tool_call = None
        tool_responses = []
        for msg in msgs:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    if tool_call is None:
                        tool_call = tc
            elif msg.get("role") == "tool":
                tool_responses.append(msg)
        
        if tool_call is None:
            continue
        
        tool_name = tool_call.get("function", {}).get("name", "")
        if not tool_name:
            continue
        
        if not is_raw_json_l0(old_l0, tool_name):
            skipped_struct += 1
            continue
        
        try:
            new_l1, new_l0 = summarizer.summarize(tool_call, tool_responses)
        except Exception as e:
            print(f"  SKIP turn={turn_idx}/{sub_idx} {tool_name}: {e}")
            continue
        
        if new_l0 == old_l0:
            skipped_no_change += 1
            continue
        
        new_l1_str = json.dumps(new_l1, ensure_ascii=False)
        cur.execute(
            "UPDATE turn_cache SET l0_text=?, l1_text=? WHERE rowid=?",
            (new_l0[:100], new_l1_str, rowid)
        )
        fixed += 1
    
    conn.commit()
    conn.close()
    print(f"  fixed={fixed} skipped_struct={skipped_struct} no_change={skipped_no_change}")


if __name__ == "__main__":
    # 全量回填所有 DB
    dbs = sorted(Path(CA_CACHE_DIR).glob("*.db"))
    dbs = [db for db in dbs if not db.name.endswith("-shm") and not db.name.endswith("-wal")]
    print(f"Found {len(dbs)} databases")
    for db_path in dbs:
        backfill_db(db_path)
