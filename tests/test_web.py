"""HTTP 端到端测试：在临时端口启动 ThreadingHTTPServer，走真实会话。"""
from __future__ import annotations

import http.cookiejar
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from app import db
from app.adapters import PaymentAdapter, WaterAdapter
from app.seed import seed
from app.web import DB_PATH, RECEIPT_DIR, Handler, Kernel


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WATERFEE_DB"] = os.path.join(cls.tmp.name, "waterfee.db")
        os.environ["WATERFEE_RECEIPTS"] = os.path.join(cls.tmp.name, "receipts")
        # 重新绑定模块级路径常量
        import app.web as web
        web.DB_PATH = os.environ["WATERFEE_DB"]
        web.RECEIPT_DIR = os.environ["WATERFEE_RECEIPTS"]
        cls.kernel = Kernel(
            db_path=web.DB_PATH,
            payments=PaymentAdapter(), water=WaterAdapter(),
            receipt_dir=web.RECEIPT_DIR,
        )
        conn = db.connect(web.DB_PATH)
        db.init_db(conn)
        conn.close()
        # 用内核主线程 App（含 user()）播种
        seed(cls.kernel.app(), verbose=False)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Handler.kernel = cls.kernel
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def client(self):
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))

        def get(path, expect=None):
            try:
                return op.open(self.base + path).read().decode()
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                if expect is not None:
                    self.assertEqual(e.code, expect)
                    return body
                raise

        def post(path, data, expect=None):
            req = urllib.request.Request(
                self.base + path, urllib.parse.urlencode(data).encode())
            try:
                r = op.open(req)
                return r.geturl(), r.read().decode()
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                if expect is not None:
                    self.assertEqual(e.code, expect)
                    return e.url, body
                raise

        return get, post

    def login(self, post, uid):
        post("/login", {"user_id": uid})

    def test_public_query_and_bad_account(self):
        get, _ = self.client()
        h = get("/?account_no=H1001")
        self.assertIn("青-A-031", h)
        h = get("/?account_no=NOPE", expect=400)
        self.assertIn("不存在", h)

    def test_pay_confirm_zone_fixed_and_reversal_flow(self):
        get, post = self.client()
        self.login(post, "U01")  # 张三 青石
        # 确认区固定展示应缴金额
        h = get("/pay?account_no=H1001&period=2026-09")
        self.assertIn("收款确认区", h)
        self.assertIn("41.50", h)
        # 金额不符被拒
        _, b = post("/pay", {"account_no": "H1001", "period": "2026-09",
                             "amount_yuan": "40.00", "channel": "CASH"}, expect=400)
        self.assertIn("应缴不一致", b)
        # 正确收款
        url, receipt = post("/pay", {"account_no": "H1001", "period": "2026-09",
                                     "amount_yuan": "41.50", "channel": "CASH"})
        ev_id = url.rstrip("/").split("/")[-1].split("?")[0]
        self.assertTrue(ev_id.startswith("EVT_"))
        # 重复缴费
        _, b = post("/pay", {"account_no": "H1001", "period": "2026-09",
                             "amount_yuan": "41.50", "channel": "CASH"}, expect=400)
        self.assertIn("已缴清", b)
        # 发起冲正
        post("/reversal/request", {"original_id": ev_id, "reason": "HTTP 冲正测试"})

        # 主管批准
        get2, post2 = self.client()
        self.login(post2, "U09")
        h = get2("/reversals")
        card = [c for c in re.split(r'<div class="card" style="border-left', h)
                if "HTTP 冲正测试" in c]
        self.assertTrue(card)
        rid = re.findall(r"/reversal/(RR_[0-9a-f]+)/decide", card[0])[0]
        post2(f"/reversal/{rid}/decide", {"approve": "1", "note": "同意"})
        h = get2(f"/receipt/{ev_id}")
        self.assertIn("更正单", h)
        self.assertIn("负数：冲减原缴款", h)
        # 本地演示收据文件已重写为原单+更正单并列
        files = os.listdir(os.environ["WATERFEE_RECEIPTS"])
        self.assertTrue(files)

    def test_site_isolation_over_http(self):
        get, post = self.client()
        self.login(post, "U01")
        # 青石操作员看不到梧岗户号的代缴入口
        h = get("/?account_no=H3001")
        self.assertIn("非本站点户", h)
        # 现金记录不含梧岗数据
        h = get("/cash")
        self.assertNotIn("H3001", h)
        # 强传其他站点参数被拒
        b = get("/cash?site=WG-02", expect=400)
        self.assertIn("本服务点", b)

        # 主管跨站点可见
        get2, post2 = self.client()
        self.login(post2, "U09")
        h = get2("/cash")
        self.assertIn("梧岗镇", h)

    def test_supervisor_only_reversal_page(self):
        get, post = self.client()
        self.login(post, "U01")
        get("/reversals", expect=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
