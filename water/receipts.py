"""演示收据：落地为本地文本文件（真实场景为针式打印机）。

三种单据：
- NORMAL  正常收款收据
- REDLINE 红字更正收据（冲正，金额为负，并引用原收据/原流水）
作废已打印收据通过冲正完成：原 NORMAL 单标记 voided 并链到红字单，绝不删除文件。
"""
import sqlite3
from pathlib import Path

from .money import cents_chinese, cents_to_yuan

REASON_CN = {
    "wrong_account": "输错户号",
    "duplicate": "重复缴费",
    "cross_period": "跨账期付款",
    "gateway": "支付/水务回执异常",
    "other": "其他原因",
    "shortage": "交班现金短款/长款",
}


def _width(s: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in s)


def _box(lines: list, width: int = 46) -> str:
    """元素：('title', 文本) 居中标题 / ('note', 文本) 备注行 / (label, value)。"""
    out = ["+" + "-" * width + "+"]
    for label, value in lines:
        if label in ("title", "note"):
            raw = value.strip()
            w = _width(raw)
            if w > width:
                raw, w = raw[: width // 2], _width(raw[: width // 2])
            left = (width - w) // 2
            out.append("|" + " " * max(left, 0) + raw + " " * max(width - left - w, 0) + "|")
            continue
        text = f"{label}：{value}"
        if _width(text) > width:
            text = text[: width - 1]
        pad = width - _width(text)
        out.append("|" + text + " " * max(pad, 0) + "|")
    out.append("+" + "-" * width + "+")
    return "\n".join(out)


def _method_label(t: sqlite3.Row) -> str:
    from .adapters import PaymentAdapter

    return PaymentAdapter.LABELS.get(t["channel"] or t["method"], t["method"] or "—")


def render_receipt(conn: sqlite3.Connection, receipt_no: str) -> str:
    r = conn.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
    if r is None:
        raise KeyError(receipt_no)
    t = conn.execute("SELECT * FROM ledger WHERE txn_id=?", (r["txn_id"],)).fetchone()
    site = conn.execute("SELECT name FROM sites WHERE site_id=?", (t["site_id"],)).fetchone()

    if r["kind"] == "REDLINE":
        orig_t = conn.execute(
            "SELECT * FROM ledger WHERE txn_id=?", (t["related_txn"],)
        ).fetchone()
        body = [
            ("title", "红字更正收据（冲正凭证）"),
            ("更正收据号", r["receipt_no"]),
            ("原收据号", r["linked_receipt_no"]),
            ("更正流水号", t["txn_id"]),
            ("原交易流水", t["related_txn"]),
            ("服务点", site["name"]),
            ("户号", t["account_no"] or "—"),
            ("账期", t["period"] or "—"),
            ("更正金额", f"-¥{cents_to_yuan(-t['amount_cents'])}（负数：{cents_chinese(t['amount_cents'])[1:]}）"),
            ("原渠道流水", orig_t["pay_ref"] or "现金（已退回居民）"),
            ("更正原因", REASON_CN.get(t["reason_code"], t["reason_code"] or "—")),
            ("说明", t["memo"] or "—"),
            ("打印时间", r["created_at"]),
            ("note", f"原收据 {r['linked_receipt_no']} 已作废，款项退回"),
        ]
    else:
        body = [
            ("title", "村镇便民点 水费代收收据"),
            ("收据号", r["receipt_no"]),
            ("流水号", t["txn_id"]),
            ("服务点", site["name"]),
            ("户号", t["account_no"]),
            ("账期", t["period"]),
            ("实收金额", f"¥{cents_to_yuan(t['amount_cents'])}（{cents_chinese(t['amount_cents'])}）"),
            ("收款方式", _method_label(t)),
            ("渠道流水", t["pay_ref"]),
            ("状态", "收款成功" if t["pay_status"] == "SUCCESS" else "支付回执处理中"),
            ("水务销账", {"CONFIRMED": "已销账", "PENDING": "销账确认中", "REJECTED": "销账被拒"}[t["water_status"]]),
            ("打印时间", r["created_at"]),
        ]
        if r["voided"]:
            body.append(("note", f"＊本收据已作废，见红字单 {r['linked_receipt_no']}"))
    return _box(body)


def write_receipt_file(conn: sqlite3.Connection, receipt_dir, receipt_no: str) -> str:
    text = render_receipt(conn, receipt_no)
    receipt_dir = Path(receipt_dir)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / f"{receipt_no}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)
