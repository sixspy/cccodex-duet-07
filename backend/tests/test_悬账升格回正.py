"""悬账升格回正：超时扫描 / 旁路终态 / 影子晋升 / 并发不双落。"""
import os
import threading
from pathlib import Path

# 与基线测试共用同一临时库目录：app.db 在导入期定死 数据目录，
# 两个测试模块各自另起目录会互相串库。
_tmp = Path(__file__).resolve().parent / "_tmp"
_tmp.mkdir(exist_ok=True)
os.environ["数据目录"] = str(_tmp)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed
from app.db import 库路径
from app.services import 左装载, 右装载, 对齐, 结论, 目录
from app.modules import 悬账升格回正 as 升格


def _清库():
    for p in (库路径, Path(str(库路径) + "-journal"), Path(str(库路径) + "-wal"), Path(str(库路径) + "-shm")):
        if p.exists():
            p.unlink()
    seed.初始化库()


def setup_function():
    _清库()


def _造悬账():
    左装载.装一批({
        "事件": [
            {"事件编号": "L1", "业务键": "K1", "金额分": 100, "源键": "渠甲", "事件时": "2026-11-23T10:00:00Z"},
            {"事件编号": "L2", "业务键": "K2", "金额分": 200, "源键": "渠甲", "事件时": "2026-11-23T10:00:00Z"},
        ]
    })
    右装载.装一批({
        "事件": [
            {"事件编号": "R3", "业务键": "K3", "金额分": 50, "源键": "核乙", "事件时": "2026-11-23T10:00:00Z"},
        ]
    })
    对齐.跑对齐()


def test_升格空桩已落地且枚举可见():
    assert 升格.NAME == "悬账升格回正"
    from app.routers.核心 import 路由
    路 = {getattr(r, "path", "") for r in 路由.routes}
    assert "/api/悬账升格" in 路
    assert "/api/悬账升格/扫描" in 路
    assert "/api/悬账升格/读" in 路


def test_升格只读接口区分可对齐与不可对齐():
    _造悬账()
    r = 升格.读升格("K1")
    assert r["状态"] == "挂起"
    assert r["可自动对齐"] is True
    assert r["悬龄秒"] >= 0

    升格.扫描({"超时秒": 0, "形态": "旁路"})
    r = 升格.读升格("K1")
    assert r["状态"] == "已升格"
    assert r["升格形态"] == "旁路"
    assert r["旁路键"].startswith("旁路-")
    assert r["valid"] is True
    assert r["可自动对齐"] is False


def test_旁路终态补齐对边也不再自动对齐():
    _造悬账()
    升格.扫描({"超时秒": 0, "形态映射": {"K1": "旁路", "K2": "影子", "K3": "影子"}})
    # 旁路键补齐对边后，跑对齐仍不参与
    r = 升格.补齐对边({"业务键": "K1", "边": "右", "金额分": 100})
    assert r["晋升"] is False
    assert "旁路" in r["命运"]
    统计 = 对齐.跑对齐()
    assert 统计["升格跳过"] >= 1
    末 = 升格.读升格("K1")
    assert 末["可自动对齐"] is False
    assert 末["状态"] == "已升格"
    差 = [x for x in 目录.差列表() if x["业务键"] == "K1"][0]
    assert 差["差型"] == "升格旁路"


def test_影子补齐后原子晋升回现行对齐():
    _造悬账()
    升格.扫描({"超时秒": 0, "形态": "影子"})
    assert 升格.读升格("K2")["可晋升"] is False

    r = 升格.补齐对边({"业务键": "K2", "边": "右", "金额分": 200})
    assert r["晋升"] is True
    assert r["命运"] == "影子已晋升回现行对齐"

    末 = 升格.读升格("K2")
    assert 末["状态"] == "已回正"
    assert 末["valid"] is False  # 影子已失效
    assert 末["可自动对齐"] is True
    影 = [x for x in 目录.悬列表() if x["业务键"] == "K2"][0]
    assert 影["状态"] == "已回正"
    差 = [x for x in 目录.差列表() if x["业务键"] == "K2"][0]
    assert 差["差型"] == "对齐"


