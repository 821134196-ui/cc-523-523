"""村镇便民点水费代缴系统 —— 本地 Web（仅标准库）。

启动：python3 app.py
"""
import html
import os
import sqlite3
import threading
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from water.adapters import PaymentAdapter, WaterAdapter
from water.db import connect, init_db
from water.money import cents_chinese, cents_to_yuan, fmt_money, fmt_signed, yuan_to_cents
from water.receipts import render_receipt
from water.seed import seed_if_empty
from water.service import REASONS, AuthError, ServiceError, WaterService

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "water.db")
RECEIPT_DIR = os.path.join(BASE_DIR, "data", "receipts")
SCRIPT_PATH = os.path.join(BASE_DIR, "data", "gateway_script.json")
STATIC_DIR = os.path.join(BASE_DIR, "web")

PAY_STATUS_CN = {"PENDING": "回执处理中", "SUCCESS": "支付成功", "FAILED": "支付失败"}
WATER_CN = {"PENDING": "销账确认中", "CONFIRMED": "已销账", "REJECTED": "销账被拒"}
EVENT_CN = {"PAYMENT": "代收水费", "REVERSAL": "冲正更正", "SHORTAGE": "交班差异"}
REASON_CN = dict(REASONS, shortage="交班现金短款/长款")

sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_tls = threading.local()


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def money(c) -> str:
    return esc(fmt_money(c or 0))


def signed(c) -> str:
    """负数：符号与中文文字同时出现。"""
    if c is None or c >= 0:
        return money(c or 0)
    return f'<span class="neg">-¥{esc(cents_to_yuan(-c))} <span class="word">（负数：{esc(cents_chinese(c)[1:])}）</span></span>'


def badge(text, kind=""):
    return f'<span class="badge {kind}">{esc(text)}</span>'


def pay_badge(status):
    return {"PENDING": badge(PAY_STATUS_CN[status], "warn"),
            "SUCCESS": badge(PAY_STATUS_CN[status], "ok"),
            "FAILED": badge(PAY_STATUS_CN[status], "bad")}[status]


def water_badge(status):
    return {"PENDING": badge(WATER_CN[status], "warn"),
            "CONFIRMED": badge(WATER_CN[status], "ok"),
            "REJECTED": badge(WATER_CN[status], "bad")}[status]


def method_cn(channel, method):
    return PaymentAdapter.LABELS.get(channel or method, method or "—")


def status_bill(s):
    return {"PAID": badge("已结清", "ok"),
            "PARTIAL": badge("部分缴纳", "warn"),
            "UNPAID": badge("待缴费", "info")}[s]


class AppState:
    def __init__(self):
        seeded = seed_if_empty(DB_PATH, RECEIPT_DIR, SCRIPT_PATH)
        self.payment = PaymentAdapter(SCRIPT_PATH)
        self.water = WaterAdapter(self.payment)
        self.seeded = seeded


STATE: AppState | None = None


def db() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = connect(DB_PATH)
        _tls.conn = conn
        _tls.svc = WaterService(conn, RECEIPT_DIR, STATE.payment, STATE.water)
    return conn


def svc() -> WaterService:
    """每线程独立连接与服务实例（SQLite 连接不可跨线程共享）。"""
    s = getattr(_tls, "svc", None)
    if s is None:
        conn = db()
        s = _tls.svc
    return s


# ----------------------------------------------------------------- 页面模板
def layout(session, active, body, title=""):
    op = session.get("op") if session else None
    if op is None:
        return body
    nav = [
        ("/", "账单查询", "home"),
        ("/cash", "现金记录", "cash"),
        ("/reversal", "冲正申请", "reversal"),
        ("/approvals", "冲正审批", "approvals"),
        ("/shifts", "交班对账", "shifts"),
    ]
    links = "".join(
        f'<a href="{u}"{" class=active" if k == active else ""}>{t}</a>'
        for u, t, k in nav
    )
    role = "主管" if op["role"] == "supervisor" else "操作员"
    site = db().execute("SELECT name FROM sites WHERE site_id=?",
                              (op["site_id"],)).fetchone()["name"]
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title or '水费代缴')}｜村镇便民点</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<header class="topbar">
  <span class="brand">💧 水费代缴</span>
  <nav>{links}</nav>
  <span class="user">{esc(site)} · {esc(op['display_name'])}（{role}）
    <form method="post" action="/logout"><button type="submit">切换</button></form>
  </span>
</header>
<main>{body}</main>
</body></html>"""


def flash_html(session):
    msgs = session.pop("_flash", [])
    out = []
    for kind, text in msgs:
        out.append(f'<div class="flash {kind}">{esc(text)}</div>')
    return "".join(out)


def page_login():
    rows = db().execute(
        "SELECT o.*, s.name site_name FROM operators o JOIN sites s USING(site_id) "
        "ORDER BY o.site_id, o.role DESC, o.username"
    ).fetchall()
    btns = []
    for r in rows:
        role = "主管" if r["role"] == "supervisor" else "操作员"
        btns.append(
            f'<button formaction="/login" name="username" value="{esc(r["username"])}">'
            f'{esc(r["display_name"])}｜{esc(r["site_name"])}｜{role}'
            f'<small>账号 {esc(r["username"])}</small></button>'
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>登录｜村镇便民点水费代缴</title><link rel="stylesheet" href="/static/style.css">
</head><body><main class="login-wrap">
<div class="brandblock">💧 村镇便民点水费代缴</div>
<div class="sub2 muted">请选择本班操作员登录（演示环境免密）</div>
<div class="card"><form method="post">
  <div class="login-accounts">{''.join(btns)}</div>
</form>
<p class="muted" style="font-size:12px;margin-top:16px">
河东村：赵小娥（操作员）/ 钱主管（审批）；河西村：孙大柱 / 李主管 —— 用于站点隔离演示。</p>
</div></main></body></html>"""


