"""账务服务：所有业务规则集中于此。

铁律：
1. ledger 只追加——更正只能新增负向冲正事件，绝不 DELETE/UPDATE 金额；
2. 冲正必须 related_txn 引用原交易，且先经主管批准；
3. 付款在 BEGIN IMMEDIATE 事务内重算应收，并发下应收不得变负；
4. 所有查询按 operator 所属 site_id 过滤，操作员只看得见本服务点现金；
5. 渠道回执按 pay_ref 唯一索引幂等，重复送达不产生第二条流水。
"""
import sqlite3
import uuid
from datetime import datetime

from .adapters import PaymentAdapter, WaterAdapter
from .money import cents_to_yuan
from .receipts import write_receipt_file


class ServiceError(Exception):
    """业务规则拒绝（4xx 语义）。"""


class AuthError(ServiceError):
    """越权 / 未登录。"""


REASONS = {
    "wrong_account": "输错户号",
    "duplicate": "重复缴费",
    "cross_period": "跨账期付款",
    "gateway": "支付/水务回执异常",
    "other": "其他原因",
}

# 模拟异常回执的哨兵流水号前缀（适配器识别）
_SIM_PREFIX = {
    "pay_delay": "PAYDELAY",    # 支付成功但渠道回执延迟
    "pay_fail": "PAYFAIL",      # 支付失败
    "water_delay": "WATDELAY",  # 支付即时成功、水务销账回执延迟
}


def new_txn_id() -> str:
    return "T" + datetime.now().strftime("%Y%m%d%H%M") + uuid.uuid4().hex[:6].upper()


def _receipt_no(txn_id: str) -> str:
    return "R" + txn_id[1:]


