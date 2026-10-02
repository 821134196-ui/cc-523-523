"""冲正平衡：冲正必须引用原交易、经主管批准；原单 + 红字单金额之和为零。"""
from water.money import yuan_to_cents
from water.service import AuthError, ServiceError
from tests.helpers import ServiceCase


class TestReversalBalance(ServiceCase):
    def _pay_one(self, channel="cash"):
        self.open_shift()
        return self.svc.pay_bill(self.zhao, "320102", "2026-09", channel)

    def test_reversal_net_zero_and_bill_due_restored(self):
        paid = self._pay_one("cash")
        rid = self.svc.request_reversal(
            self.zhao, paid["txn_id"], "wrong_account", "误录户号")
        res = self.svc.review_reversal(self.qian, rid, True, "属实")

        rev = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (res["txn_id"],)).fetchone()
        orig = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (paid["txn_id"],)).fetchone()

        # 冲正必须引用原交易
        self.assertEqual(rev["related_txn"], paid["txn_id"])
        self.assertEqual(rev["event_type"], "REVERSAL")
        # 等额为负
        self.assertEqual(rev["amount_cents"], -orig["amount_cents"])
        # 原单 + 更正单净额为零，应收完全回补
        self.assertEqual(orig["amount_cents"] + rev["amount_cents"], 0)
        self.assertEqual(self.due("320102"), yuan_to_cents("46.00"))
        # 账单净缴为零但流水两条都还在（不删除）
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM ledger WHERE bill_id=?",
            (self.bill_id("320102"),)).fetchone()["c"]
        self.assertEqual(n, 2)

    def test_voided_receipt_chain(self):
        paid = self._pay_one()
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "duplicate", "重复")
        res = self.svc.review_reversal(self.qian, rid, True)

        orig_r = self.conn.execute(
            "SELECT * FROM receipts WHERE txn_id=?", (paid["txn_id"],)).fetchone()
        red_r = self.conn.execute(
            "SELECT * FROM receipts WHERE receipt_no=?",
            (res["receipt_no"],)).fetchone()
        self.assertTrue(orig_r["voided"])
        self.assertEqual(orig_r["linked_receipt_no"], red_r["receipt_no"])
        self.assertEqual(red_r["kind"], "REDLINE")
        self.assertEqual(red_r["linked_receipt_no"], orig_r["receipt_no"])
        # 两张收据文件都保留在磁盘上
        from pathlib import Path
        self.assertTrue(Path(orig_r["file_path"]).exists())
        self.assertTrue(Path(red_r["file_path"]).exists())

    def test_operator_cannot_approve(self):
        paid = self._pay_one()
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "x")
        with self.assertRaises(AuthError):
            self.svc.review_reversal(self.zhao, rid, True)
        # 未批准前没有冲正事件、额度不回补
        self.assertEqual(self.due("320102"), 0)

    def test_supervisor_of_other_site_cannot_approve(self):
        paid = self._pay_one()
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "x")
        with self.assertRaises(AuthError):
            self.svc.review_reversal(self.li, rid, True)

    def test_cannot_reverse_twice_or_double_request(self):
        paid = self._pay_one()
        rid1 = self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "x")
        # 待批期间重复申请被拒
        with self.assertRaises(ServiceError):
            self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "x")
        self.svc.review_reversal(self.qian, rid1, True)
        # 批准后再次申请被拒
        with self.assertRaises(ServiceError):
            self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "x")
        # 驳回后可以再次申请
        paid2 = self.svc.pay_bill(self.zhao, "320102", "2026-10", "cash")
        rid2 = self.svc.request_reversal(self.zhao, paid2["txn_id"], "other", "x")
        self.svc.review_reversal(self.qian, rid2, False)
        rid3 = self.svc.request_reversal(self.zhao, paid2["txn_id"], "other", "再次申请")
        self.assertIsInstance(rid3, int)

    def test_electronic_reversal_creates_refund_ref(self):
        paid = self._pay_one("alipay")
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "gateway", "回执异常")
        res = self.svc.review_reversal(self.qian, rid, True)
        rev = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (res["txn_id"],)).fetchone()
        self.assertTrue(rev["pay_ref"].startswith("REF"))
        self.assertEqual(rev["amount_cents"], -yuan_to_cents("46.00"))

    def test_pending_water_cannot_reverse_until_confirmed(self):
        self.open_shift()
        r = self.svc.pay_bill(self.zhao, "320102", "2026-09",
                              "alipay", simulate="water_delay")
        with self.assertRaises(ServiceError):
            self.svc.request_reversal(self.zhao, r["txn_id"], "other", "x")
        # 重推确认后可以冲正
        self.svc.reconfirm_water(self.zhao, r["txn_id"])
        rid = self.svc.request_reversal(self.zhao, r["txn_id"], "other", "x")
        self.assertIsInstance(rid, int)


if __name__ == "__main__":
    import unittest
    unittest.main()
