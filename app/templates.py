"""HTML 模板（无第三方依赖）。白底 + 深蓝；负数符号与文字并列。"""
from __future__ import annotations

import html

from .money import channel_label, kind_label, pay_status_label, signed, yuan


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def g(row, key, default=""):
    """安全读取 sqlite3.Row 的列（无 .get 方法）。"""
    try:
        v = row[key]
        return default if v is None else v
    except (IndexError, KeyError):
        return default


BASE_CSS = """
:root { --navy:#0b3d91; --navy2:#123a75; --ink:#1a2433; --line:#d6e0f0;
        --bg:#fff; --soft:#eef3fb; --red:#a5281f; --green:#1e6b3a; --amber:#8a6110; }
* { box-sizing:border-box; }
body { font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif; margin:0;
       color:var(--ink); background:var(--bg); }
header { background:var(--navy); color:#fff; padding:12px 26px; display:flex;
         align-items:center; gap:18px; flex-wrap:wrap; }
header .brand { font-size:18px; font-weight:700; }
header nav a { color:#dbe7fb; text-decoration:none; margin-right:14px; font-size:14px; }
header nav a:hover { color:#fff; text-decoration:underline; }
header .who { margin-left:auto; font-size:13px; color:#cdd9f0; }
main { max-width:1000px; margin:22px auto; padding:0 20px; }
h2 { color:var(--navy); border-bottom:3px solid var(--navy); padding-bottom:6px; }
h3 { color:var(--navy2); }
a { color:var(--navy); }
table.grid { width:100%; border-collapse:collapse; margin:12px 0; background:#fff; }
table.grid th, table.grid td { border:1px solid var(--line); padding:8px 10px;
       text-align:left; font-size:14px; }
table.grid th { background:var(--soft); color:var(--navy); }
.card { border:1px solid var(--line); border-radius:8px; padding:16px 18px;
        margin:14px 0; background:#fff; }
.fixed-zone { border:2px solid var(--navy); border-radius:8px; background:var(--soft);
        padding:16px 20px; margin:14px 0; }
.fixed-zone table { width:100%; }
.fixed-zone th { text-align:left; color:var(--navy); width:120px; padding:6px 8px; }
.fixed-zone .big { font-size:22px; font-weight:700; color:var(--navy); }
.btn { display:inline-block; background:var(--navy); color:#fff; border:0;
       padding:8px 18px; border-radius:6px; font-size:14px; cursor:pointer;
       text-decoration:none; }
.btn:hover { background:var(--navy2); }
.btn.red { background:var(--red); }
.btn.ghost { background:#fff; color:var(--navy); border:1px solid var(--navy); }
input[type=text],input[type=password],select { padding:7px 9px; border:1px solid #9fb0cc;
       border-radius:5px; font-size:14px; }
label { font-size:14px; margin-right:14px; }
.msg { padding:10px 14px; border-radius:6px; margin:10px 0; font-size:14px; }
.msg.ok { background:#e6f4ea; color:var(--green); border:1px solid #bfe3cb; }
.msg.err { background:#fdecea; color:var(--red); border:1px solid #f2c4c0; }
.msg.info { background:#fff6e0; color:var(--amber); border:1px solid #f0dfae; }
.neg { color:var(--red); font-weight:700; }
.pos { color:var(--green); font-weight:700; }
.tag { display:inline-block; padding:1px 9px; border-radius:10px; font-size:12px; }
.tag.paid { background:#e6f4ea; color:var(--green); }
.tag.unpaid { background:#fdecea; color:var(--red); }
.tag.partial { background:#fff6e0; color:var(--amber); }
.tag.pending { background:#fff6e0; color:var(--amber); }
.muted { color:#5d6b82; font-size:12px; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:820px){ .cols{grid-template-columns:1fr} }
.stat { font-size:15px; line-height:1.9; }
.stat b { color:var(--navy); }
.formrow { margin:10px 0; }
.pill { font-size:12px; border:1px solid var(--line); border-radius:10px;
        padding:1px 8px; color:var(--navy2); }
"""


