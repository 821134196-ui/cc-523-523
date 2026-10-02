"""金额与展示工具。金额一律以分（整数）存储。"""
from __future__ import annotations


def yuan(cents: int) -> str:
    """分 -> 元字符串，如 12345 -> '123.45'。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def signed(cents: int) -> str:
    """带符号金额，正数显式 '+'。"""
    return ("+" if cents >= 0 else "-") + yuan(abs(cents))


def negative_word(cents: int) -> str:
    """负数同时以符号与文字说明（满足“负数以符号和文字同时说明”）。"""
    if cents < 0:
        return f"{yuan(cents)} 元（负数：冲减/短款）"
    return f"{yuan(cents)} 元"


def channel_label(ch: str | None) -> str:
    return {"CASH": "现金", "ELECTRONIC": "电子支付"}.get(ch or "", ch or "—")


def kind_label(kind: str) -> str:
    return {
        "PAYMENT": "缴款",
        "PAY_CONFIRM": "支付到账确认",
        "WATER_CONFIRM": "水务销账回执",
        "REVERSAL": "冲正",
        "SHORTAGE": "现金短款",
        "SHORTAGE_REPAY": "短款补缴",
    }.get(kind, kind)


def pay_status_label(st: str | None) -> str:
    return {"PENDING": "支付到账延迟", "SUCCESS": "支付成功"}.get(st or "", st or "—")
