# -*- coding: utf-8 -*-
"""
数据同步脚本：从网络真实数据源抓取最新数据写入数据库（自动去重）
用法：
    python scripts/数据同步.py          # 更新全部
    python scripts/数据同步.py hot      # 只更新热榜
    python scripts/数据同步.py joke     # 只更新笑话
可选目标：hot热榜 / joke笑话 / wisdom名言 / image壁纸 / history历史今天
说明：无需启动API服务，直接连数据库写入；也可配合 Windows 计划任务定时执行
"""
import os
import sys

# 脚本位于 scripts/ 子目录，需要把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import SessionLocal, SYNC_FUNCS


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    db = SessionLocal()
    try:
        if target == "all":
            total = 0
            for name, fn in SYNC_FUNCS.items():
                n = fn(db)
                total += n
                print(f"[{name}] 新增 {n} 条")
            print(f"\n同步完成，共新增 {total} 条数据")
        else:
            fn = SYNC_FUNCS.get(target)
            if not fn:
                print(f"不支持的目标：{target}，可选：{list(SYNC_FUNCS.keys())} 或 all")
                return
            n = fn(db)
            print(f"[{target}] 新增 {n} 条")
    finally:
        db.close()


if __name__ == "__main__":
    main()