class WaterService:
    def __init__(self, conn: sqlite3.Connection, receipt_dir, payment: PaymentAdapter,
                 water: WaterAdapter):
        self.conn = conn
        self.receipt_dir = receipt_dir
        self.pay = payment
        self.water = water

    # ---------------------------------------------------------------- 会话
    def login(self, username: str) -> sqlite3.Row:
        op = self.conn.execute(
            "SELECT * FROM operators WHERE username=?", (username,)
        ).fetchone()
        if op is None:
            raise AuthError(f"用户不存在：{username}")
        return op

    def operator(self, operator_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM operators WHERE operator_id=?", (operator_id,)
        ).fetchone()
        if row is None:
            raise AuthError("操作员不存在")
        return row

    def _require_site(self, op: sqlite3.Row, site_id: str):
        if op["site_id"] != site_id:
            raise AuthError("站点隔离：仅可操作本服务点记录")

    # ---------------------------------------------------------------- 查询
    def lookup_household(self, op: sqlite3.Row, account_no: str) -> dict:
        h = self.conn.execute(
            "SELECT * FROM households WHERE account_no=?", (account_no,)
        ).fetchone()
        if h is None:
            raise ServiceError(f"户号 {account_no} 不存在，请核对后重新输入")
        self._require_site(op, h["site_id"])
        bills = self.conn.execute(
            "SELECT * FROM bills WHERE account_no=? ORDER BY period", (account_no,)
        ).fetchall()
        result = {"household": h, "bills": []}
        for b in bills:
            state = self.bill_state(b["bill_id"])
            result["bills"].append({
                "bill": b,
                "due_cents": state["due"],
                "paid_cents": state["paid"],
                "status": state["status"],
            })
        return result

    def bill_state(self, bill_id: int) -> dict:
        """应收、已缴、状态。FAILED/PENDING 支付不占用额度；冲正为负自动回补。"""
        b = self.conn.execute("SELECT * FROM bills WHERE bill_id=?", (bill_id,)).fetchone()
        if b is None:
            raise ServiceError("账单不存在")
        row = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN pay_status='SUCCESS'
                                       THEN amount_cents ELSE 0 END),0) AS eff
               FROM ledger WHERE bill_id=?""",
            (bill_id,),
        ).fetchone()
        paid = row["eff"]
        due = b["amount_cents"] - paid
        if due <= 0:
            status = "PAID"
        elif paid > 0:
            status = "PARTIAL"
        else:
            status = "UNPAID"
        return {"bill": b, "paid": paid, "due": due, "status": status}

    def payment_vouchers(self, op: sqlite3.Row, account_no: str) -> list:
        """居民查询本户已缴凭证（成功收款 + 其冲正/作废状态）。"""
        h = self.conn.execute(
            "SELECT site_id FROM households WHERE account_no=?", (account_no,)
        ).fetchone()
        if h is None:
            raise ServiceError("户号不存在")
        self._require_site(op, h["site_id"])
        return self.conn.execute(
            """SELECT l.*, r.receipt_no, r.voided, r.linked_receipt_no
               FROM ledger l LEFT JOIN receipts r ON r.txn_id=l.txn_id
               WHERE l.account_no=? AND l.event_type IN ('PAYMENT','REVERSAL')
               ORDER BY l.id""",
            (account_no,),
        ).fetchall()

    # ---------------------------------------------------------------- 代缴
    def current_shift(self, op: sqlite3.Row) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM shifts WHERE operator_id=? AND closed_at IS NULL "
            "ORDER BY shift_id DESC",
            (op["operator_id"],),
        ).fetchone()

    def _make_pay_ref(self, method: str, simulate: str) -> str:
        if simulate == "normal":
            return self.pay.new_ref(method)
        if method == "cash":
            raise ServiceError("现金渠道不模拟延迟/失败回执")
        if simulate not in _SIM_PREFIX:
            raise ServiceError("未知模拟场景")
        return _SIM_PREFIX[simulate] + uuid.uuid4().hex[3:16].upper()

    def pay_bill(self, op: sqlite3.Row, account_no: str, period: str,
                 channel: str, simulate: str = "normal") -> dict:
        """代收一笔水费。channel: cash/alipay/wechat/bankcash。

        simulate（仅模拟适配器）：normal / pay_delay / pay_fail / water_delay。
        户号、账期、金额全部来自服务端账单，页面确认区只作展示，不信前端传值。
        """
        hh = self.conn.execute(
            "SELECT * FROM households WHERE account_no=?", (account_no,)
        ).fetchone()
        if hh is None:
            raise ServiceError(f"户号 {account_no} 不存在，缴费失败")
        self._require_site(op, hh["site_id"])
        bill = self.conn.execute(
            "SELECT * FROM bills WHERE account_no=? AND period=?", (account_no, period)
        ).fetchone()
        if bill is None:
            raise ServiceError(f"户号 {account_no} 无 {period} 账期账单")

        shift = self.current_shift(op)
        if shift is None:
            raise ServiceError("本班次尚未开班，不能收款（请先开班）")

        method = "cash" if channel == "cash" else "electronic"
        pay_ref = self._make_pay_ref(method, simulate)
        txn_id = new_txn_id()
        receipt_no = _receipt_no(txn_id)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 锁内重算，杜绝两个并发支付把同一账单缴成负数
            state = self.bill_state(bill["bill_id"])
            due = state["due"]
            if due <= 0:
                raise ServiceError("该账单已结清，重复缴费已被拒绝")
            amount = bill["amount_cents"]
            if amount != due:
                raise ServiceError(
                    f"应缴金额与待缴余额不一致（应缴 ¥{cents_to_yuan(amount)}，"
                    f"待缴 ¥{cents_to_yuan(due)}），请先处理历史冲正"
                )

            if method == "cash":
                pay_status, water_status = "SUCCESS", "CONFIRMED"
            else:
                pay_status = {
                    "success": "SUCCESS", "delay": "PENDING", "fail": "FAILED",
                }[self.pay.pay_behavior(pay_ref)]
                wb = self.pay.water_behavior(pay_ref)
                water_status = {"confirm": "CONFIRMED", "delay": "PENDING",
                                "reject": "REJECTED"}[wb]
                if pay_status == "FAILED":
                    water_status = "PENDING"

            conn.execute(
                """INSERT INTO ledger
                   (txn_id, site_id, operator_id, event_type, method, channel,
                    amount_cents, account_no, bill_id, period, pay_ref,
                    pay_status, water_status, shift_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (txn_id, op["site_id"], op["operator_id"], "PAYMENT", method, channel,
                 amount, account_no, bill["bill_id"], period, pay_ref,
                 pay_status, water_status, shift["shift_id"]),
            )
            # 只有收款确实成功才出收据；渠道回执延迟的，待回执送达后补开
            if pay_status == "SUCCESS":
                conn.execute(
                    "INSERT INTO receipts (receipt_no, txn_id, kind, file_path) "
                    "VALUES (?,?, 'NORMAL', '')",
                    (receipt_no, txn_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if pay_status == "SUCCESS":
            path = write_receipt_file(conn, self.receipt_dir, receipt_no)
            conn.execute("UPDATE receipts SET file_path=? WHERE receipt_no=?",
                         (path, receipt_no))
        return {
            "txn_id": txn_id, "pay_ref": pay_ref,
            "amount_cents": bill["amount_cents"],
            "pay_status": pay_status, "water_status": water_status,
            "receipt_no": receipt_no if pay_status == "SUCCESS" else None,
        }

    # ------------------------------------------------- 支付/水务回执处理
    def receive_gateway_receipt(self, pay_ref: str, gateway_status: str) -> dict:
        """渠道回执送达（含重复送达）。按 pay_ref 定位事件，天然幂等。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            t = conn.execute("SELECT * FROM ledger WHERE pay_ref=?", (pay_ref,)).fetchone()
            if t is None:
                raise ServiceError(f"未知渠道流水 {pay_ref}")
            duplicate = t["pay_status"] != "PENDING"
            new_receipt = False
            if not duplicate:
                new_status = "SUCCESS" if gateway_status == "SUCCESS" else "FAILED"
                conn.execute(
                    "UPDATE ledger SET pay_status=? WHERE txn_id=?",
                    (new_status, t["txn_id"]),
                )
                if new_status == "SUCCESS":
                    # 回执到达时若水务可确认则一并确认；哨兵 delay 的继续等销账回执
                    wb = self.pay.water_behavior(pay_ref)
                    if wb != "delay":
                        conn.execute(
                            "UPDATE ledger SET water_status=? WHERE txn_id=?",
                            ("CONFIRMED" if wb == "confirm" else "REJECTED",
                             t["txn_id"]),
                        )
                    exists = conn.execute(
                        "SELECT 1 FROM receipts WHERE txn_id=?", (t["txn_id"],)
                    ).fetchone()
                    if not exists:
                        receipt_no = _receipt_no(t["txn_id"])
                        conn.execute(
                            "INSERT INTO receipts (receipt_no, txn_id, kind, file_path) "
                            "VALUES (?,?, 'NORMAL','')",
                            (receipt_no, t["txn_id"]),
                        )
                        new_receipt = receipt_no
            final = conn.execute(
                "SELECT pay_status FROM ledger WHERE txn_id=?", (t["txn_id"],)
            ).fetchone()["pay_status"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # 重复送达：直接返回既有状态，不再补开任何凭证
        if new_receipt:
            path = write_receipt_file(conn, self.receipt_dir, new_receipt)
            conn.execute("UPDATE receipts SET file_path=? WHERE receipt_no=?",
                         (path, new_receipt))
        return {"txn_id": t["txn_id"], "duplicate": duplicate, "pay_status": final}

    def sync_pending_payments(self, op: sqlite3.Row) -> list:
        """轮询所有回执延迟的电子支付（站点隔离）。"""
        rows = self.conn.execute(
            "SELECT * FROM ledger WHERE site_id=? AND pay_status='PENDING'",
            (op["site_id"],),
        ).fetchall()
        return [
            self.receive_gateway_receipt(t["pay_ref"],
                                         self.pay.query_gateway(t["pay_ref"]))
            for t in rows
        ]

    def pending_water(self, op: sqlite3.Row) -> list:
        return self.conn.execute(
            "SELECT * FROM ledger WHERE site_id=? AND water_status='PENDING' "
            "AND pay_status='SUCCESS'",
            (op["site_id"],),
        ).fetchall()

    def reconfirm_water(self, op: sqlite3.Row, txn_id: str) -> str:
        """水务销账延迟：重推。"""
        t = self.conn.execute("SELECT * FROM ledger WHERE txn_id=?", (txn_id,)).fetchone()
        if t is None:
            raise ServiceError("流水不存在")
        self._require_site(op, t["site_id"])
        if t["water_status"] != "PENDING":
            raise ServiceError("该单水务状态不是待确认")
        result = self.water.reconfirm(t["pay_ref"])
        self.conn.execute(
            "UPDATE ledger SET water_status=? WHERE txn_id=?", (result, txn_id)
        )
        return result

    # ------------------------------------------------------------ 冲正链
    def find_payment(self, op: sqlite3.Row, txn_id: str) -> sqlite3.Row:
        t = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (txn_id,)
        ).fetchone()
        if t is None or t["event_type"] != "PAYMENT":
            raise ServiceError("原交易不存在或不是收款流水")
        self._require_site(op, t["site_id"])
        return t

    def request_reversal(self, op: sqlite3.Row, original_txn_id: str,
                         reason_code: str, reason_detail: str = "") -> int:
        if reason_code not in REASONS:
            raise ServiceError("未知冲正原因")
        t = self.find_payment(op, original_txn_id)
        if t["pay_status"] != "SUCCESS":
            raise ServiceError("支付未成功的单据不能冲正（失败单自动释放额度，无需冲正）")
        if t["water_status"] == "PENDING":
            raise ServiceError("水务销账仍在确认中，请待回执明确后再冲正")
        existing = self.conn.execute(
            "SELECT status FROM approval_requests WHERE original_txn_id=? "
            "AND status IN ('PENDING','APPROVED')",
            (original_txn_id,),
        ).fetchone()
        if existing:
            raise ServiceError(
                "该原单已存在待批冲正，不能重复申请"
                if existing["status"] == "PENDING"
                else "该原单已冲正，不能再次冲正"
            )
        cur = self.conn.execute(
            """INSERT INTO approval_requests
               (reversal_txn_id, original_txn_id, site_id, requested_by,
                reason_code, reason_detail)
               VALUES (NULL,?,?,?,?,?)""",
            (original_txn_id, op["site_id"], op["operator_id"],
             reason_code, reason_detail),
        )
        return cur.lastrowid

    def list_approvals(self, op: sqlite3.Row, status: str | None = None) -> list:
        sql = ("SELECT ar.*, o.display_name AS reviewer_name, "
               "op.display_name AS requester_name "
               "FROM approval_requests ar "
               "JOIN operators op ON op.operator_id=ar.requested_by "
               "LEFT JOIN operators o ON o.operator_id=ar.reviewer_id "
               "WHERE ar.site_id=?")
        args: list = [op["site_id"]]
        if status:
            sql += " AND ar.status=?"
            args.append(status)
        sql += " ORDER BY ar.id DESC"
        return self.conn.execute(sql, args).fetchall()

    def review_reversal(self, supervisor: sqlite3.Row, request_id: int,
                        approve: bool, comment: str = "") -> dict:
        if supervisor["role"] != "supervisor":
            raise AuthError("只有主管可以审批冲正")
        req = self.conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (request_id,)
        ).fetchone()
        if req is None:
            raise ServiceError("审批单不存在")
        self._require_site(supervisor, req["site_id"])
        if req["status"] != "PENDING":
            raise ServiceError("该审批单已处理，不能重复审批")

        if not approve:
            self.conn.execute(
                "UPDATE approval_requests SET status='REJECTED', reviewer_id=?, "
                "review_comment=?, reviewed_at=datetime('now','localtime') WHERE id=?",
                (supervisor["operator_id"], comment, request_id),
            )
            return {"approved": False}

        orig = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (req["original_txn_id"],)
        ).fetchone()
        orig_receipt = self.conn.execute(
            "SELECT receipt_no FROM receipts WHERE txn_id=? AND kind='NORMAL'",
            (orig["txn_id"],),
        ).fetchone()
        if orig_receipt is None:
            raise ServiceError("原单收据缺失，无法建立冲正链")

        txn_id = new_txn_id()
        receipt_no = _receipt_no(txn_id)
        orig_receipt_no = orig_receipt["receipt_no"]
        # 电子渠道走退款流水；现金冲正直接在现金账内扣减（退现金给居民）
        new_ref = None if orig["method"] == "cash" else self.pay.refund(orig["pay_ref"])

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            dup = conn.execute(
                "SELECT 1 FROM approval_requests WHERE original_txn_id=? "
                "AND id!=? AND status='APPROVED'",
                (orig["txn_id"], request_id),
            ).fetchone()
            if dup:
                raise ServiceError("该原单已被另一审批冲正")

            conn.execute(
                """INSERT INTO ledger
                   (txn_id, site_id, operator_id, event_type, method, channel,
                    amount_cents, account_no, bill_id, period, pay_ref,
                    pay_status, water_status, related_txn, reason_code, memo,
                    shift_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (txn_id, supervisor["site_id"], supervisor["operator_id"], "REVERSAL",
                 orig["method"], orig["channel"], -orig["amount_cents"],
                 orig["account_no"], orig["bill_id"], orig["period"], new_ref,
                 "SUCCESS", "CONFIRMED", orig["txn_id"],
                 req["reason_code"], req["reason_detail"], None),
            )
            conn.execute(
                """INSERT INTO receipts
                   (receipt_no, txn_id, kind, file_path, linked_receipt_no)
                   VALUES (?,?,'REDLINE','',?)""",
                (receipt_no, txn_id, orig_receipt_no),
            )
            # 原收据作废（不删文件），链到红字单
            conn.execute(
                "UPDATE receipts SET voided=1, linked_receipt_no=? WHERE receipt_no=?",
                (receipt_no, orig_receipt_no),
            )
            conn.execute(
                "UPDATE approval_requests SET status='APPROVED', reviewer_id=?, "
                "review_comment=?, reviewed_at=datetime('now','localtime'), "
                "reversal_txn_id=? WHERE id=?",
                (supervisor["operator_id"], comment, txn_id, request_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # 事务提交后再落收据文件（红字单 + 重写盖作废章的原单）
        redline_path = write_receipt_file(conn, self.receipt_dir, receipt_no)
        conn.execute("UPDATE receipts SET file_path=? WHERE receipt_no=?",
                     (redline_path, receipt_no))
        orig_path = write_receipt_file(conn, self.receipt_dir, orig_receipt_no)
        conn.execute("UPDATE receipts SET file_path=? WHERE receipt_no=?",
                     (orig_path, orig_receipt_no))
        return {"approved": True, "txn_id": txn_id, "receipt_no": receipt_no,
                "orig_receipt_no": orig_receipt_no}

    def approval_detail(self, op: sqlite3.Row, request_id: int) -> dict:
        req = self.conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (request_id,)
        ).fetchone()
        if req is None:
            raise ServiceError("审批单不存在")
        self._require_site(op, req["site_id"])
        orig = self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (req["original_txn_id"],)
        ).fetchone()
        rev = None
        if req["reversal_txn_id"]:
            rev = self.conn.execute(
                "SELECT * FROM ledger WHERE txn_id=?", (req["reversal_txn_id"],)
            ).fetchone()
        orig_r = self.conn.execute(
            "SELECT * FROM receipts WHERE txn_id=?", (orig["txn_id"],)
        ).fetchone()
        rev_r = None
        if rev:
            rev_r = self.conn.execute(
                "SELECT * FROM receipts WHERE txn_id=?", (rev["txn_id"],)
            ).fetchone()
        return {"req": req, "orig": orig, "rev": rev,
                "orig_receipt": orig_r, "rev_receipt": rev_r}

    # ---------------------------------------------------------------- 交班
    def open_shift(self, op: sqlite3.Row, opening_float_cents: int = 0) -> int:
        if opening_float_cents < 0:
            raise ServiceError("开班备用金不能为负")
        if self.current_shift(op) is not None:
            raise ServiceError("已有未结班次，请先交班")
        cur = self.conn.execute(
            "INSERT INTO shifts (site_id, operator_id, opening_float_cents) "
            "VALUES (?,?,?)",
            (op["site_id"], op["operator_id"], opening_float_cents),
        )
        return cur.lastrowid

    def _shift_lines(self, shift_id: int) -> list:
        """本班流水：本班产生的事件 + 结班前批准的、针对本班收款的现金冲正。"""
        lines = self.conn.execute(
            "SELECT * FROM ledger WHERE shift_id=? ORDER BY id", (shift_id,)
        ).fetchall()
        ids = {l["id"] for l in lines}
        extra = self.conn.execute(
            """SELECT l.* FROM ledger l
               JOIN ledger o ON o.txn_id=l.related_txn
               JOIN shifts s ON s.shift_id=o.shift_id
               WHERE l.event_type='REVERSAL' AND o.shift_id=?
                 AND (s.closed_at IS NULL OR l.created_at<=s.closed_at)""",
            (shift_id,),
        ).fetchall()
        merged = sorted(list(lines) + [e for e in extra if e["id"] not in ids],
                        key=lambda r: r["id"])
        return merged

    def shift_report(self, op: sqlite3.Row, shift_id: int) -> dict:
        s = self.conn.execute("SELECT * FROM shifts WHERE shift_id=?", (shift_id,)).fetchone()
        if s is None:
            raise ServiceError("班次不存在")
        self._require_site(op, s["site_id"])
        if op["role"] != "supervisor" and s["operator_id"] != op["operator_id"]:
            raise AuthError("仅可核对本人班次")

        lines = self._shift_lines(shift_id)
        cash_in = sum(l["amount_cents"] for l in lines
                      if l["method"] == "cash" and l["event_type"] == "PAYMENT"
                      and l["pay_status"] == "SUCCESS")
        cash_rev = sum(l["amount_cents"] for l in lines
                       if l["method"] == "cash" and l["event_type"] == "REVERSAL")
        elec: dict = {}
        for l in lines:
            if l["method"] == "electronic" and l["pay_status"] == "SUCCESS":
                elec[l["channel"]] = elec.get(l["channel"], 0) + l["amount_cents"]
        pending = [l for l in self.conn.execute(
            "SELECT * FROM ledger WHERE shift_id=? AND pay_status='PENDING'",
            (shift_id,)).fetchall()]

        closed = s["closed_at"] is not None
        # 已结班次以封存数字为准，不再受后续事件影响
        opening = s["opening_float_cents"]
        expected = s["expected_cash_cents"] if closed else opening + cash_in + cash_rev
        return {
            "shift": s, "lines": lines,
            "cash_collected": cash_in,
            "cash_reversed": cash_rev,
            "electronic": elec,
            "expected_cash": expected,
            "pending": pending,
            "actual_cash": s["actual_cash_cents"],
            "shortage": s["shortage_cents"],
            "shortage_txn": s["shortage_txn_id"],
        }

    def close_shift(self, op: sqlite3.Row, shift_id: int,
                    actual_cash_cents: int, note: str = "") -> dict:
        report = self.shift_report(op, shift_id)  # 含权限校验
        if report["shift"]["closed_at"]:
            raise ServiceError("班次已结")
        if actual_cash_cents < 0:
            raise ServiceError("实点现金不能为负")
        if report["pending"]:
            raise ServiceError("仍有电子支付回执未到，请先同步后再交班")
        expected = report["expected_cash"]
        diff = actual_cash_cents - expected  # 负=短款，正=长款

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            shortage_txn = None
            if diff != 0:
                t = new_txn_id()
                shortage_txn = t
                # 短款/长款本身也是一条有符号账务事件，留下冲正链供追查
                conn.execute(
                    """INSERT INTO ledger
                       (txn_id, site_id, operator_id, event_type, method, channel,
                        amount_cents, account_no, bill_id, period, pay_ref,
                        pay_status, water_status, related_txn, reason_code, memo,
                        shift_id)
                       VALUES (?,?,?,?, 'cash','cash', ?, NULL,NULL,NULL,NULL,
                               'SUCCESS','CONFIRMED',NULL,'shortage',?,?)""",
                    (t, op["site_id"], op["operator_id"], "SHORTAGE", diff,
                     f"交班核对差异（负数为短款，正数为长款）；备注：{note or '无'}",
                     shift_id),
                )
            conn.execute(
                "UPDATE shifts SET closed_at=datetime('now','localtime'), "
                "expected_cash_cents=?, actual_cash_cents=?, shortage_cents=?, "
                "shortage_txn_id=?, note=? WHERE shift_id=?",
                (expected, actual_cash_cents, diff, shortage_txn, note, shift_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"expected": expected, "actual": actual_cash_cents,
                "diff": diff, "shortage_txn": shortage_txn}

    def list_shifts(self, op: sqlite3.Row) -> list:
        sql = ("SELECT s.*, o.display_name FROM shifts s JOIN operators o "
               "ON o.operator_id=s.operator_id WHERE s.site_id=?")
        args: list = [op["site_id"]]
        if op["role"] != "supervisor":
            sql += " AND s.operator_id=?"
            args.append(op["operator_id"])
        sql += " ORDER BY s.shift_id DESC"
        return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------- 现金/流水浏览
    def cash_records(self, op: sqlite3.Row) -> list:
        """本站现金记录（站点隔离）：含收款、现金冲正、短款。"""
        return self.conn.execute(
            """SELECT l.*, h.account_code FROM ledger l
               LEFT JOIN households h ON h.account_no=l.account_no
               WHERE l.site_id=? AND l.method='cash' ORDER BY l.id DESC""",
            (op["site_id"],),
        ).fetchall()

    def recent_txns(self, op: sqlite3.Row, limit: int = 30) -> list:
        return self.conn.execute(
            """SELECT l.*, h.account_code FROM ledger l
               LEFT JOIN households h ON h.account_no=l.account_no
               WHERE l.site_id=? ORDER BY l.id DESC LIMIT ?""",
            (op["site_id"], limit),
        ).fetchall()

    def txn(self, txn_id: str) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (txn_id,)
        ).fetchone()
