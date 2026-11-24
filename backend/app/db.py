import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "对拍.db"

# 同进程写路径互斥（对齐 / 升格扫描 / 影子晋升 / 补齐 / 人工回正）。
# 跨进程另有 SQLite BEGIN IMMEDIATE 写锁兜底。
_写锁 = threading.RLock()


def 连接() -> sqlite3.Connection:
    数据目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(库路径, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # 写事务排队：升格扫描 / 对齐 / 装载并发时不要立刻报 database is locked
    c.execute("PRAGMA busy_timeout = 10000")
    return c


@contextmanager
def 写事务():
    """显式写事务：进锁 → BEGIN IMMEDIATE → 成功 COMMIT / 异常 ROLLBACK。

    读-判-写必须在同一事务内完成，否则升格与人工回正会双成功。
    """
    with _写锁:
        数据目录.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(库路径, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 10000")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            c.close()