def page_home(session, q: str, result=None, error=None):
    op = session["op"]
    flash = flash_html(session)
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    cards = ""
    if result is not None:
        h = result["household"]
        rows = []
        for item in result["bills"]:
            b, st = item["bill"], item["status"]
            due = item["due_cents"]
            pay_cell = money(item["paid_cents"])
            if st == "PAID":
                action = '<span class="muted">—</span>'
            else:
                action = (f'<a class="btn small" href="/pay?account_no={esc(h["account_no"])}'
                          f'&period={esc(b["period"])}">代缴</a>')
            rows.append(
                f"<tr><td>{esc(b['period'])}</td><td class=num>{money(b['amount_cents'])}</td>"
                f"<td class=num>{pay_cell}</td><td class=num>{money(due)}</td>"
                f"<td>{status_bill(st)}</td><td>{action}</td></tr>"
            )
        vouchers = svc().payment_vouchers(op, h["account_no"])
        vrows = []
        for v in vouchers:
            if v["event_type"] == "PAYMENT":
                amt = money(v["amount_cents"])
                ref = f'<a href="/receipt?no={esc(v["receipt_no"])}">{esc(v["receipt_no"])}</a>' if v["receipt_no"] else "—"
                if v["voided"]:
                    ref += ' ' + badge("已作废", "bad")
                chain = f'<a href="/reversal?txn={esc(v["txn_id"])}">申请冲正</a>'
                if v["pay_status"] != "SUCCESS":
                    chain = pay_badge(v["pay_status"])
            else:
                amt = signed(v["amount_cents"])
                ref = f'<a href="/receipt?no={esc(v["receipt_no"])}">{esc(v["receipt_no"])}</a>'
                chain = f'红字更正，引用原单 <code>{esc(v["related_txn"])}</code>'
            vrows.append(
                f"<tr><td class=nowrap>{esc(v['created_at'])}</td>"
                f"<td>{EVENT_CN[v['event_type']]}</td><td>{esc(v['period'])}</td>"
                f"<td class=num>{amt}</td><td>{esc(method_cn(v['channel'], v['method']))}</td>"
                f"<td>{pay_badge(v['pay_status'])} {water_badge(v['water_status'])}</td>"
                f"<td class=nowrap>{ref}</td><td>{chain}</td></tr>"
            )
        vtable = ("<table><thead><tr><th>时间</th><th>类型</th><th>账期</th><th class=num>金额</th>"
                  "<th>渠道</th><th>状态</th><th>收据</th><th>冲正链</th></tr></thead><tbody>"
                  + "".join(vrows) + "</tbody></table>") if vrows else '<p class="muted">本户暂无缴费记录。</p>'
        cards = f"""
<h2>户号 {esc(h['account_no'])}｜{esc(h['account_code'])}（{esc(h['village'])}）的账单</h2>
<div class="card"><table><thead><tr>
  <th>账期</th><th class=num>应缴金额</th><th class=num>已缴</th><th class=num>待缴</th>
  <th>状态</th><th>操作</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>本户已缴凭证与冲正记录</h2>
<div class="card">{vtable}</div>"""

    # 待处理回执
    pend_pay = db().execute(
        "SELECT * FROM ledger WHERE site_id=? AND pay_status='PENDING'",
        (op["site_id"],)).fetchall()
    pend_water = db().execute(
        "SELECT * FROM ledger WHERE site_id=? AND water_status='PENDING' AND pay_status='SUCCESS'",
        (op["site_id"],)).fetchall()
    pend_rows = []
    for t in pend_pay:
        pend_rows.append(
            f"<tr><td><code>{esc(t['txn_id'])}</code></td><td>{esc(t['account_no'])}</td>"
            f"<td>{esc(method_cn(t['channel'],t['method']))}</td><td class=num>{money(t['amount_cents'])}</td>"
            f"<td>{pay_badge('PENDING')}</td><td>支付回执延迟，等待渠道</td></tr>")
    for t in pend_water:
        pend_rows.append(
            f"<tr><td><code>{esc(t['txn_id'])}</code></td><td>{esc(t['account_no'])}</td>"
            f"<td>{esc(method_cn(t['channel'],t['method']))}</td><td class=num>{money(t['amount_cents'])}</td>"
            f"<td>{water_badge('PENDING')}</td>"
            f"<td><form method='post' action='/water-reconfirm' style='display:inline'>"
            f"<input type=hidden name=txn value='{esc(t['txn_id'])}'>"
            f"<button class='small ghost'>重推水务销账</button></form></td></tr>")
    sync_form = ""
    if pend_pay:
        sync_form = ('<form method="post" action="/sync-payments" style="margin:0 0 10px">'
                     '<button class="small">同步渠道回执（模拟轮询）</button></form>')
    pend_box = f"""
<h2>待处理回执（支付成功但回执延迟 / 水务销账延迟）</h2>
<div class="card">{sync_form}
<table><thead><tr><th>流水号</th><th>户号</th><th>渠道</th><th class=num>金额</th>
<th>当前状态</th><th>处理</th></tr></thead><tbody>{''.join(pend_rows) or
'<tr><td colspan=6 class=muted>无待处理回执</td></tr>'}</tbody></table></div>"""

    shift = svc().current_shift(op)
    if shift:
        shift_box = (f'<div class="flash ok">当前班次 #{shift["shift_id"]} 进行中'
                     f'（备用金 {money(shift["opening_float_cents"])}），'
                     f'<a href="/shift?id={shift["shift_id"]}">查看交班单</a></div>')
    else:
        shift_box = ('<div class="flash warn">尚未开班：'
                     '<form method="post" action="/shift/open" style="display:inline">'
                     '<button class="small">立即开班</button></form></div>')

    body = f"""
<h1>账单查询与代缴</h1>
<div class="sub">输入居民户号查询账单、已缴凭证；居民也可在此核对本户缴费。</div>
{flash}{err}{shift_box}
<div class="card"><form method="get" action="/">
  <label class="field"><span>户号</span>
    <input type="text" name="q" value="{esc(q)}" placeholder="例如 320101" required autofocus>
  </label>
  <button type="submit">查询</button>
  <span class="muted" style="margin-left:10px">可试：320101（正常）、320102（错户冲正样例）、320104 / 320105（回执延迟）</span>
</form></div>
{cards}
{pend_box}"""
    return layout(session, "home", body, "账单查询")


