# -*- coding: utf-8 -*-
"""数据库连接与会话管理：引擎、会话工厂、声明基类、请求级会话依赖。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import (
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT,
    SQLALCHEMY_DATABASE_URL,
)

# 连接池预检测 + 定期回收，避免长连接被 MySQL 断开后报错；
# 连接/读写超时压短，MySQL 挂掉时后台请求快速失败，而不是每个页面卡十几秒
_engine_options = {
    "pool_recycle": DB_POOL_RECYCLE,
    "pool_pre_ping": True,
}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    _engine_options["connect_args"] = {"check_same_thread": False}
else:
    _engine_options.update(
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=min(DB_POOL_TIMEOUT, 5),
        connect_args={"connect_timeout": 3, "read_timeout": 20, "write_timeout": 20},
    )
engine = create_engine(SQLALCHEMY_DATABASE_URL, **_engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI 请求级数据库会话依赖"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
