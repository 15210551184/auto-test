import sys
import types
import unittest


# 本地单元测试只验证纯进度回调，不需要安装浏览器运行时。
if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.TimeoutError = type('TimeoutError', (Exception,), {})
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine.crawler import _progress, _route_url


class CrawlerProgressTests(unittest.TestCase):
    def test_progress_callback_receives_message(self):
        messages = []

        _progress(messages.append, "正在展开 2/5：订单管理")

        self.assertEqual(["正在展开 2/5：订单管理"], messages)

    def test_route_url_keeps_application_prefix(self):
        self.assertEqual("http://example.test/web/charging/priceTemplate",
                         _route_url("http://example.test/web/index", "/charging/priceTemplate"))