def page_pay(session, account_no, period, error=None):
    op = session["op"]
    try:
        info = svc().lookup_household(op, account_no)
    except ServiceError as e:
        return page_home(session, account_no, error=str(e))
    item = next((x for x in info["bills"] if x["bill"]["period"] == period), None)
    if item is None:
        return page_home(session, account_no, error=f"无 {period} 账期账单")
    b = item["bill"]
    if item["status"] == "PAID":
        return page_home(session, account_no, error="该账单已结清，不能重复缴费")
    shift = svc().current_shift(op)
    shift_warn = ""
    if shift is None:
        shift_warn = '<div class="flash err">当前没有进行中的班次，请先开班再收款。</div>'
    flash = flash_html(session)
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    h = info["household"]
    body = f"""
<h1>代缴确认</h1>
<div class="sub">以下信息取自水务账单，固定不可修改；请与居民当面核对户号、账期、金额。</div>
{flash}{err}{shift_warn}
<div class="confirm-box">
  <div class="cb-title">缴费确认区（系统固定）</div>
  <dl>
    <dt>户号</dt><dd>{esc(h['account_no'])}</dd>
    <dt>户名代号</dt><dd>{esc(h['account_code'])}</dd>
    <dt>账期</dt><dd>{esc(b['period'])}</dd>
    <dt>村组</dt><dd>{esc(h['village'])}</dd>
    <dt>应缴金额</dt><dd class="amount">{money(b['amount_cents'])}</dd>
    <dt>大写</dt><dd>{esc(cents_chinese(b['amount_cents']))}</dd>
  </dl>
</div>
<p class="lock-note">🔒 户号 / 户名代号 / 账期 / 应缴金额由账单锁定，页面与后端均不接受手改金额。</p>
<div class="card"><form method="post" action="/pay">
  <input type="hidden" name="account_no" value="{esc(h['account_no'])}">
  <input type="hidden" name="period" value="{esc(b['period'])}">
  <label class="field"><span>收款渠道</span>
    <div class="radio-row">
      <label><input type="radio" name="channel" value="cash" checked> 现金</label>
      <label><input type="radio" name="channel" value="alipay"> 支付宝</label>
      <label><input type="radio" name="channel" value="wechat"> 微信</label>
      <label><input type="radio" name="channel" value="bankcash"> 银行代收</label>
    </div>
  </label>
  <label class="field"><span>模拟回执场景（演示用）</span>
    <select name="simulate">
      <option value="normal">正常：即时回执 + 即时销账</option>
      <option value="pay_delay">支付回执延迟（钱已扣，渠道稍后回执）</option>
      <option value="water_delay">支付即时成功，水务销账回执延迟</option>
      <option value="pay_fail">支付失败（不出收据、不占额度）</option>
    </select>
  </label>
  <div class="btns">
    <button type="submit" {"disabled" if shift is None else ""}>确认收款 {fmt_money(b['amount_cents'])}</button>
    <a class="btn ghost" href="/?q={esc(h['account_no'])}">返回</a>
  </div>
  <script>
    (function(){{
      var radios = document.querySelectorAll('input[name=channel]');
      var sel = document.querySelector('select[name=simulate]');
      function sync(){{
        var cash = document.querySelector('input[name=channel]:checked').value === 'cash';
        for (var i=0;i<sel.options.length;i++){{
          var o = sel.options[i];
          o.disabled = cash && o.value !== 'normal';
          if (o.disabled && o.selected) sel.value = 'normal';
        }}
      }}
      radios.forEach(function(r){{ r.addEventListener('change', sync); }});
      sync();
    }})();
  </script>
</form></div>"""
    return layout(session, "home", body, "代缴确认")


def page_receipt(session, no):
    r = db().execute("SELECT * FROM receipts WHERE receipt_no=?", (no,)).fetchone()
    if r is None:
        return None
    t = db().execute("SELECT * FROM ledger WHERE txn_id=?", (r["txn_id"],)).fetchone()
    op = session["op"]
    if t["site_id"] != op["site_id"]:
        raise AuthError("站点隔离：该收据不属于本服务点")
    text = render_receipt(db(), no)
    title = "红字更正收据" if r["kind"] == "REDLINE" else "水费代收收据"
    void = badge("本收据已作废", "bad") if r["voided"] else ""
    body = f"""
<h1>{title} {void}</h1>
<div class="sub">演示收据以文本文件保存在本机：<code>{esc(r['file_path'])}</code>（原文件保留，不删除）</div>
<div class="card"><pre class="receipt">{esc(text)}</pre></div>
<p><a class="btn ghost" href="javascript:history.back()">返回</a></p>"""
    return layout(session, "home", body, "收据")


