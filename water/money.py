"""金额一律以整数「分」存储与运算，杜绝浮点误差。"""


def yuan_to_cents(text: str) -> int:
    """把 '12.3' / '12.30' / '12' 解析为分。"""
    s = str(text).strip().replace("￥", "").replace("¥", "").replace(",", "")
    if not s:
        raise ValueError("金额为空")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if "." in s:
        whole, frac = s.split(".", 1)
        if len(frac) > 2:
            raise ValueError("金额最小单位为分")
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    if not whole:
        whole = "0"
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"非法金额：{text}")
    value = int(whole) * 100 + int(frac)
    return -value if neg else value


def cents_to_yuan(value: int) -> str:
    """分 -> '12.30'，保留符号。"""
    sign = "-" if value < 0 else ""
    v = abs(value)
    return f"{sign}{v // 100}.{v % 100:02d}"


def fmt_money(value: int) -> str:
    """带人民币符号：¥12.30"""
    return f"¥{cents_to_yuan(value)}"


_CN_DIGIT = "零壹贰叁肆伍陆柒捌玖"
_CN_UNIT = ["", "拾", "佰", "仟"]
_CN_GROUP = ["", "万", "亿", "万亿"]


def _four_cn(n: int) -> str:
    """0..9999 的中文大写。"""
    out = []
    zero = False
    for pos in range(3, -1, -1):
        d = (n // (10 ** pos)) % 10
        if d == 0:
            zero = bool(out)
        else:
            if zero:
                out.append("零")
                zero = False
            out.append(_CN_DIGIT[d] + _CN_UNIT[pos])
    return "".join(out)


def cents_chinese(value: int) -> str:
    """金额中文大写；负数以「负」开头。"""
    if value == 0:
        return "零元整"
    prefix = "负" if value < 0 else ""
    v = abs(value)
    yuan, fen = divmod(v, 100)
    jiao, fen = divmod(fen, 10)

    groups = []
    n = yuan
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    parts = []
    need_zero = False
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            need_zero = True
            continue
        if parts and (need_zero or g < 1000):
            parts.append("零")
        parts.append(_four_cn(g))
        parts.append(_CN_GROUP[i])
        need_zero = False
    text = "".join(parts) + "元"
    if jiao == 0 and fen == 0:
        text += "整"
    else:
        if jiao:
            text += _CN_DIGIT[jiao] + "角"
        elif fen:
            text += "零"
        if fen:
            text += _CN_DIGIT[fen] + "分"
        else:
            text += "整"
    return prefix + text


def fmt_signed(cents: int) -> str:
    """负数同时以符号与中文文字说明：-¥46.00（负：肆拾陆元整）。正数仅普通显示。"""
    if cents < 0:
        return f"-¥{cents_to_yuan(-cents)}（负：{cents_chinese(cents)[1:]}）"
    return f"¥{cents_to_yuan(cents)}"
