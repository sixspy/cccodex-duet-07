"""悬账超时升格回正：旁路账册终态 / 影子草稿待回正。

独立主链路：不改基线双轨对齐 happy path 的默认口径。基线只在对齐重算时
跳过已升格键（旁路不再自动对齐，影子待补齐晋升），其余键照旧。

并发口径（题面硬约束）：
- 所有升格写路径共用一把写锁 + SQLite `BEGIN IMMEDIATE`，读-判-写不裸露；
- 悬账单状态用 CAS（`WHERE 状态='挂起'`）迁跃，升格与人工回正只有一方落地；
- 升格记录与对齐差行在同一事务内写，扫描改悬写差同成败。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.db import 连接, 写事务
from app.seed import 此刻
from app.services import 目录
from app.services.时间 import 解析

# 形态常量
形态旁路 = "旁路"
形态影子 = "影子"
形态人工 = "人工"
形态集 = (形态旁路, 形态影子)

# 对齐差差型常量
差型旁路 = "升格旁路"
差型影子 = "升格影子"
升格差型组 = (差型旁路, 差型影子)

# 悬账单状态
状态挂起 = "挂起"
状态已升格 = "已升格"
状态已回正 = "已回正"

默认超时秒 = 60
默认形态 = 形态影子

_超时键候选 = ("悬账超时秒", "悬账超时轨规秒", "超时秒")
_形态键候选 = ("悬账升格形态", "升格形态")


def _整数(v, 默认: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 默认


def 读取规则() -> dict:
    规 = 目录.轨规()
    超时 = None
    for k in _超时键候选:
        if k in 规 and str(规[k]).strip() != "":
            超时 = _整数(规[k], 默认超时秒)
            break
    形态 = 默认形态
    for k in _形态键候选:
        v = str(规.get(k, "")).strip()
        if v in 形态集:
            形态 = v
            break
    return {"超时秒": 默认超时秒 if 超时 is None else max(0, 超时), "默认形态": 形态}


def 悬龄秒(时刻: str, 现: datetime | None = None) -> int:
    """悬龄按秒计；时刻不可解析时按 0（不误判超时）。"""
    try:
        t = 解析(str(时刻))
    except Exception:
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    现 = 现 or datetime.now(timezone.utc)
    return max(0, int((现 - t).total_seconds()))


def _汇总(conn) -> tuple[dict, dict, dict, dict]:
    左汇总: dict[str, int] = {}
    左代表: dict[str, str] = {}
    右汇总: dict[str, int] = {}
    右代表: dict[str, str] = {}
    for r in conn.execute("SELECT * FROM 左轨流水 WHERE 命运='已入左'").fetchall():
        k = r["业务键"]
        左汇总[k] = 左汇总.get(k, 0) + int(r["金额分"])
        左代表[k] = r["事件编号"]
    for r in conn.execute("SELECT * FROM 右轨流水 WHERE 命运='已入右'").fetchall():
        k = r["业务键"]
        右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
        右代表[k] = r["事件编号"]
    return 左汇总, 左代表, 右汇总, 右代表


def _写差(conn, 业务键: str, 左金额分, 右金额分, 差型: str, 说明: str, 时刻: str) -> None:
    """同键只维护一张现行对齐差票；悬账单与对齐差同票。"""
    行 = conn.execute(
        "SELECT 编号 FROM 对齐差 WHERE 业务键=? ORDER BY 编号 DESC LIMIT 1", (业务键,)
    ).fetchone()
    if 行:
        conn.execute(
            "UPDATE 对齐差 SET 左金额分=?,右金额分=?,差型=?,说明=?,时刻=? WHERE 编号=?",
            (左金额分, 右金额分, 差型, 说明, 时刻, 行["编号"]),
        )
    else:
        conn.execute(
            "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
            (业务键, 左金额分, 右金额分, 差型, 说明, 时刻),
        )


def 尝试晋升(conn, 容差: int = 0, 时刻: str | None = None, 业务键: str | None = None) -> list[dict]:
    """影子草稿：补齐对边后原子晋升回现行对齐。

    调用方必须已处于写事务（`写事务()` / BEGIN IMMEDIATE）内。
    逐行 CAS：悬账单 已升格→已回正、影子草稿 有效→失效、对齐差改现行票，
    三者同事务；任一 CAS 失配抛错回滚，避免与升格/人工回正双落。
    """
    时刻 = 时刻 or 此刻()
    左汇总, 左代表, 右汇总, 右代表 = _汇总(conn)
    sql = "SELECT * FROM 悬账单 WHERE 状态='已升格' AND 升格形态='影子'"
    参数: tuple = ()
    if 业务键 is not None:
        sql += " AND 业务键=?"
        参数 = (业务键,)
    已办: list[dict] = []
    for r in conn.execute(sql + " ORDER BY 编号", 参数).fetchall():
        k = r["业务键"]
        if k not in 左汇总 or k not in 右汇总:
            continue
        n = conn.execute(
            "UPDATE 悬账单 SET 状态='已回正', 升格时刻=? "
            "WHERE 编号=? AND 状态='已升格' AND 升格形态='影子'",
            (时刻, r["编号"]),
        )
        if n.rowcount != 1:
            continue
        n2 = conn.execute(
            "UPDATE 影子草稿 SET 有效=0, 晋升时刻=? WHERE 影子键=? AND 有效=1",
            (时刻, r["升格键"]),
        )
        if n2.rowcount != 1:
            raise RuntimeError(f"影子晋升 CAS 失配:{r['升格键']}")
        la, ra = 左汇总[k], 右汇总[k]
        差型 = "对齐" if abs(la - ra) <= 容差 else "金额不符"
        _写差(conn, k, la, ra, 差型, f"影子补齐晋升回现行对齐({差型})", 时刻)
        已办.append(
            {
                "业务键": k,
                "影子键": r["升格键"],
                "差型": 差型,
                "左金额分": la,
                "右金额分": ra,
                "晋升时刻": 时刻,
            }
        )
    return 已办


def _最近旁路(conn, 业务键: str):
    return conn.execute(
        "SELECT * FROM 升格旁路账册 WHERE 业务键=? ORDER BY 有效 DESC, 升格时刻 DESC, 旁路键 LIMIT 1",
        (业务键,),
    ).fetchone()


def _最近影子(conn, 业务键: str):
    return conn.execute(
        "SELECT * FROM 影子草稿 WHERE 业务键=? ORDER BY 有效 DESC, 升格时刻 DESC, 影子键 LIMIT 1",
        (业务键,),
    ).fetchone()


def _现行悬账(conn, 业务键: str):
    return conn.execute(
        "SELECT * FROM 悬账单 WHERE 业务键=? ORDER BY 编号 DESC LIMIT 1", (业务键,)
    ).fetchone()


def 读升格(业务键: str, 超时秒: int | None = None) -> dict:
    """升格只读接口：业务键、悬龄秒、旁路/影子键、valid，并区分是否仍可对齐。

    - 挂起未升格：可自动对齐=True；
    - 旁路：可自动对齐=False，终态，即使补齐对边也不再自动对齐；
    - 影子：可自动对齐=False、可晋升=对边是否已补齐；晋升后 已回正 恢复可对齐。
    """
    规则 = 读取规则()
    阈值 = 规则["超时秒"] if 超时秒 is None else max(0, int(超时秒))
    conn = 连接()
    try:
        悬 = _现行悬账(conn, 业务键)
        旁 = _最近旁路(conn, 业务键)
        影 = _最近影子(conn, 业务键)
        差 = conn.execute(
            "SELECT * FROM 对齐差 WHERE 业务键=? ORDER BY 编号 DESC LIMIT 1", (业务键,)
        ).fetchone()
        左汇总, 左代表, 右汇总, 右代表 = _汇总(conn)
        两边齐 = 业务键 in 左汇总 and 业务键 in 右汇总

        状态 = 悬["状态"] if 悬 else (状态已回正 if (影 and 影["晋升时刻"]) else "无悬账")
        形态 = 悬["升格形态"] if (悬 and 悬["升格形态"]) else ""
        升格键 = 悬["升格键"] if 悬 else ""
        起算时刻 = ""
        if 悬:
            起算时刻 = 悬["时刻"]
        elif 影:
            起算时刻 = 影["悬账时刻"]
        elif 旁:
            起算时刻 = 旁["悬账时刻"]
        龄 = 悬龄秒(起算时刻) if 起算时刻 else 0

        effective = False
        if 悬 and 悬["状态"] == 状态已升格:
            if 形态 == 形态旁路 and 旁 and 旁["旁路键"] == 悬["升格键"] and int(旁["有效"]) == 1:
                effective = True
            elif 形态 == 形态影子 and 影 and 影["影子键"] == 悬["升格键"] and int(影["有效"]) == 1:
                effective = True

        旁路键 = 旁["旁路键"] if 旁 else ""
        影子键 = 影["影子键"] if 影 else ""
        可对齐 = True
        可晋升 = False
        原因 = ""
        if 状态 == 状态挂起:
            结论 = "未升格，仍走自动对齐"
            if 龄 >= 阈值:
                结论 = "已超时未升格，仍走自动对齐；可扫描升格"
        elif 状态 == 状态已升格 and 形态 == 形态旁路:
            可对齐 = False
            原因 = "旁路账册终态，不再参与自动对齐"
            结论 = "旁路终态：补齐对边也不再自动对齐"
        elif 状态 == 状态已升格 and 形态 == 形态影子:
            可对齐 = False
            可晋升 = 两边齐
            原因 = "影子草稿只读，待补齐对边后原子晋升"
            结论 = "影子可晋升（对边已补齐）" if 两边齐 else "影子待补齐对边晋升"
        elif 状态 == 状态已回正:
            结论 = "已回正，恢复现行自动对齐"
        elif 状态 == "无悬账":
            结论 = "无现行悬账"

        return {
            "业务键": 业务键,
            "悬龄秒": 龄,
            "超时秒": 阈值,
            "是否超时": bool(龄 >= 阈值) if 起算时刻 else False,
            "状态": 状态,
            "升格形态": 形态,
            "旁路键": 旁路键,
            "影子键": 影子键,
            "升格键": 升格键,
            "valid": bool(effective),
            "升格有效": bool(effective),
            "可自动对齐": bool(可对齐),
            "仍可对齐": bool(可对齐),
            "可晋升": bool(可晋升),
            "对边齐全": bool(两边齐),
            "不可对齐原因": 原因,
            "结论": 结论,
            "悬账单": dict(悬) if 悬 else None,
            "对齐差": dict(差) if 差 else None,
            "旁路账册": dict(旁) if 旁 else None,
            "影子草稿": dict(影) if 影 else None,
        }
    finally:
        conn.close()


def 策略卡片() -> dict:
    规则 = 读取规则()
    conn = 连接()
    try:
        挂起 = conn.execute("SELECT COUNT(*) AS c FROM 悬账单 WHERE 状态='挂起'").fetchone()["c"]
        升格 = conn.execute("SELECT COUNT(*) AS c FROM 悬账单 WHERE 状态='已升格'").fetchone()["c"]
        回正 = conn.execute("SELECT COUNT(*) AS c FROM 悬账单 WHERE 状态='已回正'").fetchone()["c"]
        旁 = conn.execute("SELECT COUNT(*) AS c FROM 升格旁路账册 WHERE 有效=1").fetchone()["c"]
        影 = conn.execute("SELECT COUNT(*) AS c FROM 影子草稿 WHERE 有效=1").fetchone()["c"]
        可晋升 = 0
        左汇总, _, 右汇总, _ = _汇总(conn)
        for r in conn.execute(
            "SELECT 业务键 FROM 悬账单 WHERE 状态='已升格' AND 升格形态='影子'"
        ).fetchall():
            k = r["业务键"]
            if k in 左汇总 and k in 右汇总:
                可晋升 += 1
        return {
            "超时秒": 规则["超时秒"],
            "默认形态": 规则["默认形态"],
            "形态候选": list(形态集),
            "悬账挂起": 挂起,
            "悬账已升格": 升格,
            "悬账已回正": 回正,
            "旁路有效": 旁,
            "影子有效": 影,
            "影子可晋升": 可晋升,
            "口径": "超时悬账择定形态：旁路=终态不再自动对齐；影子=只读待补齐晋升",
        }
    finally:
        conn.close()


def 列表(限制: int = 300) -> dict:
    规则 = 读取规则()
    conn = 连接()
    try:
        行 = []
        for r in conn.execute(
            "SELECT * FROM 悬账单 ORDER BY 编号 DESC LIMIT ?", (限制,)
        ).fetchall():
            d = dict(r)
            d["悬龄秒"] = 悬龄秒(r["时刻"])
            d["是否超时"] = d["悬龄秒"] >= 规则["超时秒"]
            d["valid"] = bool(
                r["状态"] == 状态已升格
                and (
                    (r["升格形态"] == 形态旁路 and int((_最近旁路(conn, r["业务键"]) or {"有效": 0})["有效"]) == 1)
                    or (r["升格形态"] == 形态影子 and int((_最近影子(conn, r["业务键"]) or {"有效": 0})["有效"]) == 1)
                )
            )
            d["可自动对齐"] = r["状态"] != 状态已升格
            d["不可对齐原因"] = (
                "旁路终态" if r["升格形态"] == 形态旁路 else "影子待晋升"
            ) if r["状态"] == 状态已升格 else ""
            行.append(d)
        旁路 = [dict(r) for r in conn.execute(
            "SELECT * FROM 升格旁路账册 ORDER BY 升格时刻 DESC, 旁路键 LIMIT ?", (限制,)
        ).fetchall()]
        影子 = [dict(r) for r in conn.execute(
            "SELECT * FROM 影子草稿 ORDER BY 升格时刻 DESC, 影子键 LIMIT ?", (限制,)
        ).fetchall()]
        return {"悬账": 行, "旁路账册": 旁路, "影子草稿": 影子, "策略": 策略卡片()}
    finally:
        conn.close()


def 扫描(体: dict | None = None) -> dict:
    """超时悬账扫描升格。改悬（状态 CAS）与写差（对齐差票）同事务、同成败。

    若扫描时对边已被补齐（并发补齐先落），本键不再升格——避免“升格后又对齐”；
    若升格先落，后续补齐只入账、不自动对齐（旁路），影子则原子晋升。
    """
    体 = 体 or {}
    规则 = 读取规则()
    超时 = _整数(体.get("超时秒"), 规则["超时秒"])
    超时 = max(0, 超时)
    默认形 = str(体.get("形态") or 规则["默认形态"]).strip()
    if 默认形 not in 形态集:
        raise ValueError(f"形态须为 {形态集}，收到:{默认形}")
    形态映射 = 体.get("形态映射") or {}
    仅键 = 体.get("业务键")
    若键集 = {仅键} if isinstance(仅键, str) and 仅键 else None
    容差 = _整数(目录.轨规().get("金额容差分", "0"), 0)

    结果 = {
        "扫描": 0,
        "升格": 0,
        "晋升": 0,
        "未超时": 0,
        "对边齐全": 0,
        "已升格跳过": 0,
        "抢跑未落": 0,
        "超时秒": 超时,
        "形态": 默认形,
        "明细": [],
    }
    with 写事务() as conn:
        时刻 = 此刻()
        已晋升 = 尝试晋升(conn, 容差=容差, 时刻=时刻)
        结果["晋升"] = len(已晋升)
        for p in 已晋升:
            结果["明细"].append({"业务键": p["业务键"], "命运": "影子晋升", "差型": p["差型"]})

        左汇总, _, 右汇总, _ = _汇总(conn)
        悬行 = conn.execute(
            "SELECT * FROM 悬账单 WHERE 状态='挂起' ORDER BY 编号"
        ).fetchall()

        for r in 悬行:
            k = r["业务键"]
            if 若键集 is not None and k not in 若键集:
                continue
            结果["扫描"] += 1
            龄 = 悬龄秒(r["时刻"])
            if 龄 < 超时:
                结果["未超时"] += 1
                结果["明细"].append({"业务键": k, "命运": "未超时", "悬龄秒": 龄})
                continue
            # 对边已补齐：不能再升格，否则会与自动对齐双落
            if (k in 左汇总) == (k in 右汇总):
                结果["对边齐全"] += 1
                结果["明细"].append({"业务键": k, "命运": "对边齐全-不升格", "悬龄秒": 龄})
                continue
            形 = str(形态映射.get(k) or 默认形).strip()
            if 形 not in 形态集:
                形 = 默认形
            键 = f"{'旁路' if 形 == 形态旁路 else '影子'}-{k}-{r['编号']}"
            表 = "升格旁路账册" if 形 == 形态旁路 else "影子草稿"
            键列 = "旁路键" if 形 == 形态旁路 else "影子键"
            现成 = conn.execute(
                f"SELECT {键列} AS 键 FROM {表} WHERE 业务键=? AND 有效=1", (k,)
            ).fetchone()
            新开 = 现成 is None
            if 现成:
                # 已有有效升格记录：只把悬账状态对齐到它，不重复开记录
                键 = 现成["键"]
            if 新开:
                try:
                    if 形 == 形态旁路:
                        conn.execute(
                            """INSERT INTO 升格旁路账册
                               (旁路键,业务键,悬边,金额分,事件编号,悬账时刻,升格时悬龄秒,有效,升格时刻,说明)
                               VALUES (?,?,?,?,?,?,?,1,?,?)""",
                            (键, k, r["悬边"], r["金额分"], r["事件编号"], r["时刻"], 龄, 时刻,
                             f"超时{龄}秒升格入旁路账册（终态，不再自动对齐）"),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO 影子草稿
                               (影子键,业务键,悬边,金额分,事件编号,悬账时刻,升格时悬龄秒,有效,升格时刻,说明)
                               VALUES (?,?,?,?,?,?,?,1,?,?)""",
                            (键, k, r["悬边"], r["金额分"], r["事件编号"], r["时刻"], 龄, 时刻,
                             f"超时{龄}秒升格入影子草稿（只读，待补齐晋升）"),
                        )
                except sqlite3.IntegrityError:
                    结果["已升格跳过"] += 1
                    结果["明细"].append({"业务键": k, "命运": "已升格跳过"})
                    continue
            差型 = 差型旁路 if 形 == 形态旁路 else 差型影子
            说明 = (
                f"悬龄{龄}秒≥{超时}秒升格→旁路账册终态"
                if 形 == 形态旁路
                else f"悬龄{龄}秒≥{超时}秒升格→影子草稿待回正"
            )

            n = conn.execute(
                "UPDATE 悬账单 SET 状态='已升格',升格形态=?,升格键=?,升格时刻=? "
                "WHERE 编号=? AND 状态='挂起'",
                (形, 键, 时刻, r["编号"]),
            )
            if n.rowcount != 1:
                # 升格被人工回正等抢跑：撤掉本次刚开的升格记录（已有记录不动），
                # 保持“改悬写差同成败”，不留下无主旁路/影子行。
                if 新开:
                    conn.execute(f"DELETE FROM {表} WHERE {键列}=?", (键,))
                结果["抢跑未落"] += 1
                结果["明细"].append({"业务键": k, "命运": "抢跑未落"})
                continue

            左额 = r["金额分"] if r["悬边"] == "左" else None
            右额 = r["金额分"] if r["悬边"] == "右" else None
            _写差(conn, k, 左额, 右额, 差型, 说明, 时刻)
            结果["升格"] += 1
            结果["明细"].append(
                {"业务键": k, "命运": "已升格", "形态": 形, "升格键": 键, "悬龄秒": 龄, "差型": 差型}
            )

    return 结果