def page_reversal_form(session, txn, error=None):
    op = session["op"]
    flash = flash_html(session)
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    orig_block = ""
    if txn:
        try:
            t = svc().find_payment(op, txn)
        except ServiceError as e:
            return page_reversal_form(session, "", error=str(e))
        r = db().execute(
            "SELECT * FROM receipts WHERE txn_id=? AND kind='NORMAL'", (txn,)
        ).fetchone()
        h = db().execute(
            "SELECT * FROM households WHERE account_no=?", (t["account_no"],)
        ).fetchone()
        existing = db().execute(
            "SELECT status FROM approval_requests WHERE original_txn_id=? "
            "AND status IN ('PENDING','APPROVED')", (txn,)).fetchone()
        block_warn = ""
        if existing:
            block_warn = ('<div class="flash err">该原单已存在'
                          + ("待主管审批的冲正申请" if existing["status"] == "PENDING"
                             else "批准冲正，不能重复申请") + "</div>")
        disabled = bool(existing) or t["water_status"] == "PENDING"
        if t["water_status"] == "PENDING":
            block_warn = '<div class="flash err">水务销账仍在确认中，须等回执明确后才能冲正。</div>'
        reason_opts = "".join(
            f'<option value="{k}">{v}</option>' for k, v in REASONS.items()
        )
        orig_block = f"""
<h2>原交易（将被冲正）</h2>
<div class="card"><dl class="kv">
  <dt>原流水号</dt><dd><code>{esc(t['txn_id'])}</code></dd>
  <dt>户号 / 户名代号</dt><dd>{esc(t['account_no'])} / {esc(h['account_code'])}</dd>
  <dt>账期</dt><dd>{esc(t['period'])}</dd>
  <dt>原收金额</dt><dd>{money(t['amount_cents'])}</dd>
  <dt>渠道</dt><dd>{esc(method_cn(t['channel'],t['method']))}（{esc(t['pay_ref'])}）</dd>
  <dt>原收据</dt><dd>{('<a href="/receipt?no='+esc(r['receipt_no'])+'">'+esc(r['receipt_no'])+'</a>') if r else '—'}</dd>
</dl></div>
{block_warn}
<h2>填写冲正申请</h2>
<div class="card"><form method="post" action="/reversal">
  <input type="hidden" name="txn" value="{esc(txn)}">
  <label class="field"><span>冲正原因</span>
    <select name="reason" {"disabled" if disabled else ""}>{reason_opts}</select></label>
  <label class="field"><span>情况说明（必填，留痕）</span>
    <textarea name="detail" rows="3" style="width:100%" {"disabled" if disabled else ""}
      placeholder="例如：居民本意缴 320101，误录为 320102；或居民重复扫码缴费一次"></textarea></label>
  <button type="submit" {"disabled" if disabled else ""}>提交主管审批</button>
  <span class="muted" style="margin-left:8px">冲正不会删除流水；批准后生成等额负向事件与红字收据。</span>
</form></div>"""

    recent = svc().recent_txns(op, 15)
    opts = []
    for t in recent:
        if t["event_type"] != "PAYMENT" or t["pay_status"] != "SUCCESS":
            continue
        opts.append(
            f"<option value='{esc(t['txn_id'])}'>{esc(t['txn_id'])}｜"
            f"{esc(t['account_no'])}｜{esc(t['period'])}｜{fmt_money(t['amount_cents'])}｜"
            f"{esc(method_cn(t['channel'],t['method']))}</option>")
    body = f"""
<h1>冲正申请</h1>
<div class="sub">输错户号、重复缴费、跨账期付款、回执异常、作废已打印收据——均须引用原交易并经主管批准。</div>
{flash}{err}
<div class="card"><form method="get" action="/reversal">
  <label class="field"><span>原收款流水号</span>
    <input type="text" name="txn" value="{esc(txn)}" placeholder="T 开头的流水号，可从账单页点击">
  </label>
  <button>调出原单</button>
</form></div>
{orig_block}
<h2>本班最近成功收款（快捷选择）</h2>
<div class="card"><select onchange="if(this.value)location.href='/reversal?txn='+this.value">
<option value="">— 选择流水 —</option>{''.join(opts)}</select></div>"""
    return layout(session, "reversal", body, "冲正申请")


def page_approvals(session):
    op = session["op"]
    flash = flash_html(session)
    rows = svc().list_approvals(op)
    trs = []
    for a in rows:
        st = {"PENDING": badge("待审批", "warn"),
              "APPROVED": badge("已批准·红字冲正", "ok"),
              "REJECTED": badge("已驳回", "bad")}[a["status"]]
        trs.append(
            f"<tr><td>#{a['id']}</td><td class=nowrap>{esc(a['created_at'])}</td>"
            f"<td>{esc(a['requester_name'])}</td>"
            f"<td><code>{esc(a['original_txn_id'])}</code></td>"
            f"<td>{REASON_CN.get(a['reason_code'], a['reason_code'])}</td>"
            f"<td>{esc((a['reason_detail'] or '')[:28])}</td><td>{st}</td>"
            f"<td><a class='btn small' href='/approval?id={a['id']}'>查看原单/更正单</a></td></tr>")
    body = f"""
<h1>冲正审批{'' if op['role']=='supervisor' else '（仅主管可批准）'}</h1>
<div class="sub">冲正必须引用原交易、经主管批准；批准后原收据作废并出具红字更正收据，账务以负向事件平衡。</div>
{flash}
<div class="card"><table><thead><tr>
<th>单号</th><th>申请时间</th><th>申请人</th><th>原流水</th><th>原因</th>
<th>说明</th><th>状态</th><th>操作</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=8 class=muted>暂无冲正申请</td></tr>'}</tbody></table></div>"""
    return layout(session, "approvals", body, "冲正审批")


