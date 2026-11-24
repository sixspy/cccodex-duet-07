"""同业务键对齐：双边金额比对，落对齐差 + 悬账单。

悬账升格回正（邻题）接入点：
- 已升格且仍有效的键不参与自动对齐（旁路终态 / 影子待晋升）；
- 影子对边补齐后，在同一写事务内原子晋升回现行对齐；
- 悬账单只重算「挂起」行，终态（已升格/已回正）留档。
无升格记录时口径与基线完全一致。
"""
from __future__ import annotations
from app.db import 写事务, 连接
from app.seed import 此刻
from app.services import 目录


def _升格服务():
    from app.modules.悬账升格回正 import 服务
    return 服务


def 跑对齐() -> dict:
    规 = 目录.轨规()
    容差 = int(规.get("金额容差分", "0"))
    服务 = _升格服务()
    with 写事务() as conn:
        时刻 = 此刻()

        # 保留仍有效的升格差票
        升格行 = conn.execute(
            f"SELECT * FROM 对齐差 WHERE 差型 IN ('{服务.差型旁路}','{服务.差型影子}')"
        ).fetchall()
        升格键 = {
            r["业务键"]
            for r in conn.execute(
                "SELECT 业务键 FROM 悬账单 WHERE 状态='已升格'"
            ).fetchall()
        }

        # 挂起重算前先记下同键同边的起算时刻：反复跑对齐不能把悬龄清零，
        # 否则超时升格永远等不到。
        旧悬时刻 = {
            (r["业务键"], r["悬边"]): r["时刻"]
            for r in conn.execute(
                "SELECT 业务键,悬边,时刻 FROM 悬账单 WHERE 状态='挂起'"
            ).fetchall()
        }

        # 清空上次对齐结果（终态悬账与升格差票除外）
        conn.execute("DELETE FROM 对齐差")
        conn.execute("DELETE FROM 悬账单 WHERE 状态='挂起'")
        for r in 升格行:
            if r["业务键"] in 升格键:
                conn.execute(
                    "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                    (r["业务键"], r["左金额分"], r["右金额分"], r["差型"], r["说明"], r["时刻"]),
                )

        # 影子：对边已补齐的先原子晋升回现行对齐（同一事务；须在清空之后写）
        已晋升 = 服务.尝试晋升(conn, 容差=容差, 时刻=时刻)

        # 同键多笔：按业务键汇总金额（基线口径）
        左汇总: dict[str, int] = {}
        左代表: dict[str, str] = {}
        for r in conn.execute("SELECT * FROM 左轨流水 WHERE 命运='已入左'").fetchall():
            k = r["业务键"]
            左汇总[k] = 左汇总.get(k, 0) + int(r["金额分"])
            左代表[k] = r["事件编号"]

        右汇总: dict[str, int] = {}
        右代表: dict[str, str] = {}
        for r in conn.execute("SELECT * FROM 右轨流水 WHERE 命运='已入右'").fetchall():
            k = r["业务键"]
            右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
            右代表[k] = r["事件编号"]

        所有键 = set(左汇总) | set(右汇总)
        待对齐键 = 所有键 - 升格键 - {p["业务键"] for p in 已晋升}
        对齐 = 0
        差 = 0
        悬 = 0
        旁路跳过 = len(所有键 & 升格键)

        for k in sorted(待对齐键):
            有左 = k in 左汇总
            有右 = k in 右汇总
            if 有左 and 有右:
                la, ra = 左汇总[k], 右汇总[k]
                if abs(la - ra) <= 容差:
                    conn.execute(
                        "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                        (k, la, ra, "对齐", "双边金额在容差内", 时刻),
                    )
                    对齐 += 1
                else:
                    conn.execute(
                        "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                        (k, la, ra, "金额不符", f"左{la}-右{ra}", 时刻),
                    )
                    差 += 1
            elif 有左:
                conn.execute(
                    "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                    (k, 左汇总[k], None, "右缺", "仅左轨有键", 时刻),
                )
                conn.execute(
                    "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                    (k, "左", 左汇总[k], 左代表.get(k, ""), "右轨缺键", 旧悬时刻.get((k, "左"), 时刻)),
                )
                悬 += 1
                差 += 1
            else:
                conn.execute(
                    "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                    (k, None, 右汇总[k], "左缺", "仅右轨有键", 时刻),
                )
                conn.execute(
                    "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                    (k, "右", 右汇总[k], 右代表.get(k, ""), "左轨缺键", 旧悬时刻.get((k, "右"), 时刻)),
                )
                悬 += 1
                差 += 1

        return {
            "键总数": len(所有键),
            "对齐": 对齐,
            "差与悬": 差,
            "悬账": 悬,
            "容差分": 容差,
            "升格跳过": 旁路跳过,
            "影子晋升": len(已晋升),
            "晋升明细": 已晋升,
            "时刻": 时刻,
        }