def 晋升(体: dict | None = None) -> dict:
    体 = 体 or {}
    键 = str(体.get("业务键") or "").strip()
    if not 键:
        raise ValueError("缺业务键")
    规则 = 读取规则()
    容差 = _整数(目录.轨规().get("金额容差分", "0"), 0)
    with 写事务() as conn:
        时刻 = 此刻()
        悬 = _现行悬账(conn, 键)
        if 悬 and 悬["状态"] == 状态已升格 and 悬["升格形态"] == 形态旁路:
            return {"晋升": False, "业务键": 键, "原因": "旁路账册终态，不可晋升", "形态": 形态旁路}
        if not 悬 or 悬["状态"] != 状态已升格:
            return {"晋升": False, "业务键": 键, "原因": "无待晋升影子", "状态": 悬["状态"] if 悬 else "无悬账"}
        已办 = 尝试晋升(conn, 容差=容差, 时刻=时刻, 业务键=键)
        if not 已办:
            return {"晋升": False, "业务键": 键, "原因": "对边未补齐，影子保持只读", "形态": 形态影子}
        return {"晋升": True, "业务键": 键, "形态": 形态影子, **已办[0]}


def 人工回正(体: dict | None = None) -> dict:
    """人工回正：与超时升格抢同一张挂起悬账。

    与扫描共用写锁 + CAS：谁先把 状态 从 '挂起' 改走，谁落地；另一方返回未落。
    对边缺失时允许由人工给定 金额分 作为对侧口径；不伪造轨迹流水。
    """
    体 = 体 or {}
    键 = str(体.get("业务键") or "").strip()
    if not 键:
        raise ValueError("缺业务键")
    边 = 体.get("边")
    人工金额 = 体.get("金额分")
    说明 = str(体.get("说明") or "人工回正")
    容差 = _整数(目录.轨规().get("金额容差分", "0"), 0)
    with 写事务() as conn:
        时刻 = 此刻()
        左汇总, _, 右汇总, _ = _汇总(conn)
        sql = "SELECT * FROM 悬账单 WHERE 业务键=? AND 状态 IN ('挂起','已升格')"
        参数: tuple = (键,)
        if 边 in ("左", "右"):
            sql += " AND 悬边=?"
            参数 += (边,)
        行 = conn.execute(sql + " ORDER BY 编号", 参数).fetchall()
        if not 行:
            return {"回正": False, "业务键": 键, "原因": "无现行悬账"}
        已升格 = [r for r in 行 if r["状态"] == 状态已升格]
        if 已升格:
            return {
                "回正": False,
                "业务键": 键,
                "原因": "悬账已升格，人工回正未落（升格先到）",
                "形态": 已升格[0]["升格形态"],
                "升格键": 已升格[0]["升格键"],
            }
        两边齐 = 键 in 左汇总 and 键 in 右汇总
        if not 两边齐 and 人工金额 is None:
            return {"回正": False, "业务键": 键, "原因": "对边未补齐且未给金额分，不可回正"}

        已回 = []
        for r in 行:
            左额 = 左汇总.get(键, int(r["金额分"]) if r["悬边"] == "左" else 人工金额)
            右额 = 右汇总.get(键, int(r["金额分"]) if r["悬边"] == "右" else 人工金额)
            if 左额 is None or 右额 is None:
                continue
            差型 = "对齐" if abs(int(左额) - int(右额)) <= 容差 else "金额不符"
            n = conn.execute(
                "UPDATE 悬账单 SET 状态='已回正',升格形态=?,升格键='',升格时刻=? "
                "WHERE 编号=? AND 状态='挂起'",
                (形态人工, 时刻, r["编号"]),
            )
            if n.rowcount != 1:
                continue
            _写差(conn, 键, int(左额), int(右额), 差型, f"{说明}（{差型}）", 时刻)
            已回.append({"悬账编号": r["编号"], "悬边": r["悬边"], "差型": 差型})
        if not 已回:
            return {"回正": False, "业务键": 键, "原因": "抢跑未落（状态已被并发改写）"}
        return {"回正": True, "业务键": 键, "形态": 形态人工, "明细": 已回}