def _receipt_panel(kind, title, receipt_no, txn_id, redline=False):
    if receipt_no:
        try:
            text = render_receipt(db(), receipt_no)
        except Exception:
            text = "（收据生成失败）"
        link = f' <a class="btn small" href="/receipt?no={esc(receipt_no)}">打开收据文件</a>'
    else:
        text, link = "（尚未出具）", ""
    cls = " panel redline" if redline else ""
    return f"""<div class="panel{cls}"><h3>{title}{link}</h3>
      <div class="body"><pre class="receipt">{esc(text)}</pre></div></div>"""


def page_approval_detail(session, req_id, error=None):
    op = session["op"]
    d = svc().approval_detail(op, req_id)
    req, orig, rev = d["req"], d["orig"], d["rev"]
    h = db().execute(
        "SELECT * FROM households WHERE account_no=?", (orig["account_no"],)
    ).fetchone()
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    flash = flash_html(session)

    def txn_kv(t):
        return f"""<dl class="kv">
      <dt>流水号</dt><dd><code>{esc(t['txn_id'])}</code></dd>
      <dt>户号/代号</dt><dd>{esc(t['account_no'])} / {esc(h['account_code'])}</dd>
      <dt>账期</dt><dd>{esc(t['period'])}</dd>
      <dt>金额</dt><dd>{signed(t['amount_cents'])}</dd>
      <dt>渠道</dt><dd>{esc(method_cn(t['channel'],t['method']))}</dd>
      <dt>渠道流水</dt><dd><code>{esc(t['pay_ref'] or '—')}</code></dd>
      <dt>引用原交易</dt><dd><code>{esc(t['related_txn'] or '—')}</code></dd>
      <dt>时间</dt><dd>{esc(t['created_at'])}</dd></dl>"""

    orig_panel = (f'<div class="panel"><h3>原单（收款）</h3><div class="body">'
                  f"{txn_kv(orig)}</div></div>")
    if rev:
        rev_panel = (f'<div class="panel redline"><h3>更正单（红字冲正，等额为负）</h3>'
                     f'<div class="body">{txn_kv(rev)}</div></div>')
    else:
        rev_panel = ('<div class="panel redline"><h3>更正单（红字冲正）</h3>'
                     '<div class="body muted">主管批准后生成：等额负向事件、退款/退现金、'
                     '原收据作废并出具红字收据。</div></div>')

    orig_receipt = _receipt_panel("NORMAL", "原收据",
                                  d["orig_receipt"]["receipt_no"] if d["orig_receipt"] else None,
                                  orig["txn_id"])
    rev_receipt = _receipt_panel("REDLINE", "红字更正收据",
                                 d["rev_receipt"]["receipt_no"] if d["rev_receipt"] else None,
                                 rev["txn_id"] if rev else None, redline=True)

    action = ""
    if req["status"] == "PENDING":
        if op["role"] == "supervisor":
            action = f"""
<h2>主管审批</h2>
<div class="card"><form method="post" action="/approval">
  <input type="hidden" name="id" value="{req_id}">
  <label class="field"><span>审批意见</span>
    <textarea name="comment" rows="2" style="width:100%" placeholder="可填写批准/驳回说明"></textarea></label>
  <div class="btns">
    <button name="decision" value="approve">批准冲正</button>
    <button class="danger" name="decision" value="reject">驳回</button>
    <a class="btn ghost" href="/approvals">返回列表</a>
  </div>
</form></div>"""
        else:
            action = ('<div class="flash warn">该申请等待主管审批；操作员无权批准。</div>')
    else:
        reviewer = esc(req["reviewer_id"] or "")
        action = f"""<div class="card"><dl class="kv">
        <dt>审批结果</dt><dd>{badge('已批准','ok') if req['status']=='APPROVED' else badge('已驳回','bad')}</dd>
        <dt>审批时间</dt><dd>{esc(req['reviewed_at'])}</dd>
        <dt>审批意见</dt><dd>{esc(req['review_comment'] or '—')}</dd>
        </dl></div>"""

    body = f"""
<h1>冲正单 #{req_id}　{badge(REASON_CN.get(req['reason_code'],req['reason_code']),'info')}
　{badge({'PENDING':'待审批','APPROVED':'已批准','REJECTED':'已驳回'}[req['status']],
        {'PENDING':'warn','APPROVED':'ok','REJECTED':'bad'}[req['status']])}</h1>
<div class="sub">申请人：{esc(req['requested_by'])}；情况说明：{esc(req['reason_detail'] or '—')}</div>
{flash}{err}
<h2>账务记录并列对比</h2>
<div class="split">{orig_panel}{rev_panel}</div>
<h2>收据并列对比</h2>
<div class="split">{orig_receipt}{rev_receipt}</div>
{action}"""
    return layout(session, "approvals", body, f"冲正单#{req_id}")


