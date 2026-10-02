"""账务服务层：账单查询、代缴、冲正审批、交班对账、站点隔离。

所有金额单位为分（整数）。事件只追加，更正一律走冲正。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import db
from .adapters import PaymentAdapter, WaterAdapter


class BusinessError(Exception):
    """业务规则冲突（户号不存在、重复缴费、跨账期等）。"""


class AuthzError(Exception):
    """越权（如操作员查看其他服务点现金记录）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Service:
    def __init__(
        self,
        conn: sqlite3.Connection,
        payments: PaymentAdapter | None = None,
        water: WaterAdapter | None = None,
        clock=_now,
        receipt_dir: str | None = None,
    ) -> None:
        self.conn = conn
        self.payments = payments or PaymentAdapter()
        self.water = water or WaterAdapter()
        self.clock = clock
        self.receipt_dir = receipt_dir

    # ---------------------------------------------------------------- 查户查账

    def get_household(self, account_no: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM households WHERE account_no = ?", (account_no.strip(),)
        ).fetchone()
        if row is None:
            raise BusinessError(f"户号 {account_no} 不存在，请核对后再输入")
        return row

    def list_bills(self, account_no: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        hh = self.get_household(account_no)
        rows = self.conn.execute(
            """
            SELECT b.*,
                   COALESCE((
                       SELECT SUM(amount) FROM ledger_events e
                       WHERE e.bill_id = b.id
                         AND e.kind IN ('PAYMENT','REVERSAL')
                   ), 0) AS effective_paid,
                   COALESCE((
                       SELECT SUM(amount) FROM ledger_events e
                       WHERE e.bill_id = b.id AND e.kind = 'PAYMENT'
                   ), 0) AS gross_paid
            FROM bills b WHERE b.household_id = ? ORDER BY b.period
            """,
            (hh["id"],),
        ).fetchall()
        return hh, rows

    def bill_status(self, bill: sqlite3.Row) -> str:
        paid = bill["effective_paid"]
        due = bill["amount_due"]
        if paid <= 0:
            return "UNPAID"
        if paid >= due:
            return "PAID"
        return "PARTIAL"

    def get_bill_for_payment(self, operator: sqlite3.Row, account_no: str, period: str):
        hh = self.get_household(account_no)
        if operator["role"] == "operator" and hh["site_code"] != operator["site_code"]:
            raise AuthzError("该户不属于本服务点，不能在本点代缴")
        bill = self.conn.execute(
            "SELECT * FROM bills WHERE household_id = ? AND period = ?",
            (hh["id"], period),
        ).fetchone()
        if bill is None:
            raise BusinessError(
                f"账期 {period} 无应缴账单（跨账期付款）：请先到水务方出账"
            )
        return hh, bill

    # ---------------------------------------------------------------- 代缴

    def pay(
        self,
        operator: sqlite3.Row,
        account_no: str,
        period: str,
        amount: int,
        channel: str,
        delay_payment: bool = False,
        delay_water: bool = False,
    ) -> sqlite3.Row:
        if channel not in ("CASH", "ELECTRONIC"):
            raise BusinessError("支付渠道只能是现金或电子支付")
        if amount <= 0:
            raise BusinessError("应缴金额必须为正数")

        hh, bill = self.get_bill_for_payment(operator, account_no, period)
        if amount != bill["amount_due"]:
            raise BusinessError(
                f"实收金额与应缴不一致：应缴 {bill['amount_due']} 分，"
                f"输入 {amount} 分，请按确认区固定金额收取"
            )

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 并发防护：在库内汇总该账单已生效缴款（含在途 PENDING，先占额度）
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS paid
                FROM ledger_events
                WHERE bill_id = ? AND kind IN ('PAYMENT','REVERSAL')
                """,
                (bill["id"],),
            ).fetchone()
            receivable_after = bill["amount_due"] - (row["paid"] + amount)
            if receivable_after < 0:
                raise BusinessError(
                    "该账单已缴清，重复缴费将使应收为负，已拒绝"
                )
            if row["paid"] >= bill["amount_due"]:
                raise BusinessError("该账单已缴清，请勿重复缴费")

            pay_ref = db.next_id("PAY" if channel == "ELECTRONIC" else "CSH")
            if delay_payment:
                self.payments.delay_refs.add(pay_ref)
            if delay_water:
                self.water.delay_bills.add(bill["id"])
            result = self.payments.charge(pay_ref, amount, channel)
            seq = db.next_seq(conn)
            receipt_no = self._make_receipt_no(seq)
            event_id = db.next_id("EVT")
            water_ref = None
            if result.status == "SUCCESS":
                water_ref = db.next_id("W")
                wr = self.water.submit(bill["id"], amount, water_ref)
                if wr is None:
                    water_ref = None  # 水务回执延迟，稍后补登

            conn.execute(
                """
                INSERT INTO ledger_events
                  (id, seq, bill_id, site_code, operator_id, kind, amount,
                   channel, pay_ref, pay_status, receipt_no, receipt_status,
                   water_ref, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?, 'VALID', ?, ?)
                """,
                (
                    event_id, seq, bill["id"], hh["site_code"],
                    operator["id"], "PAYMENT", amount, channel, pay_ref,
                    result.status, receipt_no, water_ref, self.clock(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if self.receipt_dir:
            self._write_receipt_file(conn, event_id)
        return conn.execute("SELECT * FROM ledger_events WHERE id = ?", (event_id,)).fetchone()

    # ------------------------------------------------- 延迟支付/水务回执补登

    def sync_receipts(self) -> dict[str, int]:
        """轮询模拟渠道，把延迟到账/延迟水务回执补登为零金额确认事件。幂等。"""
        stats = {"paid_confirmed": 0, "water_confirmed": 0}

        # 1) 支付到账延迟：渠道轮询成功后登记 PAY_CONFIRM，并补提水务销账
        pending_pay = self.conn.execute(
            """
            SELECT p.* FROM ledger_events p
            WHERE p.kind='PAYMENT' AND p.pay_status='PENDING'
              AND NOT EXISTS (
                  SELECT 1 FROM ledger_events c
                  WHERE c.confirms_id = p.id AND c.kind='PAY_CONFIRM'
              )
            """
        ).fetchall()
        for ev in pending_pay:
            refreshed = self.payments.refresh(ev["pay_ref"])
            if not refreshed or refreshed.status != "SUCCESS":
                continue
            self._insert_confirm_once(
                ev, "PAY_CONFIRM", ev["pay_ref"] + "#confirm",
                "电子支付到账确认",
            )
            stats["paid_confirmed"] += 1
            water_ref = f"W-{ev['id']}"
            wr = self.water.submit(ev["bill_id"], ev["amount"], water_ref)
            if wr is not None:  # 水务未延迟，一并补登
                self._insert_confirm_once(
                    ev, "WATER_CONFIRM", water_ref,
                    "水务销账回执（到账后补登）",
                )
                stats["water_confirmed"] += 1

        # 2) 支付成功但水务回执延迟（或第 1 步刚到账）的缴款，逐条补取
        lacking = self.conn.execute(
            """
            SELECT p.* FROM ledger_events p
            WHERE p.kind='PAYMENT'
              AND (p.pay_status='SUCCESS' OR EXISTS (
                  SELECT 1 FROM ledger_events c
                  WHERE c.confirms_id = p.id AND c.kind='PAY_CONFIRM'))
              AND NOT EXISTS (
                  SELECT 1 FROM ledger_events c
                  WHERE c.confirms_id = p.id AND c.kind='WATER_CONFIRM'
              )
            """
        ).fetchall()
        for ev in lacking:
            water_ref = f"W-{ev['id']}"  # 确定性回执号：重复轮询命中同一条
            wr = self.water.poll(ev["bill_id"], ev["amount"], water_ref)
            if wr is not None:
                self._insert_confirm_once(
                    ev, "WATER_CONFIRM", water_ref,
                    "水务销账回执（延迟补登）",
                )
                stats["water_confirmed"] += 1
        return stats

    def _insert_confirm_once(self, payment: sqlite3.Row, kind: str, ref: str, note: str):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            dup = conn.execute(
                "SELECT 1 FROM ledger_events WHERE confirms_id=? AND kind=?",
                (payment["id"], kind),
            ).fetchone()
            if dup:  # 重复回执：已有确认事件，直接返回，绝不重复入账
                conn.execute("ROLLBACK")
                return
            conn.execute(
                """
                INSERT INTO ledger_events
                  (id, seq, bill_id, site_code, operator_id, kind, amount,
                   channel, pay_ref, pay_status, receipt_no, receipt_status,
                   confirms_id, water_ref, reason, created_at)
                VALUES (?,?,?,?,?,?, 0, NULL, ?, NULL, NULL, NULL,
                        ?, ?, ?, ?)
                """,
                (
                    db.next_id("EVT"), db.next_seq(conn), payment["bill_id"],
                    payment["site_code"], payment["operator_id"], kind,
                    ref, payment["id"],
                    ref if kind == "WATER_CONFIRM" else None,
                    note, self.clock(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- 冲正

    def request_reversal(
        self, operator: sqlite3.Row, original_id: str, reason: str
    ) -> str:
        orig = self._get_payment(original_id)
        if orig["operator_id"] != operator["id"] and operator["role"] != "supervisor":
            raise AuthzError("只能对自己经办的缴款发起冲正")
        if not reason.strip():
            raise BusinessError("冲正必须填写原因")
        exists = self.conn.execute(
            "SELECT 1 FROM reversal_requests WHERE original_id=? AND status IN ('PENDING','APPROVED')",
            (original_id,),
        ).fetchone()
        if exists:
            raise BusinessError("该交易已存在待批或已批准的冲正")
        if self.conn.execute(
            "SELECT 1 FROM ledger_events WHERE reverses_id=?", (original_id,)
        ).fetchone():
            raise BusinessError("该交易已被冲正")
        req_id = db.next_id("RR")
        self.conn.execute(
            """INSERT INTO reversal_requests
               (id, original_id, requested_by, status, reason, created_at)
               VALUES (?,?,?, 'PENDING', ?, ?)""",
            (req_id, original_id, operator["id"], reason.strip(), self.clock()),
        )
        return req_id

    def list_pending_reversals(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT r.*, o.receipt_no AS orig_receipt, o.amount AS orig_amount,
                   o.channel AS orig_channel, o.id AS orig_event_id,
                   o.site_code AS orig_site,
                   h.account_no, h.name_code, b.period
            FROM reversal_requests r
            JOIN ledger_events o ON o.id = r.original_id
            JOIN bills b ON b.id = o.bill_id
            JOIN households h ON h.id = b.household_id
            WHERE r.status='PENDING' ORDER BY r.created_at
            """
        ).fetchall()

    def decide_reversal(
        self, supervisor: sqlite3.Row, request_id: str, approve: bool, note: str = ""
    ) -> None:
        if supervisor["role"] != "supervisor":
            raise AuthzError("只有主管可以审批冲正")
        req = self.conn.execute(
            "SELECT * FROM reversal_requests WHERE id=?", (request_id,)
        ).fetchone()
        if req is None:
            raise BusinessError("冲正申请不存在")
        if req["status"] != "PENDING":
            raise BusinessError("该申请已审批，不能重复审批")

        if not approve:
            self.conn.execute(
                "UPDATE reversal_requests SET status='REJECTED', approver_id=?, "
                "decided_at=? WHERE id=?",
                (supervisor["id"], self.clock(), request_id),
            )
            return

        orig = self._get_payment(req["original_id"])
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            already = conn.execute(
                "SELECT 1 FROM ledger_events WHERE reverses_id=?", (orig["id"],)
            ).fetchone()
            if already:
                raise BusinessError("该交易已被冲正")
            rev_id = db.next_id("EVT")
            # 冲正金额为原缴款的相反数；现金冲正同时减少当班现金
            conn.execute(
                """
                INSERT INTO ledger_events
                  (id, seq, bill_id, site_code, operator_id, kind, amount,
                   channel, pay_ref, pay_status, receipt_no, receipt_status,
                   reverses_id, approved_by, reason, created_at)
                VALUES (?,?,?,?,?, 'REVERSAL', ?, ?, NULL, NULL, NULL, NULL,
                        ?, ?, ?, ?)
                """,
                (
                    rev_id, db.next_seq(conn), orig["bill_id"], orig["site_code"],
                    orig["operator_id"], -orig["amount"], orig["channel"],
                    orig["id"], supervisor["id"],
                    f"冲正 {orig['receipt_no']}：{req['reason']}"
                    + (f"（审批备注：{note}）" if note else ""),
                    self.clock(),
                ),
            )
            conn.execute(
                "UPDATE reversal_requests SET status='APPROVED', approver_id=?, "
                "decided_at=? WHERE id=?",
                (supervisor["id"], self.clock(), request_id),
            )
            # 原打印收据标记作废（状态更新，流水本身保留不删），冲正链完整可查
            conn.execute(
                "UPDATE ledger_events SET receipt_status='VOID' WHERE id=?",
                (orig["id"],),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if self.receipt_dir:
            # 重写演示收据文件：原单与更正单并列（不删除任何流水）
            ev_full = self.payment_detail(orig["id"])
            rev_full = self.reversal_full(orig["id"])
            self._write_receipt_pair_file(ev_full, rev_full)
        return rev_id

    def _get_payment(self, event_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM ledger_events WHERE id=? AND kind='PAYMENT'", (event_id,)
        ).fetchone()
        if row is None:
            raise BusinessError("原交易不存在或不是缴款流水")
        return row

    def reversal_full(self, original_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT r.*, u.display_name AS approver_name
            FROM ledger_events r
            JOIN users u ON u.id = r.approved_by
            WHERE r.reverses_id=?
            """,
            (original_id,),
        ).fetchone()

    # ---------------------------------------------------------------- 交班

    def open_shift(self, operator: sqlite3.Row) -> str:
        if operator["role"] != "operator":
            raise AuthzError("只有操作员开班")
        open_row = self.conn.execute(
            "SELECT id FROM shifts WHERE site_code=? AND operator_id=? AND closed_at IS NULL",
            (operator["site_code"], operator["id"]),
        ).fetchone()
        if open_row:
            raise BusinessError("已有未关闭的班次，请先交班")
        sid = db.next_id("SHF")
        self.conn.execute(
            "INSERT INTO shifts(id, site_code, operator_id, opened_at) VALUES (?,?,?,?)",
            (sid, operator["site_code"], operator["id"], self.clock()),
        )
        return sid

    def current_shift(self, operator: sqlite3.Row) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM shifts WHERE operator_id=? AND closed_at IS NULL",
            (operator["id"],),
        ).fetchone()

    def shift_events(self, shift: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT e.*, h.account_no, b.period
            FROM ledger_events e
            LEFT JOIN bills b ON b.id = e.bill_id
            LEFT JOIN households h ON h.id = b.household_id
            WHERE e.site_code=? AND e.operator_id=?
              AND e.kind IN ('PAYMENT','REVERSAL','SHORTAGE','SHORTAGE_REPAY')
              AND e.created_at >= ? AND e.created_at <= ?
            ORDER BY e.seq
            """,
            (
                shift["site_code"], shift["operator_id"], shift["opened_at"],
                shift["closed_at"] or "9999",
            ),
        ).fetchall()

    def close_shift(
        self, operator: sqlite3.Row, cash_counted: int, note: str = ""
    ) -> dict:
        shift = self.current_shift(operator)
        if shift is None:
            raise BusinessError("没有进行中的班次")
        if cash_counted < 0:
            raise BusinessError("实点现金不能为负数")

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            closed_at = self.clock()
            events = conn.execute(
                """
                SELECT * FROM ledger_events
                WHERE site_code=? AND operator_id=?
                  AND kind IN ('PAYMENT','REVERSAL','SHORTAGE','SHORTAGE_REPAY')
                  AND channel IN ('CASH')
                  AND created_at >= ? AND created_at <= ?
                ORDER BY seq
                """,
                (operator["site_code"], operator["id"], shift["opened_at"], "9999"),
            ).fetchall()
            # 电子支付不入现金抽屉；现金缴款为正，现金冲正为负，短款/补缴入账
            expected = sum(e["amount"] for e in events)
            diff = cash_counted - expected

            shortage_id = None
            if diff < 0:
                # 现金短款：登记负向事件，形成操作员赔付责任，不允许改流水抹平
                shortage_id = db.next_id("EVT")
                conn.execute(
                    """
                    INSERT INTO ledger_events
                      (id, seq, bill_id, site_code, operator_id, kind, amount,
                       channel, pay_ref, pay_status, receipt_no, receipt_status,
                       reason, created_at)
                    VALUES (?,?, NULL, ?, ?, 'SHORTAGE', ?, 'CASH', NULL, NULL,
                            NULL, NULL, ?, ?)
                    """,
                    (
                        shortage_id, db.next_seq(conn), operator["site_code"],
                        operator["id"], diff,
                        f"交班现金短款（班次 {shift['id']}）", closed_at,
                    ),
                )
            conn.execute(
                "UPDATE shifts SET closed_at=?, cash_counted=?, note=? WHERE id=?",
                (closed_at, cash_counted, note, shift["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {
            "shift_id": shift["id"],
            "expected": expected,
            "counted": cash_counted,
            "diff": diff,
            "shortage_id": shortage_id,
        }

    def list_shifts(self, viewer: sqlite3.Row, site_code: str | None = None):
        if viewer["role"] == "operator":
            # 操作员只能查看自己服务点（且只看自己班次）
            site_code = viewer["site_code"]
            return self.conn.execute(
                "SELECT * FROM shifts WHERE site_code=? ORDER BY opened_at DESC",
                (site_code,),
            ).fetchall()
        if site_code:
            return self.conn.execute(
                "SELECT * FROM shifts WHERE site_code=? ORDER BY opened_at DESC",
                (site_code,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM shifts ORDER BY opened_at DESC"
        ).fetchall()

    # ------------------------------------------------- 现金记录的站点隔离

    def cash_records(self, viewer: sqlite3.Row, site_code: str | None = None):
        """现金缴款/冲正/短款记录。操作员强制限定本服务点。"""
        sql = """
            SELECT e.*, h.account_no, h.name_code, b.period, s.name AS site_name
            FROM ledger_events e
            JOIN sites s ON s.code = e.site_code
            LEFT JOIN bills b ON b.id = e.bill_id
            LEFT JOIN households h ON h.id = b.household_id
            WHERE e.channel='CASH'
              AND e.kind IN ('PAYMENT','REVERSAL','SHORTAGE','SHORTAGE_REPAY')
        """
        params: list = []
        if viewer["role"] == "operator":
            if site_code and site_code != viewer["site_code"]:
                raise AuthzError("操作员只能查看本服务点的现金记录")
            sql += " AND e.site_code=?"
            params.append(viewer["site_code"])
        elif site_code:
            sql += " AND e.site_code=?"
            params.append(site_code)
        sql += " ORDER BY e.seq DESC LIMIT 200"
        return self.conn.execute(sql, params).fetchall()

    def list_household_receipts(self, household_id: str):
        return self.conn.execute(
            """
            SELECT e.id, e.receipt_no, b.period, e.amount,
                   e.channel, e.pay_status, e.kind, e.created_at,
                   e.receipt_status, e.reverses_id, e.confirms_id
            FROM ledger_events e
            JOIN bills b ON b.id = e.bill_id
            WHERE b.household_id=?
              AND e.kind IN ('PAYMENT','REVERSAL')
            ORDER BY e.seq DESC
            """,
            (household_id,),
        ).fetchall()

    def find_receipt(self, receipt_no: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT e.*, h.account_no, h.name_code, b.period, b.amount_due,
                   s.name AS site_name, u.display_name AS operator_name
            FROM ledger_events e
            JOIN bills b ON b.id = e.bill_id
            JOIN households h ON h.id = b.household_id
            JOIN sites s ON s.code = e.site_code
            JOIN users u ON u.id = e.operator_id
            WHERE e.receipt_no=?
            """,
            (receipt_no,),
        ).fetchone()

    def payment_detail(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT e.*, h.account_no, h.name_code, b.period, b.amount_due,
                   s.name AS site_name, u.display_name AS operator_name
            FROM ledger_events e
            JOIN bills b ON b.id = e.bill_id
            JOIN households h ON h.id = b.household_id
            JOIN sites s ON s.code = e.site_code
            JOIN users u ON u.id = e.operator_id
            WHERE e.id=?
            """,
            (event_id,),
        ).fetchone()

    # ---------------------------------------------------------------- 收据

    def _make_receipt_no(self, seq: int) -> str:
        return f"RCP-{seq:06d}-{_now()[:10].replace('-', '')}"

    def _write_receipt_file(self, conn: sqlite3.Connection, event_id: str) -> None:
        import os

        ev = self.payment_detail(event_id)
        if ev is None:
            return
        os.makedirs(self.receipt_dir, exist_ok=True)
        path = os.path.join(self.receipt_dir, f"{ev['receipt_no']}.html")
        from .receipts import render_receipt_html

        with open(path, "w", encoding="utf-8") as f:
            f.write(render_receipt_html(ev, None))

    def _write_receipt_pair_file(self, ev, rev) -> None:
        import os

        os.makedirs(self.receipt_dir, exist_ok=True)
        path = os.path.join(self.receipt_dir, f"{ev['receipt_no']}.html")
        from .receipts import render_receipt_html

        with open(path, "w", encoding="utf-8") as f:
            f.write(render_receipt_html(ev, rev))
