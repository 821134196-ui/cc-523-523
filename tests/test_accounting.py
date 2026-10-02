"""账务核心测试。

覆盖题目四类关键场景：
  1) 重复回执幂等（支付渠道 / 水务回执 / 补登）
  2) 同一账单并发付款：恰好一笔成功，应收绝不为负
  3) 冲正平衡：必须引用原交易 + 主管批准，不删除流水，借贷净额为零
  4) 站点隔离：操作员只能看本站点现金记录、只能收本站点户
另含错户号、跨账期、金额不符、延迟到账、现金短款等异常路径。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

from app import db
from app.adapters import PaymentAdapter, WaterAdapter
from app.services import AuthzError, BusinessError, Service


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        conn = db.connect(self.db_path)
        db.init_db(conn)
        self.payments = PaymentAdapter()
        self.water = WaterAdapter()
        self.svc = Service(conn, payments=self.payments, water=self.water,
                           receipt_dir=os.path.join(self.tmp.name, "rcpts"))
        self._seed()

    def tearDown(self) -> None:
        self.svc.conn.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ 基础数据

    def _seed(self) -> None:
        c = self.svc.conn
        c.executescript("""
            INSERT INTO sites(code,name) VALUES('QS','青石'),('WG','梧岗');
            INSERT INTO users VALUES('OP1','z','张三','operator','QS');
            INSERT INTO users VALUES('OP2','l','李四','operator','WG');
            INSERT INTO users VALUES('SUP','w','王主管','supervisor',NULL);
            INSERT INTO households VALUES('HH1','H1','户1','QS');
            INSERT INTO households VALUES('HH2','H2','户2','QS');
            INSERT INTO households VALUES('HH3','H3','户3','WG');
            INSERT INTO bills VALUES('B1','HH1','2026-09',1000);
            INSERT INTO bills VALUES('B2','HH2','2026-09',2000);
            INSERT INTO bills VALUES('B3','HH3','2026-09',3000);
        """)
        self.op1 = c.execute("SELECT * FROM users WHERE id='OP1'").fetchone()
        self.op2 = c.execute("SELECT * FROM users WHERE id='OP2'").fetchone()
        self.sup = c.execute("SELECT * FROM users WHERE id='SUP'").fetchone()

    def paid_for_bill(self, bill_id="B1") -> int:
        return self.svc.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM ledger_events "
            "WHERE bill_id=? AND kind IN ('PAYMENT','REVERSAL')", (bill_id,)
        ).fetchone()["s"]

    def n_events(self, bill_id="B1") -> int:
        return self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events WHERE bill_id=?", (bill_id,)
        ).fetchone()["c"]

    # ------------------------------------------------------------ 输入校验

    def test_wrong_account_number(self):
        with self.assertRaisesRegex(BusinessError, "不存在"):
            self.svc.pay(self.op1, "NOPE", "2026-09", 1000, "CASH")
        with self.assertRaisesRegex(BusinessError, "不存在"):
            self.svc.list_bills("NOPE")

    def test_cross_period_rejected(self):
        with self.assertRaisesRegex(BusinessError, "跨账期"):
            self.svc.pay(self.op1, "H1", "2030-01", 1000, "CASH")

    def test_amount_must_equal_due(self):
        with self.assertRaisesRegex(BusinessError, "应缴不一致"):
            self.svc.pay(self.op1, "H1", "2026-09", 900, "CASH")
        with self.assertRaisesRegex(BusinessError, "正数"):
            self.svc.pay(self.op1, "H1", "2026-09", 0, "CASH")

    # ------------------------------------------------------------ 重复回执

    def test_duplicate_payment_channel_receipt_idempotent(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "ELECTRONIC")
        ref = ev["pay_ref"]
        # 渠道侧对同一 pay_ref 的重复扣款通知只返回首次结果
        r1 = self.payments.charge(ref, 1000, "ELECTRONIC")
        r2 = self.payments.charge(ref, 1000, "ELECTRONIC")
        self.assertEqual((r1.status, r2.status), ("SUCCESS", "SUCCESS"))
        self.assertEqual(self.n_events(), 1)  # 未产生第二笔流水

    def test_duplicate_water_receipt_idempotent(self):
        # 支付成功但水务回执延迟 -> 补登一次；再同步不得重复补登
        self.svc.water.delay_bills.add("B1")
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        self.assertIsNone(ev["water_ref"])
        s1 = self.svc.sync_receipts()
        self.assertEqual(s1["water_confirmed"], 1)
        s2 = self.svc.sync_receipts()
        self.assertEqual(s2["water_confirmed"], 0)
        confirms = self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events WHERE confirms_id=? "
            "AND kind='WATER_CONFIRM'", (ev["id"],)).fetchone()["c"]
        self.assertEqual(confirms, 1)

    def test_delayed_payment_then_confirm_once(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "ELECTRONIC",
                          delay_payment=True)
        self.assertEqual(ev["pay_status"], "PENDING")
        # PENDING 已占应收额度：重复缴费仍被拒绝
        with self.assertRaisesRegex(BusinessError, "已缴清"):
            self.svc.pay(self.op1, "H1", "2026-09", 1000, "ELECTRONIC")
        s1 = self.svc.sync_receipts()
        self.assertEqual(s1["paid_confirmed"], 1)
        s2 = self.svc.sync_receipts()
        self.assertEqual(s2["paid_confirmed"], 0)
        kinds = [r["kind"] for r in self.svc.conn.execute(
            "SELECT kind FROM ledger_events WHERE bill_id='B1' ORDER BY seq")]
        self.assertEqual(kinds, ["PAYMENT", "PAY_CONFIRM", "WATER_CONFIRM"])

    def test_repeat_payment_on_same_bill_rejected(self):
        self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        with self.assertRaisesRegex(BusinessError, "重复缴费|已缴清"):
            self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        self.assertEqual(self.paid_for_bill(), 1000)  # 应收不变负

    # ------------------------------------------------------------ 并发付款

    def test_concurrent_payments_exactly_one_wins(self):
        results: list[Exception | object] = []
        barrier = threading.Barrier(2)

        def worker():
            conn = db.connect(self.db_path)
            svc = Service(conn, payments=self.payments, water=self.water)
            op = conn.execute("SELECT * FROM users WHERE id='OP1'").fetchone()
            barrier.wait()
            try:
                ev = svc.pay(op, "H1", "2026-09", 1000, "CASH")
                results.append(ev["id"])
            except Exception as ex:  # noqa: BLE001 - 测试需记录失败方
                results.append(ex)
            conn.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if isinstance(r, str)]
        errs = [r for r in results if not isinstance(r, str)]
        self.assertEqual(len(oks), 1, f"应恰好一笔成功：{results}")
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0], BusinessError)
        # 只有一笔缴款，应收 1000 全额抵减，绝不为负
        self.assertEqual(self.paid_for_bill(), 1000)
        self.assertEqual(self.n_events(), 1)
        # 现金记录总额也只有 1000
        total = self.svc.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM ledger_events "
            "WHERE bill_id='B1' AND channel='CASH'").fetchone()["s"]
        self.assertEqual(total, 1000)

    # ------------------------------------------------------------ 冲正平衡

    def test_reversal_requires_request_approval_and_reference(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        # 未审批前没有冲正事件
        rid = self.svc.request_reversal(self.op1, ev["id"], "输错户号")
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events WHERE reverses_id=?",
            (ev["id"],)).fetchone()["c"], 0)
        # 操作员不能自批
        with self.assertRaises(AuthzError):
            self.svc.decide_reversal(self.op1, rid, True)
        # 主管批准后生成负数冲正单
        rev_id = self.svc.decide_reversal(self.sup, rid, True, "同意")
        rev = self.svc.conn.execute(
            "SELECT * FROM ledger_events WHERE id=?", (rev_id,)).fetchone()
        self.assertEqual(rev["kind"], "REVERSAL")
        self.assertEqual(rev["amount"], -1000)          # 相反数
        self.assertEqual(rev["reverses_id"], ev["id"])  # 引用原交易
        self.assertEqual(rev["approved_by"], "SUP")     # 主管批准
        self.assertEqual(rev["channel"], "CASH")

    def test_reversal_balances_to_zero_and_keeps_chain(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        count_before = self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events").fetchone()["c"]
        rid = self.svc.request_reversal(self.op1, ev["id"], "收据作废")
        self.svc.decide_reversal(self.sup, rid, True)
        count_after = self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events").fetchone()["c"]
        # 原流水不删除：只新增了一条冲正事件
        self.assertEqual(count_after, count_before + 1)
        orig = self.svc.payment_detail(ev["id"])
        self.assertEqual(orig["amount"], 1000)                 # 原单保持正数
        self.assertEqual(orig["receipt_status"], "VOID")       # 收据标记作废
        # 该账单净缴为零：缴款 +1000 与冲正 -1000 平衡
        self.assertEqual(self.paid_for_bill(), 0)
        # 冲正后可重新向正确账单收款
        again = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        self.assertEqual(again["amount"], 1000)
        self.assertEqual(self.paid_for_bill(), 1000)

    def test_reject_reversal_creates_no_event(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        rid = self.svc.request_reversal(self.op1, ev["id"], "误操作")
        self.svc.decide_reversal(self.sup, rid, False)
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) c FROM ledger_events WHERE reverses_id=?",
            (ev["id"],)).fetchone()["c"], 0)
        self.assertEqual(self.paid_for_bill(), 1000)
        # 已处理的申请不能重复审批
        with self.assertRaisesRegex(BusinessError, "已审批"):
            self.svc.decide_reversal(self.sup, rid, True)

    def test_double_reversal_blocked(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        rid = self.svc.request_reversal(self.op1, ev["id"], "原因")
        self.svc.decide_reversal(self.sup, rid, True)
        with self.assertRaisesRegex(BusinessError, "已被冲正|已存在"):
            self.svc.request_reversal(self.op1, ev["id"], "再冲一次")

    def test_other_operator_cannot_request_reversal(self):
        ev = self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        with self.assertRaises(AuthzError):
            self.svc.request_reversal(self.op2, ev["id"], "不是我经办")

    # ------------------------------------------------------------ 现金短款

    def test_cash_shortage_records_negative_event(self):
        self.svc.open_shift(self.op1)
        self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        result = self.svc.close_shift(self.op1, 900, "少1元")  # 短款 1.00
        self.assertEqual(result["diff"], -100)
        short = self.svc.conn.execute(
            "SELECT * FROM ledger_events WHERE kind='SHORTAGE'").fetchone()
        self.assertEqual(short["amount"], -100)
        self.assertEqual(short["site_code"], "QS")
        # 现金类事件借贷平衡：+1000 缴款，-100 短款（抽屉里 900 + 责任 100）
        total = self.svc.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM ledger_events "
            "WHERE channel='CASH' AND kind IN ('PAYMENT','SHORTAGE')"
        ).fetchone()["s"]
        self.assertEqual(total, 900)
        # 短款事件不允许负数实点
        self.svc.open_shift(self.op1)
        with self.assertRaisesRegex(BusinessError, "负数"):
            self.svc.close_shift(self.op1, -1)

    # ------------------------------------------------------------ 站点隔离

    def test_operator_cannot_collect_other_site(self):
        with self.assertRaises(AuthzError):
            self.svc.pay(self.op1, "H3", "2026-09", 3000, "CASH")

    def test_cash_records_site_isolation(self):
        self.svc.pay(self.op1, "H1", "2026-09", 1000, "CASH")
        self.svc.pay(self.op2, "H3", "2026-09", 3000, "CASH")
        qs = self.svc.cash_records(self.op1)
        self.assertTrue(all(r["site_code"] == "QS" for r in qs))
        self.assertEqual(len(qs), 1)
        # 显式请求其他站点 -> 拒绝
        with self.assertRaises(AuthzError):
            self.svc.cash_records(self.op1, "WG")
        # 主管可跨站点
        allrecs = self.svc.cash_records(self.sup)
        sites = {r["site_code"] for r in allrecs}
        self.assertEqual(sites, {"QS", "WG"})
        wg_only = self.svc.cash_records(self.sup, "WG")
        self.assertTrue(all(r["site_code"] == "WG" for r in wg_only))

    def test_shifts_are_per_operator_and_site(self):
        self.svc.open_shift(self.op1)
        with self.assertRaises(BusinessError):
            self.svc.open_shift(self.op1)  # 重复开班
        # 另一站点操作员互不影响
        sid2 = self.svc.open_shift(self.op2)
        self.assertTrue(sid2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