def 补齐对边(体: dict | None = None) -> dict:
    """补齐对边：只补轨迹流水，不越权改悬账判态。

    - 影子：同事务内原子晋升回现行对齐；
    - 旁路：只入账，终态不自动对齐（两边命运分开）；
    - 挂起未升格：只入账，等下一次跑对齐或人工回正。
    """
    体 = 体 or {}
    键 = str(体.get("业务键") or "").strip()
    边 = 体.get("边")
    if not 键:
        raise ValueError("缺业务键")
    if 边 not in ("左", "右"):
        raise ValueError("边须为 左/右")
    容差 = _整数(目录.轨规().get("金额容差分", "0"), 0)
    with 写事务() as conn:
        时刻 = 此刻()
        悬 = _现行悬账(conn, 键)
        默认额 = int(悬["金额分"]) if 悬 else 0
        金额 = _整数(体.get("金额分"), 默认额)
        编号 = str(体.get("事件编号") or f"补-{边}-{键}-{uuid.uuid4().hex[:8]}")
        事件时 = str(体.get("事件时") or 时刻)
        源键 = str(体.get("源键") or "").strip()
        if not 源键:
            源 = conn.execute(
                "SELECT 源键 FROM 源登记 WHERE 轨别=? ORDER BY 源键 LIMIT 1", (边,)
            ).fetchone()
            源键 = 源["源键"] if 源 else ("渠甲" if 边 == "左" else "核乙")
        表 = "左轨流水" if 边 == "左" else "右轨流水"
        命运 = "已入左" if 边 == "左" else "已入右"
        已存在 = conn.execute(f"SELECT 1 FROM {表} WHERE 事件编号=?", (编号,)).fetchone()
        if not 已存在:
            conn.execute(
                f"INSERT INTO {表}(事件编号,业务键,金额分,源键,事件时,处理时,命运,批次编号) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (编号, 键, 金额, 源键, 事件时, 时刻, 命运, "补齐对边"),
            )
        已晋升 = 尝试晋升(conn, 容差=容差, 时刻=时刻, 业务键=键)
        悬 = _现行悬账(conn, 键)
        状态 = 悬["状态"] if 悬 else "无悬账"
        形 = 悬["升格形态"] if 悬 else ""
        两边齐 = bool(
            conn.execute(f"SELECT 1 FROM {表} WHERE 业务键=?", (键,)).fetchone()
        ) and bool(
            conn.execute(
                f"SELECT 1 FROM {'右轨流水' if 边 == '左' else '左轨流水'} WHERE 业务键=?",
                (键,),
            ).fetchone()
        )
        return {
            "补齐": True,
            "业务键": 键,
            "边": 边,
            "事件编号": 编号,
            "金额分": 金额,
            "已存在": bool(已存在),
            "对边齐全": 两边齐,
            "状态": 状态,
            "升格形态": 形,
            "晋升": bool(已晋升),
            "晋升明细": 已晋升,
            "可自动对齐": 状态 != 状态已升格,
            "命运": (
                "影子已晋升回现行对齐"
                if 已晋升
                else ("旁路终态：已入账但不再自动对齐" if 形 == 形态旁路
                      else ("已补齐，待跑对齐" if 状态 != 状态已升格 else "已补齐，影子仍待晋升"))
            ),
        }