def test_超时与未超时区分():
    _造悬账()
    r = 升格.扫描({"超时秒": 3600, "形态": "旁路"})
    assert r["升格"] == 0
    assert r["未超时"] == 3
    assert all(x["状态"] == "挂起" for x in 目录.悬列表())
    r = 升格.扫描({"超时秒": 0, "形态": "旁路"})
    assert r["升格"] == 3


def test_悬账单与对齐差同票():
    _造悬账()
    升格.扫描({"超时秒": 0, "形态": "旁路"})
    for 悬 in 目录.悬列表():
        if 悬["状态"] != "已升格":
            continue
        票 = [x for x in 目录.差列表() if x["业务键"] == 悬["业务键"]]
        assert len(票) == 1
        assert 票[0]["差型"] in ("升格旁路", "升格影子")


def test_重跑对齐不清零悬龄():
    _造悬账()
    from app.db import 连接
    conn = 连接()
    conn.execute("UPDATE 悬账单 SET 时刻='2026-11-23T00:00:00Z' WHERE 业务键='K1'")
    conn.commit()
    conn.close()
    for _ in range(3):
        对齐.跑对齐()
    悬 = [x for x in 目录.悬列表() if x["业务键"] == "K1"][0]
    assert 悬["时刻"] == "2026-11-23T00:00:00Z"
    assert 升格.读升格("K1")["是否超时"] is True
    assert 升格.扫描({"形态": "旁路"})["升格"] >= 1


def test_升格与人工回正并发只落一边():
    坏 = 0
    for _ in range(8):
        _清库()
        _造悬账()
        结果 = {}

        def 扫():
            结果["扫"] = 升格.扫描({"超时秒": 0, "形态": "旁路"})

        def 回():
            结果["回"] = 升格.人工回正({"业务键": "K1", "金额分": 100})

        t1 = threading.Thread(target=扫)
        t2 = threading.Thread(target=回)
        t1.start(); t2.start(); t1.join(); t2.join()
        if (结果["扫"]["升格"] == 1) == (结果["回"]["回正"] is True):
            坏 += 1
        assert 升格.读升格("K1")["状态"] in ("已升格", "已回正")
    assert 坏 == 0


def test_升格与补齐并发不双落():
    坏 = 0
    for _ in range(8):
        _清库()
        _造悬账()
        结果 = {}

        def 扫():
            结果["扫"] = 升格.扫描({"超时秒": 0, "形态": "影子"})

        def 补():
            结果["补"] = 升格.补齐对边({"业务键": "K1", "边": "右", "金额分": 100})

        t1 = threading.Thread(target=扫)
        t2 = threading.Thread(target=补)
        t1.start(); t2.start(); t1.join(); t2.join()
        # 升格先落：补齐晋升；补齐先落：扫描见对边齐全不升格
        if 结果["扫"]["升格"] == 1 and 结果["补"]["晋升"]:
            坏 += 1
        assert 升格.读升格("K1")["状态"] in ("已升格", "已回正", "挂起")
    assert 坏 == 0


def test_重复扫描幂等():
    _造悬账()
    assert 升格.扫描({"超时秒": 0, "形态": "旁路"})["升格"] == 3
    assert 升格.扫描({"超时秒": 0, "形态": "旁路"})["升格"] == 0
    assert len([x for x in 目录.悬列表() if x["状态"] == "已升格"]) == 3


def test_结论仍自洽():
    _造悬账()
    升格.扫描({"超时秒": 0, "形态": "影子"})
    升格.补齐对边({"业务键": "K2", "边": "右", "金额分": 200})
    g = 结论.跑一次()
    assert g["是否通过"] is True, g
