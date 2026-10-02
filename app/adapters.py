"""模拟适配器：支付渠道与水务回执。

- PaymentAdapter：按 pay_ref 幂等；同一 pay_ref 的重复扣款请求只返回一次成功。
- WaterAdapter：登记销账回执；可模拟延迟（先 PENDING，后由对账轮询补登）。

两者均不落业务库，仅维护进程内状态，方便测试重置。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


class PaymentError(Exception):
    pass


@dataclass
class PaymentResult:
    pay_ref: str
    status: str  # SUCCESS / PENDING / FAILED
    channel: str


class PaymentAdapter:
    """模拟电子支付 / 现金缴款渠道。

    现金渠道无外部扣款，直接 SUCCESS；电子渠道可能返回 PENDING。
    以 pay_ref 去重：重复回执不产生第二笔。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, PaymentResult] = {}
        # 测试钩子：把某些 pay_ref 置为延迟到账
        self.delay_refs: set[str] = set()

    def charge(self, pay_ref: str, amount: int, channel: str) -> PaymentResult:
        if amount <= 0:
            raise PaymentError("支付金额必须为正")
        with self._lock:
            if pay_ref in self._seen:
                # 重复回执：原样返回首次结果，绝不重复扣款
                return self._seen[pay_ref]
            if channel == "CASH":
                result = PaymentResult(pay_ref, "SUCCESS", "CASH")
            elif pay_ref in self.delay_refs:
                result = PaymentResult(pay_ref, "PENDING", "ELECTRONIC")
            else:
                result = PaymentResult(pay_ref, "SUCCESS", "ELECTRONIC")
            self._seen[pay_ref] = result
            return result

    def refresh(self, pay_ref: str) -> PaymentResult | None:
        """轮询延迟支付的渠道状态；模拟时延迟单在 refresh 时转为成功。"""
        with self._lock:
            cur = self._seen.get(pay_ref)
            if cur is None:
                return None
            if cur.status == "PENDING":
                cur.status = "SUCCESS"
            return cur


@dataclass
class WaterReceipt:
    water_ref: str
    bill_id: str
    amount: int
    accepted: bool


class WaterAdapter:
    """模拟水务公司销账接口。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accepted: dict[str, WaterReceipt] = {}
        # 测试钩子：延迟回执的账单（销账请求先挂起）
        self.delay_bills: set[str] = set()
        self._pending: set[str] = set()

    def submit(self, bill_id: str, amount: int, water_ref: str) -> WaterReceipt | None:
        """提交销账。返回 None 表示水务回执延迟，需稍后 poll。"""
        with self._lock:
            if water_ref in self._accepted:
                return self._accepted[water_ref]
            if bill_id in self.delay_bills:
                self._pending.add(bill_id)
                return None
            receipt = WaterReceipt(water_ref, bill_id, amount, True)
            self._accepted[water_ref] = receipt
            return receipt

    def poll(self, bill_id: str, amount: int, water_ref: str) -> WaterReceipt | None:
        """补取延迟的水务回执。演示中 poll 即成功。"""
        with self._lock:
            if water_ref in self._accepted:
                return self._accepted[water_ref]
            if bill_id in self._pending:
                self._pending.discard(bill_id)
                receipt = WaterReceipt(water_ref, bill_id, amount, True)
                self._accepted[water_ref] = receipt
                return receipt
            return None
