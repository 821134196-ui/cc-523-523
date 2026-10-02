"""零依赖 Web 服务：http.server + sqlite3。

启动：python -m app.web （或 run.sh）
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, templates as T
from .adapters import PaymentAdapter, WaterAdapter
from .services import AuthzError, BusinessError, Service

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("WATERFEE_DB", os.path.join(_HERE, "data", "waterfee.db"))
RECEIPT_DIR = os.environ.get("WATERFEE_RECEIPTS", os.path.join(_HERE, "data", "receipts"))
SECRET_FILE = os.path.join(_HERE, "data", ".secret")

# 进程级单例：模拟渠道的延迟状态在内存中（演示/测试用）
PAYMENTS = PaymentAdapter()
WATER = WaterAdapter()


def _secret() -> bytes:
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            return f.read()
    secret = os.urandom(32)
    with open(SECRET_FILE, "wb") as f:
        f.write(secret)
    return secret


def _sign(uid: str) -> str:
    return hmac.new(_secret(), uid.encode(), hashlib.sha256).hexdigest()


class Kernel:
    """进程内核：每线程独立 SQLite 连接，共享线程安全的模拟适配器。"""

    def __init__(self, db_path: str = DB_PATH,
                 payments: PaymentAdapter | None = None,
                 water: WaterAdapter | None = None,
                 receipt_dir: str = RECEIPT_DIR):
        self.db_path = db_path
        self.payments = payments or PaymentAdapter()
        self.water = water or WaterAdapter()
        self.receipt_dir = receipt_dir
        self._local = threading.local()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        init_conn = db.connect(db_path)
        db.init_db(init_conn)
        init_conn.close()

    def app(self) -> "App":
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.db_path)
            self._local.conn = conn
        return App(conn, payments=self.payments, water=self.water,
                   receipt_dir=self.receipt_dir)


def parse_yuan(text: str) -> int:
    try:
        whole, frac = (text.strip().split(".") + [""])[:2] if "." in text else (text, "")
        frac = (frac + "00")[:2]
        return int(whole) * 100 + int(frac or 0)
    except ValueError:
        raise BusinessError("金额格式不正确，示例：12.34")


class App(Service):
    def user(self, uid: str):
        return self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def sites(self):
        return self.conn.execute("SELECT * FROM sites ORDER BY code").fetchall()

    def all_users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY role, id").fetchall()

    def water_confirmed(self, ev) -> bool:
        if ev["water_ref"]:
            return True
        return self.conn.execute(
            "SELECT 1 FROM ledger_events WHERE confirms_id=? AND kind='WATER_CONFIRM'",
            (ev["id"],),
        ).fetchone() is not None

    def shift_totals(self, shift) -> dict:
        rows = self.conn.execute(
            """
            SELECT * FROM ledger_events
            WHERE site_code=? AND operator_id=? AND created_at >= ?
              AND kind IN ('PAYMENT','REVERSAL','SHORTAGE_REPAY')
            """,
            (shift["site_code"], shift["operator_id"], shift["opened_at"]),
        ).fetchall()
        cash = [r for r in rows if r["channel"] == "CASH"]
        electronic = sum(r["amount"] for r in rows if r["channel"] == "ELECTRONIC")
        return {
            "expected": sum(r["amount"] for r in cash),
            "electronic": electronic,
            "count": len(rows),
            "n_pay": sum(1 for r in rows if r["kind"] == "PAYMENT"),
            "n_rev": sum(1 for r in rows if r["kind"] == "REVERSAL"),
            "diff": None,
        }


class Handler(BaseHTTPRequestHandler):
    kernel: Kernel = None  # 由 main 注入

    @property
    def svc(self) -> "App":
        if not hasattr(self, "_svc"):
            self._svc = self.kernel.app()
        return self._svc

    def log_message(self, fmt, *args):
        pass  # 静默；演示时取消注释可看请求

    # ------------------------------------------------------------ 基础工具

    def _send(self, html: str, status: int = 200, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, cookies=None):
        self.send_response(303)
        self.send_header("Location", path)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    @property
    def user(self):
        if not hasattr(self, "_user"):
            self._user = None
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "uid" in cookie and "sig" in cookie:
                uid = cookie["uid"].value
                if hmac.compare_digest(cookie["sig"].value, _sign(uid)):
                    self._user = self.svc.user(uid)
        return self._user

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return parsed, urllib.parse.parse_qs(parsed.query)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _flash(self, q):
        return q.get("msg", [""])[0], q.get("err", [""])[0], q.get("info", [""])[0]

    # ------------------------------------------------------------ 路由

    def do_GET(self):
        parsed, q = self._query()
        path = parsed.path
        try:
            if path == "/":
                return self.home(q)
            if path == "/login":
                return self._send(T.login_view(self.svc.all_users()))
            if path == "/logout":
                return self._redirect("/", [
                    "uid=; Max-Age=0; Path=/", "sig=; Max-Age=0; Path=/"])
            if path == "/pay":
                return self.pay_form(q)
            if path.startswith("/receipt/"):
                return self.receipt(path.rsplit("/", 1)[-1])
            if path == "/reversals":
                return self.reversals(q)
            if path == "/shift":
                return self.shift(q)
            if path == "/cash":
                return self.cash(q)
            self._send_404()
        except (BusinessError, AuthzError) as ex:
            self._send(self._wrap_error(str(ex)), 400)

    def do_POST(self):
        parsed, _ = self._query()
        path = parsed.path
        form = self._form()
        try:
            if path == "/login":
                uid = form.get("user_id", "")
                if self.svc.user(uid):
                    return self._redirect("/", [
                        f"uid={uid}; Path=/; HttpOnly",
                        f"sig={_sign(uid)}; Path=/; HttpOnly"])
                return self._send(T.login_view(self.svc.all_users(),
                                               err="用户不存在"), 400)
            if self.user is None:
                raise AuthzError("请先登录")
            if path == "/pay":
                return self.do_pay(form)
            if path == "/reversal/request":
                return self.do_reversal_request(form)
            if path.startswith("/reversal/") and path.endswith("/decide"):
                rid = path.split("/")[2]
                return self.do_decide(rid, form)
            if path == "/sync":
                stats = self.svc.sync_receipts()
                info = (f"补登支付到账 {stats['paid_confirmed']} 笔、"
                        f"水务回执 {stats['water_confirmed']} 笔（重复回执自动忽略）")
                return self._redirect("/reversals?info=" + urllib.parse.quote(info))
            if path == "/shift/open":
                sid = self.svc.open_shift(self.user)
                return self._redirect("/shift?msg=" +
                                      urllib.parse.quote(f"已开班 {sid}"))
            if path == "/shift/close":
                return self.do_close(form)
            self._send_404()
        except (BusinessError, AuthzError) as ex:
            self._send(self._wrap_error(str(ex)), 400)

    def _wrap_error(self, msg: str) -> str:
        body = f'<div class="card"><p>{T.esc(msg)}</p><a class="btn" href="/">返回首页</a></div>'
        return T.page("操作未成功", body, user=self.user, nav=T.nav_for(self.user), err=msg)

    def _send_404(self):
        self._send(self._wrap_error("页面不存在"), 404)

    # ------------------------------------------------------------ 页面处理

    def home(self, q):
        account_no = q.get("account_no", [""])[0].strip()
        msg, err, info = self._flash(q)
        if not account_no:
            return self._send(T.search_view(err=err, msg=msg))
        try:
            hh, bills = self.svc.list_bills(account_no)
            receipts = self.svc.list_household_receipts(hh["id"])
            return self._send(T.search_view(
                account_no, hh, bills, receipts=receipts, user=self.user,
                err=err, info=info))
        except BusinessError as ex:
            return self._send(T.search_view(
                account_no, err=str(ex), user=self.user), 400)

    def pay_form(self, q):
        if self.user is None:
            return self._redirect("/login")
        if self.user["role"] != "operator":
            raise AuthzError("只有操作员可以代缴收款")
        account_no = q.get("account_no", [""])[0]
        period = q.get("period", [""])[0]
        hh, bill = self.svc.get_bill_for_payment(self.user, account_no, period)
        return self._send(T.pay_view(hh, bill, self.user))

    def do_pay(self, form):
        if self.user["role"] != "operator":
            raise AuthzError("只有操作员可以代缴收款")
        account_no = form["account_no"]
        period = form["period"]
        amount = parse_yuan(form.get("amount_yuan", ""))
        channel = form.get("channel", "CASH")
        ev = self.svc.pay(
            self.user, account_no, period, amount, channel,
            delay_payment=form.get("delay_pay") == "1",
            delay_water=form.get("delay_water") == "1",
        )
        msg = "收款成功，已出具收据。"
        if ev["pay_status"] == "PENDING":
            msg = "渠道回执延迟：缴款已登记为待确认，到账后补登，不会重复入账。"
        elif ev["water_ref"] is None:
            msg = "支付成功，但水务销账回执延迟，稍后在审批页一键补登。"
        return self._redirect(f"/receipt/{ev['id']}?msg=" +
                              urllib.parse.quote(msg))

    def receipt(self, event_id):
        ev = self.svc.payment_detail(event_id)
        if ev is None:
            raise BusinessError("凭证不存在")
        rev = self.svc.reversal_full(event_id)
        parsed, q = self._query()
        msg, err, _ = self._flash(q)
        water_ok = self.svc.water_confirmed(ev)
        return self._send(T.receipt_view(ev, rev, water_ok, self.user, msg, err))

    def do_reversal_request(self, form):
        ev_id = form["original_id"]
        req_id = self.svc.request_reversal(
            self.user, ev_id, form.get("reason", ""))
        return self._redirect(
            f"/receipt/{ev_id}?msg=" +
            urllib.parse.quote(f"冲正申请 {req_id} 已提交，等待主管批准"))

    def reversals(self, q):
        if self.user is None or self.user["role"] != "supervisor":
            raise AuthzError("冲正审批仅主管可用")
        msg, err, info = self._flash(q)
        pending = self.svc.list_pending_reversals()
        processed = self.svc.conn.execute(
            """
            SELECT r.*, h.account_no, b.period, o.receipt_no AS orig_receipt
            FROM reversal_requests r
            JOIN ledger_events o ON o.id = r.original_id
            JOIN bills b ON b.id = o.bill_id
            JOIN households h ON h.id = b.household_id
            WHERE r.status != 'PENDING' ORDER BY r.decided_at DESC LIMIT 30
            """
        ).fetchall()
        return self._send(T.reversals_view(
            self.user, pending, processed, err=err, msg=msg or info))

    def do_decide(self, rid, form):
        approve = form.get("approve") == "1"
        req = self.svc.conn.execute(
            "SELECT * FROM reversal_requests WHERE id=?", (rid,)).fetchone()
        self.svc.decide_reversal(self.user, rid, approve, form.get("note", ""))
        if approve:
            msg = f"已批准并生成负数冲正单（原交易 {req['original_id']} 保留）"
        else:
            msg = "已驳回该冲正申请"
        return self._redirect("/reversals?msg=" + urllib.parse.quote(msg))

    def shift(self, q):
        if self.user is None:
            return self._redirect("/login")
        msg, err, info = self._flash(q)
        if self.user["role"] == "supervisor":
            shifts = self.svc.list_shifts(self.user, q.get("site", [""])[0] or None)
            body = (
                '<div class="card"><p>主管视角：班次总览（操作员在各自站点开班/交班）。</p>'
                + "".join(
                    f"<p>{T.esc(s['id'])} ｜ {T.esc(s['site_code'])} ｜ "
                    f"{T.esc(s['opened_at'])} → {T.esc(s['closed_at'] or '进行中')} ｜ "
                    f"实点 {T.yuan(s['cash_counted']) if s['cash_counted'] is not None else '—'}</p>"
                    for s in shifts[:30])
                + "</div>"
            )
            return self._send(T.page("班次总览", body, user=self.user,
                                     nav=T.nav_for(self.user)))
        current = self.svc.current_shift(self.user)
        events, totals = [], {"expected": 0, "electronic": 0, "count": 0,
                              "n_pay": 0, "n_rev": 0, "diff": None}
        if current:
            events = self.svc.shift_events(current)
            totals = self.svc.shift_totals(current)
        recent = self.svc.list_shifts(self.user)
        return self._send(T.shift_view(
            self.user, current, events, totals, recent, err=err, msg=msg, info=info))

    def do_close(self, form):
        counted = parse_yuan(form.get("counted_yuan", "0"))
        result = self.svc.close_shift(self.user, counted, form.get("note", ""))
        if result["diff"] < 0:
            msg = (f"已交班。现金短款 {T.signed(result['diff'])} 元（负数：短款），"
                   f"已登记 SHORTAGE 事件 {result['shortage_id']}，不得删除流水。")
        elif result["diff"] > 0:
            msg = f"已交班。长款 +{T.yuan(result['diff'])} 元，请复核。"
        else:
            msg = "已交班，账实相符。"
        return self._redirect("/shift?msg=" + urllib.parse.quote(msg))

    def cash(self, q):
        if self.user is None:
            return self._redirect("/login")
        site = q.get("site", [""])[0] or None
        msg, err, _ = self._flash(q)
        # 操作员强制传其他站点参数 -> AuthzError，由全局处理返回 400 拒绝页
        records = self.svc.cash_records(self.user, site)
        effective_site = self.user["site_code"] if self.user["role"] == "operator" else site
        return self._send(T.cash_view(
            self.user, records, self.svc.sites(), effective_site or "", err=err))


def build_app(kernel: "Kernel | None" = None) -> "App":
    """主线程用：初始化 schema 并返回一个 App（播种/测试用）。"""
    kernel = kernel or Kernel()
    return kernel.app()


def main(host="127.0.0.1", port=8000, svc: "App | None" = None,
         kernel: "Kernel | None" = None):
    if kernel is None:
        kernel = Kernel(payments=PAYMENTS, water=WATER)
    if svc is not None:
        # 播种已在主线程完成；把其适配器交给内核，保证延迟状态共享
        kernel.payments = svc.payments
        kernel.water = svc.water
    Handler.kernel = kernel
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"村镇水费代缴系统已启动：http://{host}:{port}")
    print(f"数据库：{DB_PATH}")
    print(f"演示收据目录：{RECEIPT_DIR}")
    print("演示身份在 /login 选择；居民查账在首页输入户号（H1001/H1002/H2001/H3001）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    import sys

    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    main(port=p)
