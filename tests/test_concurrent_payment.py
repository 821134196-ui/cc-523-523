"""同一账单的两个并发支付不得使应收变负。

用多线程 + 各自独立连接（与 Web 多线程形态一致）同时缴同一账单，
BEGIN IMMEDIATE 串行化账单重算：恰有一笔成功，另一笔被拒，待缴停在 0。
"""
import threading
from pathlib import Path

from water.adapters import PaymentAdapter, WaterAdapter
from water.db import connect
from water.money import yuan_to_cents
from water.service import ServiceError, WaterService
from tests.helpers import ServiceCase


class TestConcurrentPayment(ServiceCase):
    def _worker(self, results, channel, barrier, simulate="normal"):
        # 独立连接，模拟另一个终端/线程
        conn = connect(self.db_path)
        try:
            svc = WaterService(conn, self.receipt_dir, self.payment, self.water)
            op = svc.login("zhao")
            barrier.wait()  # 尽量同时冲进 pay_bill
            try:
                r = svc.pay_bill(op, "320101", "2026-09", channel, simulate)
                results.append(("ok", r["pay_status"]))
            except ServiceError as e:
                results.append(("reject", str(e)))
            except Exception as e:  # 锁等待/数据库错误也算失败，但要可见
                results.append(("error", f"{type(e).__name__}: {e}"))
        finally:
            conn.close()

    def test_two_concurrent_payments_one_wins(self):
        self.open_shift()
        self.assertEqual(self.due(), yuan_to_cents("42.00"))
        results: list = []
        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=self._worker,
                              args=(results, "cash", barrier))
        t2 = threading.Thread(target=self._worker,
                              args=(results, "alipay", barrier))
        t1.start(); t2.start(); t1.join(10); t2.join(10)

        self.assertEqual(len(results), 2, results)
        statuses = sorted(s for s, _ in results)
        # 恰好一笔成功，一笔被业务规则拒绝（不允许出现数据库 error）
        self.assertEqual(statuses[0], "ok")
        self.assertEqual(statuses[1], "reject", results)

        # 关键断言：应收不为负，且恰好等于 0
        due = self.due()
        self.assertEqual(due, 0, f"应收异常：{due}")
        paid = self.svc.bill_state(self.bill_id())["paid"]
        self.assertEqual(paid, yuan_to_cents("42.00"))
        # 成功支付只有一笔
        wins = self.conn.execute(
            "SELECT COUNT(*) c FROM ledger WHERE bill_id=? AND event_type='PAYMENT' "
            "AND pay_status='SUCCESS'", (self.bill_id(),)).fetchone()["c"]
        self.assertEqual(wins, 1)

    def test_many_concurrent_payments_still_non_negative(self):
        self.open_shift()
        n = 8
        results: list = []
        barrier = threading.Barrier(n)
        threads = [
            threading.Thread(target=self._worker,
                             args=(results, "alipay", barrier))
            for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        wins = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(wins), 1, results)
        self.assertNotIn("error", [s for s, _ in results])
        self.assertEqual(self.due(), 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
