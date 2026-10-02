"""演示种子数据 + 一键启动。

用法：
    python3 -m app.seed                # 仅补种子（已有数据则跳过）
    python3 -m app.seed --serve       # 补种子后启动 Web（共享模拟适配器状态）
    python3 -m app.seed --reset       # 删除 data/ 后重建
    python3 -m app.seed --reset --serve 8010

种子覆盖全部异常样例：
  1. 输错户号：误收 H1001 的现金收据已作废，主管批准的负数冲正单与原单并列，
     随后向正确户 H1002 重新收款；
  2. 待主管审批的冲正申请（H2001 重复缴费/收据作废）；
  3. 已交班班次的现金短款 SHORTAGE 负数事件；
  4. 电子支付到账延迟（H3001，待补登 PAY_CONFIRM）；
  5. 支付成功但水务回执延迟（H2001 上期，待补登 WATER_CONFIRM）；
  6. 跨站点隔离：H3001 属于梧岗镇，青石镇操作员不可见其现金记录。
"""
from __future__ import annotations

import os
import shutil
import sys

from . import db
from .adapters import PaymentAdapter, WaterAdapter
from .services import Service
from .web import DB_PATH, RECEIPT_DIR, build_app, main as serve

SITES = [
    ("QS-01", "青石镇便民服务点"),
    ("WG-02", "梧岗镇便民服务点"),
]
USERS = [
    ("U01", "zhangsan", "张三", "operator", "QS-01"),
    ("U02", "lisi", "李四", "operator", "WG-02"),
    ("U09", "wangjg", "王主管", "supervisor", None),
]
HOUSEHOLDS = [
    ("HH1", "H1001", "青-A-031", "QS-01"),
    ("HH2", "H1002", "青-A-032", "QS-01"),
    ("HH3", "H2001", "青-B-117", "QS-01"),
    ("HH4", "H3001", "梧-C-058", "WG-02"),
]
# (户, 账期, 应缴分)
BILLS = [
    ("HH1", "2026-07", 5000),
    ("HH1", "2026-08", 3200),
    ("HH1", "2026-09", 4150),
    ("HH2", "2026-08", 3600),
    ("HH2", "2026-09", 3600),
    ("HH3", "2026-07", 2800),
    ("HH3", "2026-08", 2800),
    ("HH3", "2026-09", 2800),
    ("HH4", "2026-08", 4500),
    ("HH4", "2026-09", 4500),
]


def seed(svc: Service, verbose: bool = True) -> bool:
    """返回是否执行了播种。"""
    conn = svc.conn
    if conn.execute("SELECT COUNT(*) c FROM sites").fetchone()["c"]:
        if verbose:
            print("已有演示数据，跳过播种（--reset 可重建）。")
        return False

    conn.execute("BEGIN")
    for code, name in SITES:
        conn.execute("INSERT INTO sites(code, name) VALUES(?,?)", (code, name))
    for uid, uname, dname, role, site in USERS:
        conn.execute(
            "INSERT INTO users(id, username, display_name, role, site_code) "
            "VALUES(?,?,?,?,?)",
            (uid, uname, dname, role, site),
        )
    for hid, acct, code_name, site in HOUSEHOLDS:
        conn.execute(
            "INSERT INTO households(id, account_no, name_code, site_code) "
            "VALUES(?,?,?,?)",
            (hid, acct, code_name, site),
        )
    for hid, period, due in BILLS:
        conn.execute(
            "INSERT INTO bills(id, household_id, period, amount_due) "
            "VALUES(?,?,?,?)",
            (db.next_id("BIL"), hid, period, due),
        )
    conn.execute("COMMIT")

    op1 = svc.user("U01")
    op2 = svc.user("U02")
    sup = svc.user("U09")

    # —— 上一班次：错户收款 → 冲正 → 正确重收 → 短款交班 ——
    svc.open_shift(op1)
    # 1) 输错户号：本应收 H1002，误收 H1001（现金）
    wrong = svc.pay(op1, "H1001", "2026-08", 3200, "CASH")
    req = svc.request_reversal(
        op1, wrong["id"], "输错户号：本应缴人为 H1002，误收 H1001，申请作废收据")
    svc.decide_reversal(sup, req, True, "核对身份证与户号后同意冲正")
    # 2) 向正确户 H1002 重新收款
    svc.pay(op1, "H1002", "2026-08", 3600, "CASH")
    # 3) H1001 2026-07 正常历史缴款
    svc.pay(op1, "H1001", "2026-07", 5000, "CASH")
    # 4) H2001 2026-07 电子支付：支付成功但水务回执延迟（待补登）
    svc.pay(op1, "H2001", "2026-07", 2800, "ELECTRONIC", delay_water=True)
    # 5) 交班：系统应收 3600+5000=8600（错户款已冲回），实点 7600，短款 10.00
    svc.close_shift(op1, 7600, "抽屉少 10 元，已登记短款，次日补缴")

    # —— 当前班次：一笔待审批冲正 ——
    svc.open_shift(op1)
    dup = svc.pay(op1, "H2001", "2026-08", 2800, "CASH")
    svc.request_reversal(
        op1, dup["id"], "居民称当日已在电子渠道缴过本期，疑似重复缴费，收据待作废")

    # —— 梧岗镇：电子支付到账延迟（渠道 PENDING，待补登）——
    svc.open_shift(op2)
    svc.pay(op2, "H3001", "2026-08", 4500, "ELECTRONIC", delay_payment=True)

    if verbose:
        print("演示数据播种完成：")
        print("  - 错户冲正链：H1001 2026-08 原收据已作废 + 负数冲正单（主管已批准）")
        print("  - 待批冲正：H2001 2026-08（重复缴费，收据待作废）")
        print("  - 历史班次短款 10.00 元（SHORTAGE 负数事件）")
        print("  - H3001 2026-08 电子支付到账延迟；H2001 2026-07 水务回执延迟")
        print("  - 站点隔离：青石 QS-01 / 梧岗 WG-02")
    return True


def main(argv: list[str]) -> None:
    reset = "--reset" in argv
    serve_now = "--serve" in argv
    port = 8000
    rest = [a for a in argv[1:] if a not in ("--reset", "--serve")]
    if rest and rest[0].isdigit():
        port = int(rest[0])

    if reset and os.path.exists(os.path.dirname(DB_PATH)):
        shutil.rmtree(os.path.dirname(DB_PATH))
        print(f"已清空 {os.path.dirname(DB_PATH)}/")

    svc = build_app()
    seed(svc)
    if serve_now:
        serve(port=port, svc=svc)


if __name__ == "__main__":
    main(sys.argv)
