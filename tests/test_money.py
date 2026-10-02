"""金额工具：分整数运算、中文大写、负数符号+文字双重说明。"""
from water.money import (cents_chinese, cents_to_yuan, fmt_money, fmt_signed,
                         yuan_to_cents)
import unittest


class TestMoney(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(yuan_to_cents("12.3"), 1230)
        self.assertEqual(yuan_to_cents("12"), 1200)
        self.assertEqual(yuan_to_cents("0.01"), 1)
        self.assertEqual(yuan_to_cents("-46.00"), -4600)
        self.assertEqual(cents_to_yuan(1230), "12.30")
        self.assertEqual(cents_to_yuan(-4600), "-46.00")
        self.assertEqual(fmt_money(5), "¥0.05")

    def test_chinese(self):
        self.assertEqual(cents_chinese(0), "零元整")
        self.assertEqual(cents_chinese(4600), "肆拾陆元整")
        self.assertEqual(cents_chinese(1234), "壹拾贰元叁角肆分")
        self.assertEqual(cents_chinese(20000), "贰佰元整")
        self.assertEqual(cents_chinese(100000000), "壹佰万元整")
        self.assertEqual(cents_chinese(10000000000), "壹亿元整")
        self.assertEqual(cents_chinese(-4600), "负肆拾陆元整")

    def test_signed_negative_shows_symbol_and_words(self):
        s = fmt_signed(-1200)
        self.assertIn("-¥12.00", s)          # 符号
        self.assertIn("负", s)               # 文字
        self.assertIn("壹拾贰元整", s)

    def test_roundtrip_many_values(self):
        for y in ("0.00", "0.10", "9.99", "100.00", "38.70", "10000.01"):
            self.assertEqual(cents_to_yuan(yuan_to_cents(y)), y)


if __name__ == "__main__":
    unittest.main()
