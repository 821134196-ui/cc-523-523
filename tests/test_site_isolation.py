"""站点隔离：操作员只能查看/操作自己服务点的账单、现金、班次与审批。"""
from water.service import AuthError
from tests.helpers import ServiceCase


class TestSiteIsolation(ServiceCase):
    def test_cannot_lookup_other_site_household(self):
        with self.assertRaises(AuthError):
            self.svc.lookup_household(self.sun, "320101")   # HX 操作员查 HD 户
        with self.assertRaises(AuthError):
            self.svc.lookup_household(self.zhao, "410207")  # HD 操作员查 HX 户

    def test_cannot_pay_other_site_bill(self):
        self.svc.open_shift(self.sun)
        with self.assertRaises(AuthError):
            self.svc.pay_bill(self.sun, "320101", "2026-09", "cash")

    def test_cash_records_only_own_site(self):
        sid = self.svc.open_shift(self.zhao)
        self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")
        hd = self.svc.cash_records(self.zhao)
        hx = self.svc.cash_records(self.sun)
        self.assertTrue(any(r["account_no"] == "320101" for r in hd))
        self.assertEqual(hx, [])

    def test_cannot_view_or_close_other_operator_shift(self):
        sid = self.svc.open_shift(self.zhao)
        # 河西操作员完全不可见
        with self.assertRaises(AuthError):
            self.svc.shift_report(self.sun, sid)
        with self.assertRaises(AuthError):
            self.svc.close_shift(self.sun, sid, 0)
        # 同站主管可以查看
        report = self.svc.shift_report(self.qian, sid)
        self.assertEqual(report["shift"]["site_id"], "HD")

    def test_supervisor_cannot_review_other_site_request(self):
        self.svc.open_shift(self.zhao)
        r = self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")
        rid = self.svc.request_reversal(self.zhao, r["txn_id"], "other", "x")
        with self.assertRaises(AuthError):
            self.svc.review_reversal(self.li, rid, True)

    def test_approval_list_scoped_to_site(self):
        self.svc.open_shift(self.zhao)
        r = self.svc.pay_bill(self.zhao, "320101", "2026-09", "cash")
        self.svc.request_reversal(self.zhao, r["txn_id"], "other", "x")
        hd_pending = self.svc.list_approvals(self.qian)
        hx_pending = self.svc.list_approvals(self.li)
        self.assertEqual(len(hd_pending), 1)
        self.assertEqual(hx_pending, [])

    def test_voucher_query_isolated(self):
        with self.assertRaises(AuthError):
            self.svc.payment_vouchers(self.sun, "320101")


if __name__ == "__main__":
    import unittest
    unittest.main()