def page(title: str, body: str, user=None, nav="", msg="", err="", info="") -> str:
    flash = ""
    if msg:
        flash += f'<div class="msg ok">{esc(msg)}</div>'
    if info:
        flash += f'<div class="msg info">{esc(info)}</div>'
    if err:
        flash += f'<div class="msg err">{esc(err)}</div>'
    who = ""
    if user is not None:
        role = "主管" if user["role"] == "supervisor" else "操作员"
        site = f"｜{user['site_code']}" if user["site_code"] else "（跨站点）"
        who = (
            f'<span class="who">{esc(user["display_name"])}（{role}{site}）'
            f'<a style="color:#cdd9f0;margin-left:10px" href="/logout">退出</a></span>'
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 村镇水费代缴</title>
<style>{BASE_CSS}</style></head>
<body>
<header>
  <span class="brand">💧 村镇便民水费代缴</span>
  <nav>{nav}</nav>
  {who}
</header>
<main>
  <h2>{esc(title)}</h2>
  {flash}
  {body}
</main></body></html>"""


def nav_for(user) -> str:
    if user is None:
        return '<a href="/">查户/查账</a><a href="/login">员工登录</a>'
    links = ['<a href="/">账单查询</a>']
    if user["role"] == "operator":
        links += [
            '<a href="/shift">交班对账</a>',
            '<a href="/cash">本站点现金记录</a>',
        ]
    else:
        links += [
            '<a href="/reversals">冲正审批</a>',
            '<a href="/shift">班次总览</a>',
            '<a href="/cash">现金记录（跨站点）</a>',
        ]
    return "".join(links)


# ---------------------------------------------------------------- 登录

def login_view(users, err=""):
    cards = "".join(
        f"""<form method="post" action="/login" style="display:inline">
          <input type="hidden" name="user_id" value="{esc(u['id'])}">
          <button class="btn {'ghost' if u['role']=='supervisor' else ''}"
                  style="margin:6px">
            {esc(u['display_name'])}（{'主管' if u['role']=='supervisor' else '操作员'}
            {('｜'+esc(u['site_code'])) if u['site_code'] else ''}）
          </button></form>"""
        for u in users
    )
    body = f"""
    <p>演示环境直接选择身份登录。操作员只能处理与查看<b>本服务点</b>的业务；
       冲正审批仅主管可用。</p>
    <div class="card">{cards}</div>
    <p class="muted">居民无需登录，可在 <a href="/">账单查询</a> 页输入户号
       查询本户账单与已缴凭证。</p>"""
    return page("员工登录", body, err=err)


# ---------------------------------------------------------------- 查询

def search_view(account_no="", household=None, bills=None, err="", receipts=None,
                user=None, info="", msg=""):
    can_pay = user is not None and user["role"] == "operator"
    result = ""
    if household is not None and bills is not None:
        site_match = user is None or user["role"] == "supervisor" or \
            household["site_code"] == user["site_code"]
        rows = ""
        for b in bills:
            paid = b["effective_paid"]
            due = b["amount_due"]
            if paid <= 0:
                tag = '<span class="tag unpaid">未缴</span>'
                remain = due
            elif paid >= due:
                tag = '<span class="tag paid">已缴清</span>'
                remain = 0
            else:
                tag = '<span class="tag partial">部分缴费</span>'
                remain = due - paid
            if can_pay and site_match and remain > 0:
                action = (
                    f'<a class="btn" href="/pay?account_no='
                    f'{esc(household["account_no"])}&period={esc(b["period"])}">'
                    f'代缴</a>')
            elif can_pay and not site_match:
                action = '<span class="muted">非本站点户</span>'
            else:
                action = "—"
            rows += f"""<tr>
              <td>{esc(b['period'])}</td>
              <td>{yuan(due)}</td>
              <td>{signed(paid)}</td>
              <td>{tag}</td>
              <td>{yuan(remain)}</td>
              <td>{action}</td>
            </tr>"""
        result = f"""
        <div class="card">
          <h3>户号 {esc(household['account_no'])}
              ｜户名代号 {esc(household['name_code'])}
              ｜归属站点 {esc(household['site_code'])}</h3>
          <table class="grid">
            <tr><th>账期</th><th>应缴（元）</th><th>已缴净额（元）</th>
                <th>状态</th><th>应收余额（元）</th><th>操作</th></tr>
            {rows}
          </table>
        </div>"""

    rcpts = ""
    if receipts:
        lines = ""
        for r in receipts:
            note = ""
            target = r["reverses_id"] or r["id"]
            if r["kind"] == "REVERSAL":
                note = '<span class="neg">冲正（负数：冲减）→ 查看原单</span>'
            elif r["receipt_status"] == "VOID":
                note = '<span class="tag unpaid">已打印收据已作废</span>'
            lines += f"""<tr>
              <td>{esc(r['created_at'])}</td>
              <td>{esc(r['receipt_no'] or '—')}</td>
              <td>{esc(r['period'])}</td>
              <td>{yuan(r['amount'])}</td>
              <td>{channel_label(r['channel'])}</td>
              <td>{pay_status_label(r['pay_status'])}</td>
              <td>{note}</td>
              <td><a href="/receipt/{esc(target)}">查看凭证</a></td></tr>"""
        rcpts = f"""
        <div class="card"><h3>本户已缴凭证（含冲正链）</h3>
          <table class="grid">
            <tr><th>时间</th><th>收据号</th><th>账期</th><th>金额（元）</th>
                <th>渠道</th><th>支付状态</th><th>更正</th><th>凭证</th></tr>
            {lines}
          </table></div>"""

    body = f"""
    <form method="get" action="/" class="card">
      <div class="formrow">
        <label>户号</label>
        <input type="text" name="account_no" value="{esc(account_no)}" size="22"
               placeholder="例如 H1001">
        <button class="btn" type="submit">查询本户账单</button>
      </div>
      <p class="muted">演示户号：H1001、H1002、H2001（青石镇 QS-01）；H3001
         （梧岗镇 WG-02，用于站点隔离验证）。输错户号的冲正样例见
         <a href="/cash">现金记录</a> 中的冲正链。</p>
    </form>
    {result}
    {rcpts}"""
    return page("账单查询", body, user=user, nav=nav_for(user), err=err, info=info,
                msg=msg)


# ---------------------------------------------------------------- 代缴确认

def pay_view(household, bill, user, err="", info=""):
    body = f"""
    <div class="fixed-zone">
      <h3 style="margin-top:0">收款确认区（信息固定，不可修改）</h3>
      <table>
        <tr><th>户号</th><td>{esc(household['account_no'])}</td></tr>
        <tr><th>户名代号</th><td>{esc(household['name_code'])}</td></tr>
        <tr><th>账期</th><td>{esc(bill['period'])}</td></tr>
        <tr><th>应缴金额</th><td class="big">{yuan(bill['amount_due'])} 元</td></tr>
      </table>
    </div>
    <form method="post" action="/pay" class="card">
      <input type="hidden" name="account_no" value="{esc(household['account_no'])}">
      <input type="hidden" name="period" value="{esc(bill['period'])}">
      <input type="hidden" name="amount_yuan" value="{bill['amount_due']/100:.2f}">
      <div class="formrow">
        <label>支付渠道</label>
        <label><input type="radio" name="channel" value="CASH" checked> 现金</label>
        <label><input type="radio" name="channel" value="ELECTRONIC"> 电子支付</label>
      </div>
      <div class="formrow muted">
        <label><input type="checkbox" name="delay_pay" value="1">
          模拟支付成功但渠道回执延迟</label>
        <label><input type="checkbox" name="delay_water" value="1">
          模拟水务销账回执延迟</label>
      </div>
      <button class="btn" type="submit">确认收款并出具收据</button>
      <a class="btn ghost" href="/?account_no={esc(household['account_no'])}">返回</a>
    </form>
    <p class="muted">实付金额与应缴不一致、重复缴费、跨账期或不属于本站点的户号，
       都会被拒绝；并发缴款由数据库行锁保证应收不会变为负数。</p>"""
    return page("代缴水费", body, user=user, nav=nav_for(user), err=err, info=info)


# ---------------------------------------------------------------- 收据

def receipt_view(ev, rev, water_confirmed, user, msg="", err=""):
    rev_block = ""
    if rev is not None:
        net = ev["amount"] + rev["amount"]
        net_word = "全额冲回，净缴为零" if net == 0 else f"净缴 {yuan(net)} 元"
        rev_block = f"""
        <div class="card" style="border-left:5px solid var(--red)">
          <h3 style="color:var(--red)">更正单（冲正）—— 与原单并列</h3>
          <table class="grid">
            <tr><th>冲正流水</th><td>{esc(rev['id'])}</td></tr>
            <tr><th>冲正金额</th>
                <td class="neg">−{yuan(abs(rev['amount']))} 元
                  （负数：冲减原缴款）</td></tr>
            <tr><th>渠道</th><td>{channel_label(rev['channel'])}</td></tr>
            <tr><th>批准主管</th><td>{esc(g(rev, 'approver_name') or rev['approved_by'])}</td></tr>
            <tr><th>引用原交易</th><td>{esc(rev['reverses_id'])}</td></tr>
            <tr><th>冲正原因</th><td>{esc(rev['reason'])}</td></tr>
            <tr><th>时间</th><td>{esc(rev['created_at'])}</td></tr>
          </table>
          <p><b>原单 + 更正单净额：</b>
             <span class="{'neg' if net < 0 else 'pos'}">{signed(net)} 元</span>
             <span class="muted">（{net_word}）</span></p>
        </div>"""

    status = ""
    if rev is not None:
        status = '<span class="tag unpaid">已冲正（保留冲正链，原收据不删除）</span>'
    elif ev["pay_status"] == "PENDING":
        status = '<span class="tag pending">支付到账延迟，待补登</span>'
    elif not water_confirmed:
        status = '<span class="tag pending">水务回执延迟，待补登</span>'
    else:
        status = '<span class="tag paid">有效</span>'

    actions = ""
    if user is not None and rev is None and ev["pay_status"] != "PENDING":
        actions = f"""
        <div class="card">
          <h3>发起冲正（输错户号 / 重复缴费 / 收据作废等）</h3>
          <form method="post" action="/reversal/request">
            <input type="hidden" name="original_id" value="{esc(ev['id'])}">
            <div class="formrow">
              <input type="text" name="reason" size="60"
                     placeholder="必填：冲正原因，如“户号输错，应缴人为 H1002”">
            </div>
            <button class="btn red" type="submit">提交主管审批</button>
          </form>
          <p class="muted">冲正不会删除原流水；须引用原交易并经主管批准后，
             生成金额为负的冲正单。</p>
        </div>"""

    body = f"""
    <div class="cols">
      <div class="card" style="border-top:5px solid var(--navy)">
        <h3 style="margin-top:0">原单（收据 {esc(ev['receipt_no'])}）{status}</h3>
        <table class="grid">
          <tr><th>户号</th><td>{esc(ev['account_no'])}</td></tr>
          <tr><th>户名代号</th><td>{esc(ev['name_code'])}</td></tr>
          <tr><th>账期</th><td>{esc(ev['period'])}</td></tr>
          <tr><th>应缴金额</th><td><b>{yuan(ev['amount_due'])} 元</b></td></tr>
          <tr><th>实收金额</th><td><b>{yuan(ev['amount'])} 元</b></td></tr>
          <tr><th>渠道</th><td>{channel_label(ev['channel'])}</td></tr>
          <tr><th>渠道流水</th><td>{esc(ev['pay_ref'])}</td></tr>
          <tr><th>水务回执</th><td>{esc(ev['water_ref'] or '延迟，稍后补登')}</td></tr>
          <tr><th>支付状态</th><td>{pay_status_label(ev['pay_status'])}</td></tr>
          <tr><th>站点 / 收款人</th><td>{esc(ev['site_name'])} /
              {esc(ev['operator_name'])}</td></tr>
          <tr><th>时间</th><td>{esc(ev['created_at'])}</td></tr>
        </table>
      </div>
      <div>{rev_block or '<div class="card muted">该收据尚无更正单。</div>'}</div>
    </div>
    {actions}
    <p><a class="btn ghost" href="/?account_no={esc(ev['account_no'])}">返回本户账单</a></p>"""
    return page("收据与冲正链", body, user=user, nav=nav_for(user), msg=msg, err=err)


# ---------------------------------------------------------------- 冲正审批

def reversals_view(user, pending, processed, err="", msg=""):
    pend_rows = ""
    if not pending:
        pend_rows = '<p class="muted">暂无待批冲正申请。</p>'
    for r in pending:
        pend_rows += f"""
        <div class="card" style="border-left:5px solid var(--amber)">
          <table class="grid">
            <tr><th>申请编号</th><td>{esc(r['id'])}</td>
                <th>申请时间</th><td>{esc(r['created_at'])}</td></tr>
            <tr><th>户号 / 户名代号</th>
                <td>{esc(r['account_no'])} / {esc(r['name_code'])}</td>
                <th>账期</th><td>{esc(r['period'])}</td></tr>
            <tr><th>原收据号</th><td>{esc(r['orig_receipt'])}</td>
                <th>原渠道</th><td>{channel_label(r['orig_channel'])}</td></tr>
            <tr><th>原金额</th><td>{yuan(r['orig_amount'])} 元</td>
                <th>冲正后金额</th>
                <td class="neg">−{yuan(abs(r['orig_amount']))} 元
                    （负数：冲减）</td></tr>
            <tr><th>冲正原因</th><td colspan="3">{esc(r['reason'])}</td></tr>
          </table>
          <form method="post" action="/reversal/{esc(r['id'])}/decide"
                style="display:inline">
            <input type="hidden" name="approve" value="1">
            <input type="text" name="note" placeholder="审批备注（可选）" size="30">
            <button class="btn" type="submit">批准并生成冲正单</button>
          </form>
          <form method="post" action="/reversal/{esc(r['id'])}/decide"
                style="display:inline;margin-left:10px">
            <input type="hidden" name="approve" value="0">
            <button class="btn red" type="submit">驳回</button>
          </form>
        </div>"""

    proc_rows = ""
    for r in processed:
        cls = "paid" if r["status"] == "APPROVED" else "unpaid"
        label = "已批准" if r["status"] == "APPROVED" else "已驳回"
        proc_rows += f"""<tr>
          <td>{esc(r['created_at'])}</td>
          <td>{esc(r['account_no'])}</td>
          <td>{esc(r['period'])}</td>
          <td>{esc(r['orig_receipt'])}</td>
          <td>{esc(r['reason'])}</td>
          <td><span class="tag {cls}">{label}</span></td>
        </tr>"""
    body = f"""
    <form method="post" action="/sync" style="margin-bottom:10px">
      <button class="btn ghost" type="submit">轮询模拟渠道，补登延迟回执</button>
      <span class="muted">（支付到账延迟 / 水务回执延迟在此补登，重复回执幂等）</span>
    </form>
    <h3>待主管审批</h3>
    {pend_rows}
    <h3>已处理</h3>
    <table class="grid">
      <tr><th>时间</th><th>户号</th><th>账期</th><th>原收据</th>
          <th>原因</th><th>结果</th></tr>
      {proc_rows or '<tr><td colspan="6" class="muted">无</td></tr>'}
    </table>"""
    return page("冲正审批", body, user=user, nav=nav_for(user), err=err, msg=msg)


# ---------------------------------------------------------------- 交班

def shift_view(user, current, events, totals, recent_shifts, err="", msg="", info=""):
    if current is None:
        body = f"""
        <div class="card">
          <p>当前没有进行中的班次。</p>
          <form method="post" action="/shift/open">
            <button class="btn" type="submit">开班（开始收款）</button>
          </form>
        </div>"""
    else:
        rows = ""
        for e in events:
            if e["kind"] == "REVERSAL":
                amt = f'<span class="neg">{signed(e["amount"])} 元（负数：现金冲减）</span>'
            elif e["kind"] == "SHORTAGE":
                amt = f'<span class="neg">{signed(e["amount"])} 元（负数：短款）</span>'
            else:
                amt = f'<span class="pos">{signed(e["amount"])} 元</span>'
            rows += f"""<tr>
              <td>{esc(e['seq'])}</td>
              <td>{esc(e['created_at'])}</td>
              <td>{esc(e['account_no'] or '—')}</td>
              <td>{esc(e['period'] or '—')}</td>
              <td>{kind_label(e['kind'])}</td>
              <td>{channel_label(e['channel'])}</td>
              <td>{amt}</td>
              <td>{esc(e['receipt_no'] or '—')}</td>
            </tr>"""
        diff_note = ""
        if totals["diff"] is not None:
            if totals["diff"] < 0:
                diff_note = (
                    f'<span class="neg">{signed(totals["diff"])} 元'
                    "（负数：现金短款，已登记赔付事件，不得改流水抹平）</span>"
                )
            elif totals["diff"] > 0:
                diff_note = f'<span class="pos">+{yuan(totals["diff"])} 元（长款）</span>'
            else:
                diff_note = "0.00 元（账实相符）"
        body = f"""
        <div class="card">
          <p>班次 {esc(current['id'])} ｜ 开班 {esc(current['opened_at'])} ｜ 进行中</p>
          <p class="stat">
            本班现金应收（缴款−冲正）：<b>{yuan(totals['expected'])} 元</b><br>
            本班电子支付（不入现金抽屉）：<b>{yuan(totals['electronic'])} 元</b><br>
            本班笔数：<b>{totals['count']}</b>（缴款 {totals['n_pay']} 笔，
            冲正 {totals['n_rev']} 笔）
          </p>
        </div>
        <div class="card">
          <h3>本班现金流水</h3>
          <table class="grid">
            <tr><th>#</th><th>时间</th><th>户号</th><th>账期</th><th>类型</th>
                <th>渠道</th><th>金额</th><th>收据号</th></tr>
            {rows or '<tr><td colspan="8" class="muted">本班暂无流水</td></tr>'}
          </table>
        </div>
        <div class="card">
          <h3>交班核对</h3>
          <form method="post" action="/shift/close">
            <label>实点现金（元）</label>
            <input type="text" name="counted_yuan" size="12"
                   value="{totals['expected']/100:.2f}">
            <label>备注</label><input type="text" name="note" size="30">
            <button class="btn" type="submit">交班结账</button>
          </form>
          <p class="muted">实点少于系统应收即为短款，系统写入负数 SHORTAGE 事件；
             试试把金额少填 10.00 元查看短款链。</p>
          {f'<p>账实差额：{diff_note}</p>' if diff_note else ''}
        </div>"""

    recent = "".join(
        f"""<tr>
          <td>{esc(s['id'])}</td>
          <td>{esc(s['opened_at'])}</td>
          <td>{esc(s['closed_at'] or '进行中')}</td>
          <td>{yuan(s['cash_counted']) if s['cash_counted'] is not None else '—'}</td>
        </tr>"""
        for s in recent_shifts[:10]
    )
    body += f"""
    <h3>历史班次</h3>
    <table class="grid">
      <tr><th>班次</th><th>开班</th><th>结账</th><th>实点现金（元）</th></tr>
      {recent or '<tr><td colspan="4" class="muted">无</td></tr>'}
    </table>"""
    return page("交班对账", body, user=user, nav=nav_for(user), err=err, msg=msg, info=info)


# ---------------------------------------------------------------- 现金记录

def cash_view(user, records, sites, selected_site, err=""):
    opts = '<option value="">全部站点</option>' + "".join(
        f'<option value="{esc(s["code"])}" {"selected" if s["code"]==selected_site else ""}>'
        f'{esc(s["code"])} {esc(s["name"])}</option>' for s in sites
    )
    rows = ""
    for e in records:
        if e["amount"] < 0:
            amt = f'<span class="neg">{yuan(e["amount"])} 元（负数：{kind_label(e["kind"])}）</span>'
        else:
            amt = f'<span class="pos">+{yuan(e["amount"])} 元</span>'
        link = f'<a href="/receipt/{esc(e["id"])}">原单</a>' if e["kind"] == "PAYMENT" else "—"
        void = (' <span class="tag unpaid">收据已作废</span>'
                if e["kind"] == "PAYMENT" and e["receipt_status"] == "VOID" else "")
        rows += f"""<tr>
          <td>{esc(e['site_name'])}</td>
          <td>{esc(e['seq'])}</td>
          <td>{esc(e['created_at'])}</td>
          <td>{esc(e['account_no'] or '—')}{void}</td>
          <td>{esc(e['period'] or '—')}</td>
          <td>{kind_label(e['kind'])}</td>
          <td>{amt}</td>
          <td>{g(e, 'reverses_id', '—') or '—'}</td>
          <td>{link}</td>
        </tr>"""
    body = f"""
    <form method="get" action="/cash" class="card">
      <label>站点筛选</label>
      <select name="site" {'disabled' if user['role']=='operator' else ''}>{opts}</select>
      {'<span class="muted">操作员仅可查看本服务点记录。</span>'
        if user['role']=='operator' else ''}
      <button class="btn" type="submit">查看</button>
    </form>
    <table class="grid">
      <tr><th>站点</th><th>#</th><th>时间</th><th>户号</th><th>账期</th>
          <th>类型</th><th>金额</th><th>冲正引用</th><th>凭证</th></tr>
      {rows or '<tr><td colspan="9" class="muted">无现金记录</td></tr>'}
    </table>"""
    return page("现金记录（站点隔离）", body, user=user, nav=nav_for(user), err=err)