def page_cash(session):
    op = session["op"]
    flash = flash_html(session)
    rows = svc().cash_records(op)
    trs = []
    for l in rows:
        kind = EVENT_CN[l["event_type"]]
        if l["event_type"] == "REVERSAL":
            kind = '冲正更正 <span class="badge bad">负数退回</span>'
        elif l["event_type"] == "SHORTAGE":
            kind = "交班差异"
        cell = signed(l["amount_cents"])
        trs.append(
            f"<tr><td class=nowrap>{esc(l['created_at'])}</td><td>{kind}</td>"
            f"<td>{esc(l['account_no'] or '—')} {esc(l['account_code'] or '')}</td>"
            f"<td>{esc(l['period'] or '—')}</td><td class=num>{cell}</td>"
            f"<td>{esc(REASON_CN.get(l['reason_code'],'') ) if l['reason_code'] else '—'}</td>"
            f"<td><code>{esc(l['txn_id'])}</code></td></tr>")
    body = f"""
<h1>本服务点现金记录</h1>
<div class="sub">站点隔离：仅显示 {esc(op['site_id'])} 的现金收款、现金冲正退回与交班短款/长款。</div>
{flash}
<div class="card"><table><thead><tr>
<th>时间</th><th>类型</th><th>户号/代号</th><th>账期</th><th class=num>金额（元）</th>
<th>原因</th><th>流水号</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=7 class=muted>暂无现金记录</td></tr>'}</tbody></table></div>"""
    return layout(session, "cash", body, "现金记录")


def page_shifts(session):
    op = session["op"]
    flash = flash_html(session)
    cur = svc().current_shift(op)
    cur_box = ""
    if cur:
        cur_box = (f'<div class="flash ok">当前进行中：<a href="/shift?id={cur["shift_id"]}">'
                   f'班次 #{cur["shift_id"]}</a></div>')
    else:
        cur_box = ('<div class="flash warn">无进行中班次。'
                   '<form method="post" action="/shift/open" style="display:inline">'
                   '<button class="small">开班（备用金 200.00）</button></form></div>')
    rows = svc().list_shifts(op)
    trs = []
    for s in rows:
        state = badge("进行中", "warn") if s["closed_at"] is None else badge("已交班", "ok")
        diff = "—" if s["shortage_cents"] is None else signed(s["shortage_cents"])
        trs.append(
            f"<tr><td>#{s['shift_id']}</td><td>{esc(s['display_name'])}</td>"
            f"<td class=nowrap>{esc(s['opened_at'])}</td><td>{esc(s['closed_at'] or '—')}</td>"
            f"<td class=num>{money(s['opening_float_cents'])}</td>"
            f"<td class=num>{money(s['expected_cash_cents']) if s['expected_cash_cents'] is not None else '—'}</td>"
            f"<td class=num>{money(s['actual_cash_cents']) if s['actual_cash_cents'] is not None else '—'}</td>"
            f"<td class=num>{diff}</td><td>{state}</td>"
            f"<td><a class='btn small' href='/shift?id={s['shift_id']}'>交班单</a></td></tr>")
    body = f"""
<h1>交班对账</h1>
<div class="sub">每班核对：备用金 + 现金收款 − 现金冲正退回 = 应存现金；实点不符须登记短款/长款事件。</div>
{flash}{cur_box}
<div class="card"><table><thead><tr>
<th>班次</th><th>操作员</th><th>开班</th><th>交班</th><th class=num>备用金</th>
<th class=num>应存现金</th><th class=num>实点现金</th><th class=num>差异</th><th>状态</th><th>单据</th>
</tr></thead><tbody>{''.join(trs) or '<tr><td colspan=10 class=muted>暂无班次</td></tr>'}</tbody></table></div>"""
    return layout(session, "shifts", body, "交班对账")


