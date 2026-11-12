# -*- coding: utf-8 -*-
"""
数据清理脚本：清除业务表中的空白、空格污染和格式异常数据
用法：python scripts/数据清理.py
规则：
  1) 所有文本字段去除首尾空白字符（空格、制表符、换行）
  2) 删除关键字段为 NULL 或去除空白后为空的整条记录
  3) 专项格式校验：图片地址必须 http 开头、历史日期必须 MM-DD、
     取名分类必须 boy/girl/net/shop、怼人级别必须 mild/sharp/funny
说明：只处理 17 张业务内容表，不涉及账号、配置、日志等系统表；可重复运行
"""
import os
import re
import sys

# 脚本位于 scripts/ 子目录，需要把项目根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import String, Text, func, or_, text as sa_text
from main import (
    SessionLocal,
    KnowledgeDB, RetortDB, NameDB, LocalJokeDB, HotNewsDB, DailyImageDB,
    IdiomDB, PoemDB, CopywritingDB, WordWisdomDB, LoveTalkDB, PoisonSoupDB,
    RiddleDB, XiehouyuDB, TwisterDB, AnswerBookDB, HistoryTodayDB,
)

# (名称, 模型, 关键字段) —— 关键字段为 NULL 或去空白后为空则删除整条记录
TABLES = [
    ("knowledge", KnowledgeDB, ("category", "title", "content")),
    ("retort", RetortDB, ("content",)),
    ("name", NameDB, ("type", "text")),
    ("joke", LocalJokeDB, ("content",)),
    ("hot", HotNewsDB, ("title",)),
    ("image", DailyImageDB, ("img_url",)),
    ("idiom", IdiomDB, ("word",)),
    ("poem", PoemDB, ("title", "content")),
    ("copy", CopywritingDB, ("content",)),
    ("wisdom", WordWisdomDB, ("content",)),
    ("love", LoveTalkDB, ("content",)),
    ("poison", PoisonSoupDB, ("content",)),
    ("riddle", RiddleDB, ("question", "answer")),
    ("xiehouyu", XiehouyuDB, ("riddle", "answer")),
    ("twister", TwisterDB, ("content",)),
    ("answer", AnswerBookDB, ("content",)),
    ("history", HistoryTodayDB, ("month_day", "event")),
]

DATE_RE = re.compile(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")


def _trim_string_columns(db, model):
    """用一条 UPDATE 语句批量去除文本字段首尾空白，返回修正的字段数"""
    fixed = 0
    for col in model.__table__.columns:
        if not isinstance(col.type, (String, Text)):
            continue
        result = db.execute(
            sa_text(f"UPDATE `{model.__tablename__}` SET `{col.name}` = TRIM(`{col.name}`) "
                    f"WHERE `{col.name}` IS NOT NULL AND `{col.name}` <> TRIM(`{col.name}`)"))
        fixed += result.rowcount or 0
    return fixed


def _delete_blank_rows(db, model, key_fields):
    """删除关键字段为 NULL 或空白字符串的记录，返回删除条数"""
    conds = []
    for field in key_fields:
        col = getattr(model, field)
        conds.append(col.is_(None))
        conds.append(func.trim(col) == "")
    return db.query(model).filter(or_(*conds)).delete(synchronize_session=False)


def _delete_bad_format(db, name, model):
    """表级专项格式校验，返回删除条数"""
    if name == "image":
        return db.query(model).filter(
            or_(model.img_url.is_(None),
                ~func.trim(model.img_url).like("http%"))
        ).delete(synchronize_session=False)
    if name == "history":
        # MySQL REGEXP；非 MySQL 环境回退为 Python 侧校验
        try:
            return db.query(model).filter(
                model.month_day.op("NOT REGEXP")("^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
            ).delete(synchronize_session=False)
        except Exception:
            ids = [row.id for row in db.query(model.id, model.month_day).all()
                   if not DATE_RE.fullmatch(str(row.month_day or "").strip())]
            if not ids:
                return 0
            return db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
    if name == "name":
        return db.query(model).filter(
            ~model.type.in_(("boy", "girl", "net", "shop"))
        ).delete(synchronize_session=False)
    if name == "retort":
        return db.query(model).filter(
            ~model.level.in_(("mild", "sharp", "funny"))
        ).delete(synchronize_session=False)
    return 0


def clean():
    db = SessionLocal()
    try:
        total_trim = total_del = 0
        for name, model, keys in TABLES:
            before = db.query(model).count()
            trimmed = _trim_string_columns(db, model)
            blank = _delete_blank_rows(db, model, keys)
            bad = _delete_bad_format(db, name, model)
            db.commit()
            deleted = before - db.query(model).count()
            total_trim += trimmed
            total_del += deleted
            if trimmed or deleted:
                print(f"[清理] {name}: 修正空白 {trimmed} 处，"
                      f"删除异常 {deleted} 条（空白字段 {blank} / 格式 {bad}），"
                      f"剩余 {before - deleted} 条")
            else:
                print(f"[干净] {name}: 无需处理，共 {before} 条")
        print(f"\n清理完成：共修正空白 {total_trim} 处，删除异常数据 {total_del} 条")
    finally:
        db.close()


if __name__ == "__main__":
    clean()
