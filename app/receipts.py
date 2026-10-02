"""演示收据：本地 HTML 文件 + 页面内嵌版本。

白底深蓝；负数以符号（−）与文字（冲减/短款）同时说明。
"""
from __future__ import annotations

import html

from .money import channel_label, yuan


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def render_receipt_html(ev, rev=None, water_ok: bool = True) -> str:
    """ev / rev 为带 account_no/name_code/period/operator_name 的 Row。"""
    status_badge = ""
    if rev is not None:
        status_badge = (
            '<span class="badge badge-void">已冲正（原收据不作废删除，'
            "保留冲正链）</span>"
        )
    elif ev["pay_status"] == "PENDING":
        status_badge = '<span class="badge badge-pending">支付到账延迟，待补登</span>'
    elif not water_ok:
        status_badge = '<span class="badge badge-pending">水务回执延迟，待补登</span>'
    else:
        status_badge = '<span class="badge badge-ok">有效</span>'

    rev_block = ""
    if rev is not None:
        net = ev["amount"] + rev["amount"]
        net_word = "全额冲回，净缴为零" if net == 0 else f"净缴 {yuan(net)} 元"
        rev_block = f"""
        <div class="rev">
          <h3>更正单（冲正）</h3>
          <table class="kv">
            <tr><th>冲正流水</th><td>{_esc(rev['id'])}</td></tr>
            <tr><th>冲正金额</th><td class="neg">−{yuan(abs(rev['amount']))} 元
                <span class="negword">（负数：冲减原缴款）</span></td></tr>
            <tr><th>渠道</th><td>{channel_label(rev['channel'])}</td></tr>
            <tr><th>批准主管</th><td>{_esc(rev['approver_name'])}</td></tr>
            <tr><th>冲正原因</th><td>{_esc(rev['reason'])}</td></tr>
            <tr><th>时间</th><td>{_esc(rev['created_at'])}</td></tr>
          </table>
          <p class="net">该账单净缴：
            <span class="neg">{yuan(net)} 元<span class="negword">（{net_word}）</span></span>
          </p>
        </div>"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>水费收据 {_esc(ev['receipt_no'])}</title>
<style>
  body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
         margin: 32px; color: #1a2433; background: #fff; }}
  .paper {{ max-width: 680px; margin: 0 auto; border: 2px solid #0b3d91;
           border-radius: 10px; padding: 28px 34px; }}
  h1 {{ color: #0b3d91; margin: 0 0 4px; font-size: 22px; }}
  .sub {{ color: #4a5a72; font-size: 13px; margin-bottom: 18px; }}
  table.kv {{ width: 100%; border-collapse: collapse; margin: 8px 0 18px; }}
  table.kv th {{ text-align: left; width: 110px; color: #0b3d91;
                padding: 7px 10px; background: #eef3fb; }}
  table.kv td {{ padding: 7px 10px; border-bottom: 1px solid #d6e0f0; }}
  .amount {{ font-size: 20px; font-weight: 700; color: #0b3d91; }}
  .badge {{ padding: 2px 10px; border-radius: 12px; font-size: 12px; }}
  .badge-ok {{ background: #e6f4ea; color: #1e6b3a; }}
  .badge-void {{ background: #fdecea; color: #a5281f; }}
  .badge-pending {{ background: #fff6e0; color: #8a6110; }}
  .rev {{ border-top: 2px dashed #0b3d91; margin-top: 14px; padding-top: 14px; }}
  .rev h3 {{ color: #a5281f; margin: 0 0 8px; }}
  .neg {{ color: #a5281f; font-weight: 700; }}
  .negword {{ font-weight: 400; font-size: 12px; }}
  .net {{ background: #f6f8fc; padding: 10px; border-radius: 6px; }}
  .foot {{ margin-top: 22px; color: #4a5a72; font-size: 12px; }}
</style></head>
<body><div class="paper">
  <h1>村镇便民服务点 · 水费代收收据</h1>
  <div class="sub">{_esc(ev['site_name'])} ｜ 收据号 {_esc(ev['receipt_no'])}
       ｜ {status_badge}</div>
  <table class="kv">
    <tr><th>户号</th><td>{_esc(ev['account_no'])}</td></tr>
    <tr><th>户名代号</th><td>{_esc(ev['name_code'])}</td></tr>
    <tr><th>账期</th><td>{_esc(ev['period'])}</td></tr>
    <tr><th>应缴金额</th><td class="amount">{yuan(ev['amount_due'])} 元</td></tr>
    <tr><th>实收金额</th><td class="amount">{yuan(ev['amount'])} 元</td></tr>
    <tr><th>支付渠道</th><td>{channel_label(ev['channel'])}</td></tr>
    <tr><th>渠道流水</th><td>{_esc(ev['pay_ref'])}</td></tr>
    <tr><th>水务回执</th><td>{_esc(ev['water_ref'] or '回执延迟，稍后补登')}</td></tr>
    <tr><th>收款人</th><td>{_esc(ev['operator_name'])}</td></tr>
    <tr><th>收款时间</th><td>{_esc(ev['created_at'])}</td></tr>
  </table>
  {rev_block}
  <div class="foot">本收据为演示凭证；冲正须引用原交易并经主管批准，流水不删除。</div>
</div></body></html>"""