def page_shift_detail(session, shift_id, error=None):
    op = session["op"]
    r = svc().shift_report(op, shift_id)
    s = r["shift"]
    closed = s["closed_at"] is not None
    flash = flash_html(session)
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""

    trs = []
    for l in r["lines"]:
        if l["event_type"] == "PAYMENT":
            amt = money(l["amount_cents"])
            tag = method_cn(l["channel"], l["method"])
            st = f'{pay_badge(l["pay_status"])} {water_badge(l["water_status"])}'
        elif l["event_type"] == "REVERSAL":
            amt = signed(l["amount_cents"])
            tag = f'{method_cn(l["channel"],l["method"])}退回'
            st = badge("冲正", "bad")
        else:
            amt = signed(l["amount_cents"])
            tag = "现金差异"
            st = badge("短款" if l["amount_cents"] < 0 else "长款",
                       "bad" if l["amount_cents"] < 0 else "warn")
        trs.append(
            f"<tr><td><code>{esc(l['txn_id'])}</code></td>"
            f"<td>{EVENT_CN[l['event_type']]}</td>"
            f"<td>{esc(l['account_no'] or '—')}</td><td>{esc(l['period'] or '—')}</td>"
            f"<td>{esc(tag)}</td><td class=num>{amt}</td><td>{st}</td></tr>")

    elec_rows = "".join(
        f"<tr><td>{esc(method_cn(k,'electronic'))}</td>"
        f"<td class=num>{money(v)}</td></tr>"
        for k, v in sorted(r["electronic"].items())
    ) or '<tr><td colspan=2 class=muted>无电子收款</td></tr>'

    if closed:
        diff = signed(r["shortage"])
        close_box = f"""
<div class="totals"><table>
<tr><td>交班时间</td><td>{esc(s['closed_at'])}</td></tr>
<tr><td>应存现金</td><td class=num>{money(s['expected_cash_cents'])}</td></tr>
<tr><td>实点现金</td><td class=num>{money(s['actual_cash_cents'])}</td></tr>
<tr><td class=grand>差异（负=短款）</td><td class="num grand">{diff}</td></tr>
<tr><td>备注</td><td>{esc(s['note'] or '—')}</td></tr></table></div>"""
    else:
        pend_warn = ""
        if r["pending"]:
            pend_warn = ('<div class="flash err">仍有 '
                         f'{len(r["pending"])} 笔电子支付回执未到，'
                         '请先回首页「同步渠道回执」再交班。</div>')
        close_box = f"""
{pend_warn}
<div class="totals"><table>
<tr><td>开班备用金</td><td class=num>{money(s['opening_float_cents'])}</td></tr>
<tr><td>本班现金收款</td><td class=num>{money(r['cash_collected'])}</td></tr>
<tr><td>现金冲正退回（负数）</td><td class=num>{signed(r['cash_reversed'])}</td></tr>
<tr><td class=grand>应存现金合计</td><td class="num grand">{money(r['expected_cash'])}</td></tr>
</table></div>
<h2>交班核对</h2>
<div class="card"><form method="post" action="/shift/close">
  <input type="hidden" name="id" value="{shift_id}">
  <label class="field"><span>实点现金（元）</span>
    <input type="number" name="actual" step="0.01" min="0"
           value="{esc(cents_to_yuan(r['expected_cash']))}" required></label>
  <label class="field"><span>备注</span>
    <input type="text" name="note" style="width:60%" placeholder="短款/长款原因，如零钞误差、补缴计划"></label>
  <button type="submit" {"disabled" if r['pending'] else ""}>确认交班</button>
  <span class="muted" style="margin-left:8px">实点与应存不符时，系统自动登记有符号短款/长款事件，不得直接改数。</span>
</form></div>"""

    body = f"""
<h1>交班单 #{shift_id} {badge('进行中','warn') if not closed else badge('已交班','ok')}</h1>
<div class="sub">操作员：{esc(op['display_name'])}；开班 {esc(s['opened_at'])}</div>
{flash}{err}
<div class="split">
<div class="panel"><h3>现金账</h3><div class="body totals"><table>
<tr><td>备用金</td><td class=num>{money(s['opening_float_cents'])}</td></tr>
<tr><td>现金收款</td><td class=num>{money(r['cash_collected'])}</td></tr>
<tr><td>冲正退回</td><td class=num>{signed(r['cash_reversed'])}</td></tr>
<tr><td class=grand>应存现金</td><td class="num grand">
{money(r['expected_cash']) if not closed else money(s['expected_cash_cents'])}</td></tr>
</table></div></div>
<div class="panel"><h3>电子支付（不入现金抽屉）</h3><div class="body totals">
<table>{elec_rows}</table></div></div>
</div>
<h2>本班流水（{len(r['lines'])} 条）</h2>
<div class="card"><table><thead><tr>
<th>流水号</th><th>类型</th><th>户号</th><th>账期</th><th>渠道</th>
<th class=num>金额</th><th>状态</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=7 class=muted>本班暂无流水</td></tr>'}</tbody></table></div>
{close_box}"""
    return layout(session, "shifts", body, f"交班单#{shift_id}")


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "WaterKiosk/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- 基础 ---
    def send_html(self, text, status=200, cookies=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path, cookies=None):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", path)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def get_session(self) -> dict | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie.get("sid")
        if sid is None:
            return None
        return sessions.get(sid.value)

    def require_login(self) -> dict | None:
        sess = self.get_session()
        if sess and sess.get("op"):
            return sess
        self.send_html(page_login())
        return None

    def refresh_op(self, sess):
        sess["op"] = svc().operator(sess["op"]["operator_id"])

    def wrap(self, fn):
        try:
            return fn()
        except AuthError as e:
            self.send_html(self._error_page(str(e)), 403)
        except ServiceError as e:
            self.send_html(self._error_page(str(e)), 400)

    def _error_page(self, msg):
        sess = self.get_session()
        body = (f'<h1>操作被拒绝</h1><div class="flash err">{esc(msg)}</div>'
                '<p><a class="btn" href="/">返回首页</a></p>')
        return layout(sess, "", body, "提示") if sess and sess.get("op") else page_login()

    # --- GET ---
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/static/style.css":
            return self._serve_css()
        sess = self.require_login()
        if sess is None:
            return
        self.refresh_op(sess)
        op = sess["op"]

        if path == "/":
            q = qs.get("q", [""])[0].strip()
            if q:
                try:
                    result = svc().lookup_household(op, q)
                except ServiceError as e:
                    return self.send_html(page_home(sess, q, error=str(e)))
                return self.send_html(page_home(sess, q, result))
            return self.send_html(page_home(sess, ""))
        if path == "/pay":
            return self.send_html(page_pay(sess, qs.get("account_no", [""])[0],
                                           qs.get("period", [""])[0]))
        if path == "/receipt":
            page = page_receipt(sess, qs.get("no", [""])[0])
            return self.send_html(page if page else self._error_page("收据不存在"),
                                  200 if page else 404)
        if path == "/reversal":
            return self.send_html(page_reversal_form(sess, qs.get("txn", [""])[0].strip()))
        if path == "/approvals":
            return self.send_html(page_approvals(sess))
        if path == "/approval":
            try:
                return self.send_html(page_approval_detail(
                    sess, int(qs.get("id", ["0"])[0])))
            except ServiceError as e:
                return self.send_html(self._error_page(str(e)), 400)
        if path == "/cash":
            return self.send_html(page_cash(sess))
        if path == "/shifts":
            return self.send_html(page_shifts(sess))
        if path == "/shift":
            try:
                return self.send_html(page_shift_detail(
                    sess, int(qs.get("id", ["0"])[0])))
            except ServiceError as e:
                return self.send_html(self._error_page(str(e)), 400)
        self.send_html(self._error_page("页面不存在"), 404)

    def _serve_css(self):
        try:
            with open(os.path.join(STATIC_DIR, "style.css"), "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- POST ---
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        form = self.read_form()
        if path == "/login":
            try:
                op = svc().login(form.get("username", ""))
            except AuthError as e:
                self.send_html(self._error_page(str(e)), 403)
                return
            import secrets
            sid = secrets.token_hex(16)
            with _sessions_lock:
                sessions[sid] = {"op": dict(op)}
            return self.redirect("/", [f"sid={sid}; Path=/; HttpOnly"])
        if path == "/logout":
            return self.redirect("/login", ["sid=; Path=/; Max-Age=0"])

        sess = self.require_login()
        if sess is None:
            return
        self.refresh_op(sess)
        op = sess["op"]

        def flash(kind, msg):
            sess.setdefault("_flash", []).append((kind, msg))

        try:
            if path == "/pay":
                res = svc().pay_bill(
                    op, form["account_no"].strip(), form["period"].strip(),
                    form.get("channel", "cash"), form.get("simulate", "normal"))
                if res["pay_status"] == "FAILED":
                    flash("err", "支付失败：未出收据、未占用应缴额度，可改用其他渠道重试。")
                elif res["pay_status"] == "PENDING":
                    flash("warn", "支付渠道回执延迟：款项处理中，请稍后在首页「同步渠道回执」，"
                                  "确认后补开收据，重复送达自动幂等。")
                else:
                    flash("ok", f'收款成功，收据 {res["receipt_no"]} 已出具'
                                + ("（水务销账确认中）" if res["water_status"] == "PENDING"
                                   else "，水务已销账。"))
                return self.redirect(f'/?q={urllib.parse.quote(form["account_no"])}')

            if path == "/sync-payments":
                results = svc().sync_pending_payments(op)
                dup = sum(1 for r in results if r["duplicate"])
                ok = len(results) - dup
                flash("ok", f"渠道回执同步完成：新确认 {ok} 笔，重复回执 {dup} 笔（已幂等忽略）。")
                return self.redirect("/")

            if path == "/water-reconfirm":
                result = svc().reconfirm_water(op, form["txn"])
                flash("ok" if result == "CONFIRMED" else "err",
                      f"水务销账重推结果：{WATER_CN[result]}")
                return self.redirect("/")

            if path == "/reversal":
                rid = svc().request_reversal(
                    op, form["txn"].strip(), form.get("reason", "other"),
                    form.get("detail", "").strip() or "（未填说明）")
                flash("ok", f"冲正申请 #{rid} 已提交，等待主管审批。")
                return self.redirect(f"/approval?id={rid}")

            if path == "/approval":
                if op["role"] != "supervisor":
                    raise AuthError("只有主管可以审批冲正")
                rid = int(form["id"])
                if form.get("decision") == "approve":
                    res = svc().review_reversal(
                        op, rid, True, form.get("comment", "").strip())
                    flash("ok", f'已批准冲正：原收据作废，红字更正收据 {res["receipt_no"]} '
                                '已出具，账单额度已回补。')
                else:
                    svc().review_reversal(op, rid, False, form.get("comment", "").strip())
                    flash("warn", f"冲正申请 #{rid} 已驳回。")
                return self.redirect(f"/approval?id={rid}")

            if path == "/shift/open":
                sid = svc().open_shift(op, yuan_to_cents("200"))
                flash("ok", f"班次 #{sid} 已开班，备用金 ¥200.00。")
                return self.redirect(f"/shift?id={sid}")

            if path == "/shift/close":
                sid = int(form["id"])
                actual = yuan_to_cents(form["actual"])
                res = svc().close_shift(op, sid, actual, form.get("note", "").strip())
                if res["diff"] < 0:
                    flash("err", f'交班完成：现金短款 {fmt_signed(res["diff"])}，'
                                 '已登记 SHORTAGE 负向事件并关联本班，不得删除。')
                elif res["diff"] > 0:
                    flash("warn", f'交班完成：现金长款 {fmt_money(res["diff"])}，已登记待查。')
                else:
                    flash("ok", "交班完成：账实相符。")
                return self.redirect(f"/shift?id={sid}")

            self.send_html(self._error_page("未知操作"), 404)
        except ServiceError as e:
            # 表单类错误回到来源页更友好
            back = self.headers.get("Referer", "/")
            flash("err", str(e))
            return self.redirect(back)


def run(host="127.0.0.1", port=8000):
    global STATE
    STATE = AppState()
    httpd = ThreadingHTTPServer((host, port), Handler)
    what = "已载入现有数据" if not STATE.seeded else "已初始化演示数据（含错户冲正、短款、回执延迟样例）"
    print(f"💧 村镇便民点水费代缴系统 —— {what}")
    print(f"   本机访问：http://{host}:{port}")
    print(f"   数据目录：{os.path.join(BASE_DIR, 'data')}")
    print("   演示账号：zhao（河东·操作员） qian（河东·主管） sun/li（河西）")
    httpd.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    run(port=port)
