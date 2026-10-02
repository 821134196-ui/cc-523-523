"""交班对账：备用金 + 现金收款 − 现金冲正退回 = 应存；短款留负向事件且账实平衡。"""
from water.money import yuan_to_cents
from water.service import ServiceError
from tests.helpers import ServiceCase


class TestShiftReconcile(ServiceCase):
    def test_clean_shift_balances(self):
        sid = self.open_shift(float_yuan="200")
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")  # +42
        report = self.svc.shift_report(self.zhao, sid)
        self.assertEqual(report["expected_cash"], yuan_to_cents("242"))
        res = self.svc.close_shift(self.zhao, sid, report["expected_cash"])
        self.assertEqual(res["diff"], 0)
        self.assertIsNone(res["shortage_txn"])

    def test_cash_shortage_records_signed_event(self):
        sid = self.open_shift(float_yuan="200")
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")  # 应存 242
        res = self.svc.close_shift(self.zhao, sid, yuan_to_cents("230"), "零钞误差")
        # 短款 -12.00
        self.assertEqual(res["diff"], -yuan_to_cents("12.00"))
        self.assertIsNotNone(res["shortage_txn"])
        row = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (res["shortage_txn"],)).fetchone()
        self.assertEqual(row["event_type"], "SHORTAGE")
        self.assertEqual(row["amount_cents"], -yuan_to_cents("12.00"))
        self.assertEqual(row["shift_id"], sid)
        # 封存数字
        s = self.conn.execute("SELECT * FROM shifts WHERE shift_id=?", (sid,)).fetchone()
        self.assertEqual(s["expected_cash_cents"], yuan_to_cents("242"))
        self.assertEqual(s["actual_cash_cents"], yuan_to_cents("230"))
        self.assertEqual(s["shortage_cents"], -yuan_to_cents("12.00"))
        # 账实勾稽：应存 + 短款 = 实点
        self.assertEqual(s["expected_cash_cents"] + s["shortage_cents"],
                         s["actual_cash_cents"])

    def test_cash_reversal_reduces_expected_cash(self):
        sid = self.open_shift(float_yuan="200")
        paid = self.svc.pay_bill(self.zhao, "320102", "2026-09", "cash")  # +46
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "wrong_account", "错户")
        self.svc.review_reversal(self.qian, rid, True)  # 现金退回 -46
        report = self.svc.shift_report(self.zhao, sid)
        self.assertEqual(report["cash_collected"], yuan_to_cents("46"))
        self.assertEqual(report["cash_reversed"], -yuan_to_cents("46"))
        self.assertEqual(report["expected_cash"], yuan_to_cents("200"))

    def test_electronic_not_in_cash_drawer(self):
        sid = self.open_shift(float_yuan="200")
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "alipay")
        report = self.svc.shift_report(self.zhao, sid)
        self.assertEqual(report["expected_cash"], yuan_to_cents("200"))
        self.assertEqual(report["electronic"]["alipay"], yuan_to_cents("42"))

    def test_cannot_close_with_pending_receipt(self):
        sid = self.open_shift(float_yuan="200")
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "wechat",
                          simulate="pay_delay")
        with self.assertRaises(ServiceError):
            self.svc.close_shift(self.zhao, sid, yuan_to_cents("200"))
        # 同步回执后可以交班
        self.svc.sync_pending_payments(self.zhao)
        self.svc.close_shift(self.zhao, sid, yuan_to_cents("200"))

    def test_closed_shift_amounts_frozen(self):
        sid = self.open_shift(float_yuan="200")
        paid = self.svc.pay_bill(self.zhao, "320102", "2026-09", "cash")
        self.svc.close_shift(self.zhao, sid, yuan_to_cents("246"))
        # 结班后再批准一笔针对本班收款的冲正，封存应存不被改写
        rid = self.svc.request_reversal(self.zhao, paid["txn_id"], "other", "结班后发现")
        self.svc.review_reversal(self.qian, rid, True)
        report = self.svc.shift_report(self.zhao, sid)
        self.assertEqual(report["expected_cash"], yuan_to_cents("246"))
        self.assertEqual(report["actual_cash"], yuan_to_cents("246"))


if __name__ == "__main__":
    import unittest
    unittest.main()
