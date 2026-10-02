"""模拟外部适配器：支付渠道与水务回执。

真实部署中这些是 HTTP 客户端；演示环境全部本地模拟，但保留与真实系统一致的
「请求—回执—延迟确认—重复送达」语义：
- 支付：同步下单，可能延迟回执（本地先用 PENDING 落账，之后 sync 补确认）；
- 水务：缴费后异步销账回执，可能延迟甚至拒绝（需重推）；
- 重复回执：同一 pay_ref 多次送达必须幂等（由 service 层 + 唯一索引保证）。
"""
import json
import uuid
from pathlib import Path


class PaymentAdapter:
    LABELS = {
        "cash": "现金",
        "alipay": "支付宝（电子）",
        "wechat": "微信（电子）",
        "bankcash": "银行代收（电子）",
    }

    def __init__(self, script_path: str | Path):
        self.script_path = Path(script_path)
        self._cache = None

    def _script(self) -> dict:
        if self._cache is None:
            self._cache = (
                json.loads(self.script_path.read_text("utf-8"))
                if self.script_path.exists()
                else {}
            )
        return self._cache

    def new_ref(self, method: str) -> str:
        prefix = "CASH" if method == "cash" else "PAY"
        return f"{prefix}{uuid.uuid4().hex[:16].upper()}"

    def pay_behavior(self, pay_ref: str) -> str:
        """success / delay / fail。

        演示用哨兵前缀（页面「模拟异常回执」生成）优先，其次查脚本，默认即时成功。
        """
        if pay_ref.startswith("PAYFAIL"):
            return "fail"
        if pay_ref.startswith("PAYDELAY"):
            return "delay"
        return self._script().get(pay_ref, {}).get("pay", "success")

    def water_behavior(self, pay_ref: str) -> str:
        """confirm / delay / reject。支付回执未到或水务延迟哨兵均为 delay。"""
        if pay_ref.startswith(("PAYDELAY", "WATDELAY")):
            return "delay"
        return self._script().get(pay_ref, {}).get("water", "confirm")

    def query_gateway(self, pay_ref: str) -> str:
        """轮询渠道侧最终状态：演示中 delay 的单子稍后查询一律成功。"""
        return {"delay": "SUCCESS", "success": "SUCCESS", "fail": "FAILED"}[
            self.pay_behavior(pay_ref)
        ]

    def refund(self, original_ref: str) -> str:
        """电子渠道冲正退款流水号。现金冲正不走这里（直接退回现金）。"""
        base = original_ref[3:] if original_ref.startswith("REF") else original_ref
        return f"REF{base}{uuid.uuid4().hex[:6].upper()}"


class WaterAdapter:
    """模拟水务公司销账接口：重推延迟单。"""

    def __init__(self, payment: PaymentAdapter):
        self.payment = payment

    def reconfirm(self, pay_ref: str) -> str:
        """重推延迟销账单。演示规则：脚本里标 delay 的，重推即 CONFIRMED。"""
        return {"delay": "CONFIRMED", "confirm": "CONFIRMED", "reject": "REJECTED"}[
            self.payment.water_behavior(pay_ref)
        ]
