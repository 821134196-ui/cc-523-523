"""重复回执必须幂等：同一渠道流水多次送达，只确认一次、只出一张收据。"""
import sqlite3

from water.money import yuan_to_cents
from water.service import ServiceError
from tests.helpers import ServiceCase


class TestDuplicateReceipt(ServiceCase):
    def test_gateway_receipt_delivered_twice(self):
        self.open_shift()
        # 渠道回执延迟：先落 PENDING，不出收据
        r = self.svc.pay_bill(self.zhao, "320101", "2026-09",
                              "alipay", simulate="pay_delay")
        self.assertEqual(r["pay_status"], "PENDING")
        self.assertIsNone(r["receipt_no"])
        bill_id = self.bill_id()
        # PENDING 不占用应收
        self.assertEqual(self.svc.bill_state(bill_id)["due"], yuan_to_cents("42.00"))

        # 第一次回执送达：转 SUCCESS、补开收据
        first = self.svc.receive_gateway_receipt(r["pay_ref"], "SUCCESS")
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["pay_status"], "SUCCESS")
        receipts_after_first = self.conn.execute(
            "SELECT COUNT(*) c FROM receipts WHERE txn_id=?", (r["txn_id"],)
        ).fetchone()["c"]
        self.assertEqual(receipts_after_first, 1)

        # 第二次（甚至第三次）重复送达：幂等，状态不变、不补凭证
        second = self.svc.receive_gateway_receipt(r["pay_ref"], "SUCCESS")
        third = self.svc.receive_gateway_receipt(r["pay_ref"], "SUCCESS")
        self.assertTrue(second["duplicate"])
        self.assertTrue(third["duplicate"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM ledger WHERE pay_ref=?", (r["pay_ref"],)
            ).fetchone()["c"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM receipts WHERE txn_id=?", (r["txn_id"],)
            ).fetchone()["c"], 1)
        # 应收恰好被核减一次，不为负
        self.assertEqual(self.svc.bill_state(bill_id)["due"], 0)

    def test_pay_ref_unique_index_blocks_second_row(self):
        self.open_shift()
        r = self.svc.pay_bill(self.zhao, "320101", "2026-09", "wechat")
        # 绕过服务层直接插同 pay_ref，必须被唯一索引拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO ledger
                   (txn_id, site_id, operator_id, event_type, method, channel,
                    amount_cents, account_no, bill_id, period, pay_ref,
                    pay_status, water_status)
                   VALUES ('TDUP','HD','op_zhao','PAYMENT','electronic','wechat',
                           4200,'320101',?,'2026-09',?,'SUCCESS','CONFIRMED')""",
                (self.bill_id(), r["pay_ref"]))

    def test_paid_bill_second_payment_rejected(self):
        self.open_shift()
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")
        with self.assertRaises(ServiceError):
            self.svc.pay_bill(self.zhao, "320101", "2026-09", "alipay")

    def test_failed_payment_receipt_is_void_not_occupying(self):
        self.open_shift()
        r = self.svc.pay_bill(self.zhao, "320101", "2026-09",
                              "wechat", simulate="pay_fail")
        self.assertEqual(r["pay_status"], "FAILED")
        self.assertIsNone(r["receipt_no"])
        self.assertEqual(self.due(), yuan_to_cents("42.00"))
        # 失败后可重新缴成功
        ok = self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")
        self.assertEqual(ok["pay_status"], "SUCCESS")
        self.assertEqual(self.due(), 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
