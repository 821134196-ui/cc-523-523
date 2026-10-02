"""演示种子数据。

故事线（河东村便民点）：
- 2026-08 账期周**-01 现金已缴，有正常收据与凭证；
- 错户样例：赵小娥误把 2026-09 水费收到了 吴**-05 户上，钱主管批准冲正——
  原收据 R... 已作废、红字更正收据并列，额度回补；
- 上一班次交班现金短款 12.00 元，已留有符号 SHORTAGE 事件；
- 一笔支付宝「支付成功、水务销账回执延迟」（可在页面重推）；
- 一笔微信渠道支付回执延迟（可在页面「同步渠道回执」）；
- 赵小娥当前班次已开班，可直接收款。
"""
import json
from pathlib import Path

from .adapters import PaymentAdapter, WaterAdapter
from .db import connect, init_db
from .money import yuan_to_cents
from .service import WaterService


def _seed(conn, receipt_dir: Path, script_path: Path):
    pay = PaymentAdapter(script_path)
    water = WaterAdapter(pay)
    svc = WaterService(conn, receipt_dir, pay, water)

    conn.executescript("""
    INSERT INTO sites VALUES ('HD','河东村便民点');
    INSERT INTO sites VALUES ('HX','河西村便民点');

    INSERT INTO operators VALUES ('op_zhao','zhao','赵小娥','HD','operator');
    INSERT INTO operators VALUES ('op_qian','qian','钱主管','HD','supervisor');
    INSERT INTO operators VALUES ('op_sun','sun','孙大柱','HX','operator');
    INSERT INTO operators VALUES ('op_li','li','李主管','HX','supervisor');

    INSERT INTO households VALUES ('320101','周**-01','河东一组','HD');
    INSERT INTO households VALUES ('320102','吴**-05','河东二组','HD');
    INSERT INTO households VALUES ('320103','郑**-12','河东二组','HD');
    INSERT INTO households VALUES ('320104','王**-08','河东三组','HD');
    INSERT INTO households VALUES ('320105','冯**-03','河东三组','HD');
    INSERT INTO households VALUES ('410207','陈**-21','河西村','HX');
    INSERT INTO households VALUES ('410208','褚**-17','河西村','HX');
    """)

    bills = [
        # 户号, 账期, 应缴
        ("320101", "2026-08", "36.50"),
        ("320101", "2026-09", "42.00"),
        ("320101", "2026-10", "38.70"),
        ("320102", "2026-08", "15.20"),
        ("320102", "2026-09", "46.00"),   # 错户冲正样例账期，冲正后重新待缴
        ("320102", "2026-10", "51.30"),
        ("320103", "2026-09", "28.00"),
        ("320103", "2026-10", "33.40"),
        ("320104", "2026-09", "60.00"),
        ("320105", "2026-10", "24.60"),
        ("410207", "2026-09", "39.90"),
        ("410208", "2026-10", "44.10"),
    ]
    conn.executemany(
        "INSERT INTO bills (account_no, period, amount_cents) VALUES (?,?,?)",
        [(a, p, yuan_to_cents(m)) for a, p, m in bills],
    )

    zhao = svc.login("zhao")
    qian = svc.login("qian")
    sun = svc.login("sun")

    # ---- 上一班次（已结，含短款 12.00）--------------------------------
    sid1 = svc.open_shift(zhao, opening_float_cents=yuan_to_cents("200"))
    # 班内两笔现金
    svc.pay_bill(zhao, "320101", "2026-08", "cash")
    wrong = svc.pay_bill(zhao, "320102", "2026-09", "cash")
    # 错户：当班即发现，主管批准冲正，现金退回
    rid = svc.request_reversal(
        zhao, wrong["txn_id"], "wrong_account",
        "居民本意缴本户 2026-09 水费，操作员误录到户号 320102（吴**-05）")
    svc.review_reversal(qian, rid, True, "情况属实，原收据作废、现金退回，重新代收")
    # 交班实点比应存少 12.00（短款）
    report = svc.shift_report(zhao, sid1)
    svc.close_shift(zhao, sid1,
                    report["expected_cash"] - yuan_to_cents("12.00"),
                    "零钞兑换误差，次日补缴")

    # ---- 异常回执样例 ---------------------------------------------------
    sid2 = svc.open_shift(zhao, opening_float_cents=yuan_to_cents("200"))
    # 支付成功、水务销账回执延迟
    svc.pay_bill(zhao, "320104", "2026-09", "alipay", simulate="water_delay")
    # 渠道支付回执延迟（钱在途中，本地待确认）
    svc.pay_bill(zhao, "320105", "2026-10", "wechat", simulate="pay_delay")

    # 河西村开班，便于演示站点隔离
    svc.open_shift(sun, opening_float_cents=yuan_to_cents("100"))

    # 让水务延迟样例的流水在脚本中也登记（真实配置文件用法示例）
    rows = conn.execute(
        "SELECT pay_ref FROM ledger WHERE water_status='PENDING' OR pay_status='PENDING'"
    ).fetchall()
    script = {}
    for r in rows:
        ref = r["pay_ref"]
        if ref.startswith("WATDELAY"):
            script[ref] = {"pay": "success", "water": "delay"}
        elif ref.startswith("PAYDELAY"):
            script[ref] = {"pay": "delay", "water": "delay"}
    script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return sid2


def seed_if_empty(db_path: str | Path, receipt_dir: str | Path,
                  script_path: str | Path) -> bool:
    db_path, receipt_dir, script_path = Path(db_path), Path(receipt_dir), Path(script_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM sites").fetchone()["c"]:
        conn.close()
        return False
    _seed(conn, receipt_dir, script_path)
    conn.close()
    return True
