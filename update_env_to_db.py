#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
将 .env 文件中的键值写入数据库 settings 表的小工具。

用法：
  python update_env_to_db.py                  # 读取项目根目录下 .env 中的所有键，写入数据库
  python update_env_to_db.py --path .env.dev  # 指定 .env 路径
  python update_env_to_db.py --prefix FEISHU_ # 仅同步以 FEISHU_ 开头的键
  python update_env_to_db.py --dry-run        # 仅预览，不写入

说明：
- 数据库由应用内置的 SQLAlchemy 模型管理，脚本会调用 app.services.config_service.update_setting。
- 只同步 .env 文件中“显式存在且非空”的键。
"""

import argparse
import sys
from typing import Dict

from dotenv import dotenv_values


def _load_env_file(path: str) -> Dict[str, str]:
    data = dotenv_values(path)  # 仅解析指定文件，不污染进程环境
    # 过滤 None/空字符串
    return {k: v for k, v in (data or {}).items() if v is not None and str(v) != ""}


def main():
    parser = argparse.ArgumentParser(description="Sync .env keys into database settings")
    parser.add_argument("--path", default=".env", help=".env 文件路径，默认 .env")
    parser.add_argument("--prefix", default="", help="只同步指定前缀的键，如 FEISHU_；留空表示同步所有键")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    args = parser.parse_args()

    # 读取 .env
    env_map = _load_env_file(args.path)
    if args.prefix:
        env_map = {k: v for k, v in env_map.items() if k.startswith(args.prefix)}

    if not env_map:
        print("未从 .env 读取到可同步的键；请检查路径/前缀/内容。")
        sys.exit(0)

    # 预览
    print("将同步以下键到数据库：")
    for k in sorted(env_map.keys()):
        mask = "********" if ("SECRET" in k.upper() or "CODE" in k.upper() or "API_KEY" in k.upper() or "TOKEN" in k.upper()) else env_map[k]
        print(f" - {k} = {mask}")

    if args.dry_run:
        print("(dry-run) 预览完成，未写入数据库。")
        sys.exit(0)

    # 写入数据库
    from app.services.config_service import update_setting

    updated = 0
    for k, v in env_map.items():
        try:
            if update_setting(k, v):
                updated += 1
        except Exception as e:
            print(f"写入失败：{k}: {e}")

    print(f"✅ 同步完成，更新了 {updated} 个键。")


if __name__ == "__main__":
    main()


