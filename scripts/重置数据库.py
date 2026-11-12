# -*- coding: utf-8 -*-
"""
重置数据库脚本：清空全部数据，恢复到刚部署完的“全新状态”
用法：python scripts/重置数据库.py RESET
说明：
  - 默认禁用：后台需先设置 ALLOW_DB_RESET=1 并重启；后台运行还需输入当前管理员密码
  - 必须带参数 RESET 才会执行，防止误触；删除后不可恢复
  - 清空范围：业务内容表、C 端用户与额度、接口 Token、全部配置、全部日志与监控记录
  - 执行后必须用环境变量里的 ADMIN_USER / ADMIN_PASSWORD 登录后台（内置管理员不受影响）
  - 执行后请重启项目：敏感词内置库、接口开关等会自动重建
  - 如需基础示例数据，再运行「数据初始化」脚本
"""
import os
import sys

# 脚本位于 scripts/ 子目录，需要把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIRM = "RESET"

# 全部数据表：业务内容 + 用户令牌 + 系统配置 + 日志监控（按用途分组，仅用于打印）
TABLE_GROUPS = [
    ("业务内容", [
        "knowledge", "retort", "name_lib", "local_joke", "hot_news", "daily_image",
        "idiom_lib", "poem_lib", "copywriting_lib", "word_wisdom", "love_talk",
        "poison_soup", "riddle", "xiehouyu", "tongue_twister", "answer_book",
        "history_today", "sensitive_quarantine",
    ]),
    ("C端用户与令牌", [
        "api_token", "portal_email_code", "portal_reward", "portal_quota_log", "c_user",
    ]),
    ("系统配置", [
        "app_setting", "api_switch", "ip_rule", "alert_rule", "sensitive_word",
        "admin_user", "admin_session", "script_schedule",
    ]),
    ("日志与监控", [
        "api_log", "login_log", "audit_log", "rate_limit_log", "alert_event",
        "breaker_event", "breaker_state", "pool_metric", "schedule_run",
        "script_run", "import_log", "import_job", "export_job",
    ]),
]


def reset():
    if len(sys.argv) < 2 or sys.argv[1] != CONFIRM:
        print("[中止] 这是危险操作：将清空全部数据并恢复到全新状态，且不可恢复")
        print(f"命令行用法：python scripts/重置数据库.py {CONFIRM}")
        print("后台用法：「脚本管理」→「重置数据库」→ 选择确认项并输入 RESET、管理员密码")
        sys.exit(1)

    from sqlalchemy import text
    from main import engine

    print("[警告] 开始清空全部数据，恢复到全新状态 ...")
    total = 0
    with engine.begin() as conn:
        for group, tables in TABLE_GROUPS:
            print(f"\n--- {group} ---")
            for table in tables:
                try:
                    result = conn.execute(text(f"DELETE FROM `{table}`"))
                    count = result.rowcount or 0
                except Exception as exc:
                    print(f"[跳过] {table}: {exc}")
                    continue
                total += count
                try:
                    conn.execute(text(f"ALTER TABLE `{table}` AUTO_INCREMENT = 1"))
                except Exception:
                    pass  # 非 MySQL 或无自增列时忽略
                print(f"[清空] {table}: 删除 {count} 条")
    print(f"\n重置完成：共删除 {total} 条记录，数据库已回到全新状态")
    print("后续操作：")
    print("  1) 重启项目（接口开关、敏感词内置库会自动重建）")
    print("  2) 用环境变量中的 ADMIN_USER / ADMIN_PASSWORD 登录后台")
    print("  3) 如需基础示例数据，运行「数据初始化」脚本")


if __name__ == "__main__":
    reset()
