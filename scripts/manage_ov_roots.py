#!/usr/bin/env python3
"""manage_ov_roots.py — ov_roots 手动治理接口（决策 44 续 R5）。

filters 仅手动修改（store set_ov_root_filters + 本 CLI set-filters）；LLM 不写 filters。

用法:
    HERMES_HOME=<profile> python3 scripts/manage_ov_roots.py --profile <p> set-filters <root_uri> <filters_json>
    HERMES_HOME=<profile> python3 scripts/manage_ov_roots.py --profile <p> list

--profile 安全校验对齐 reprocess_reality_pipeline.py L385-392：
env HERMES_HOME resolve == profile 目录，否则 exit 2。
set-filters 参数解析失败/校验失败 → 明确报错 exit 2。
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

PROFILES_ROOT = Path.home() / ".hermes" / "profiles"


def _validate_profile(profile: str) -> Path:
    """HERMES_HOME 安全校验：env HERMES_HOME resolve == profile 目录，否则 exit 2。"""
    profile_dir = PROFILES_ROOT / profile
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if not env_home or Path(env_home).resolve() != profile_dir.resolve():
        print(
            f"HERMES_HOME 未指向 {profile_dir}（当前={env_home!r}）。\n"
            f"  正确用法: HERMES_HOME={profile_dir} "
            f"python3 scripts/manage_ov_roots.py --profile {profile} ...",
            file=sys.stderr,
        )
        sys.exit(2)
    return profile_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ov_roots 手动治理（set-filters / list）")
    parser.add_argument("--profile", default="winker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set-filters",
                           help="设置根 filters（include/exclude 均 list[str]）")
    p_set.add_argument("root_uri")
    p_set.add_argument("filters_json")

    sub.add_parser("list", help="列出全部 ov_roots 行（JSON 逐行）")

    args = parser.parse_args()
    _validate_profile(args.profile)

    from ca.store import list_ov_roots, set_ov_root_filters

    if args.cmd == "set-filters":
        try:
            filters = json.loads(args.filters_json)
        except json.JSONDecodeError as exc:
            print(f"filters JSON 解析失败: {exc}", file=sys.stderr)
            sys.exit(2)
        if not set_ov_root_filters(args.root_uri, filters):
            print(
                f"set-filters 校验失败（root_uri 不在 ov_roots 表或 filters 非法）: "
                f"{args.root_uri}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"OK: {args.root_uri} "
              f"filters={json.dumps(filters, ensure_ascii=False)}")
        return 0

    if args.cmd == "list":
        for row in list_ov_roots():
            print(json.dumps(row, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    main()
