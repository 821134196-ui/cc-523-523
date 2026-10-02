"""测试公共夹具：每个用例独立临时库 + 最小主数据。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from water.adapters import PaymentAdapter, WaterAdapter
from water.db import connect, init_db
from water.money import yuan_to_cents
from water.service import WaterService


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db_path = base / "t.db"
        self.receipt_dir = base / "receipts"
        self.script_path = base / "gw.json"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self._seed_master()
        self.payment = PaymentAdapter(self.script_path)
        self.water = WaterAdapter(self.payment)
        self.svc = WaterService(self.conn, self.receipt_dir, self.payment, self.water)
        self.zhao = self.svc.login("zhao")   # HD 操作员
        self.qian = self.svc.login("qian")   # HD 主管
        self.sun = self.svc.login("sun")     # HX 操作员
        self.li = self.svc.login("li")       # HX 主管

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed_master(self):
        self.conn.executescript("""
        INSERT INTO sites VALUES ('HD','河东村便民点');
        INSERT INTO sites VALUES ('HX','河西村便民点');
        INSERT INTO operators VALUES ('op_zhao','zhao','赵小娥','HD','operator');
        INSERT INTO operators VALUES ('op_qian','qian','钱主管','HD','supervisor');
        INSERT INTO operators VALUES ('op_sun','sun','孙大柱','HX','operator');
        INSERT INTO operators VALUES ('op_li','li','李主管','HX','supervisor');
        INSERT INTO households VALUES ('320101','周**-01','河东一组','HD');
        INSERT INTO households VALUES ('320102','吴**-05','河东二组','HD');
        INSERT INTO households VALUES ('410207','陈**-21','河西村','HX');
        """)
        bills = [
            ("320101", "2026-09", "42.00"),
            ("320102", "2026-09", "46.00"),
            ("320102", "2026-10", "51.30"),
            ("410207", "2026-09", "39.90"),
        ]
        self.conn.executemany(
            "INSERT INTO bills (account_no, period, amount_cents) VALUES (?,?,?)",
            [(a, p, yuan_to_cents(m)) for a, p, m in bills],
        )

    # —— 便捷方法 ——
    def bill_id(self, account_no="320101", period="2026-09"):
        return self.conn.execute(
            "SELECT bill_id FROM bills WHERE account_no=? AND period=?",
            (account_no, period)).fetchone()["bill_id"]

    def due(self, account_no="320101", period="2026-09"):
        return self.svc.bill_state(self.bill_id(account_no, period))["due"]

    def open_shift(self, op=None, float_yuan="200"):
        op = op or self.zhao
        return self.svc.open_shift(op, yuan_to_cents(float_yuan))

    def approve(self, op, original_txn_id, reason="wrong_account", detail="测试"):
        rid = self.svc.request_reversal(op, original_txn_id, reason, detail)
        return self.svc.review_reversal(self.qian, rid, True, "同意")
