import sys
import types
import unittest

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
    sync_api.TimeoutError = type('TimeoutError', (Exception,), {})
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from autotest.engine.actions import do_search


class FakeLocator:
    def __init__(self, on_click=None):
        self._on_click = on_click

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self._on_click:
            self._on_click()


class FakeResponse:
    def json(self):
        return {"rows": []}


class FakeExpectResponseCM:
    """模拟 page.expect_response()：outcome 是 'timeout' 就在退出 with 块时
    抛超时，否则把 value 设成一个假响应——跟 Playwright 真实行为一致，
    超时发生在 __exit__（等匹配的响应），不是 __enter__。"""

    def __init__(self, outcome):
        self._outcome = outcome
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            return False
        if self._outcome == "timeout":
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded")
        self.value = FakeResponse()
        return False


class FakeSearchPage:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)   # 每次 expect_response 调用消费一个
        self.click_count = 0
        self.waits = []

    def locator(self, sel):
        return FakeLocator(on_click=lambda: setattr(self, "click_count", self.click_count + 1))

    def expect_response(self, predicate, timeout=None):
        return FakeExpectResponseCM(self._outcomes.pop(0))

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class FakeConfig:
    list_api = "/country/list"
    search_timeout = 30000
    selectors = {}


class FakeCtx:
    def __init__(self, outcomes):
        self.page = FakeSearchPage(outcomes)
        self.config = FakeConfig()
        self.last_api = None

    def selector(self, key):
        return "button:has-text('搜索')"


class SearchRetryTests(unittest.TestCase):
    """
    并发批量执行下，接口响应偶尔会卡过超时线（不是接口真的坏了，只是
    那一刻撞上并发高峰）——第一次超时不该直接判失败，retry 一次给它一个
    缓冲；两次都超时才是真的该报的问题。
    """

    def test_success_on_first_attempt_no_retry_mentioned(self):
        ctx = FakeCtx(["ok"])
        msg = do_search(ctx)
        self.assertEqual("执行搜索", msg)
        self.assertEqual(1, ctx.page.click_count)

    def test_timeout_then_success_retries_once(self):
        ctx = FakeCtx(["timeout", "ok"])
        msg = do_search(ctx)
        self.assertIn("重试后成功", msg)
        self.assertEqual(2, ctx.page.click_count)

    def test_two_timeouts_raises(self):
        ctx = FakeCtx(["timeout", "timeout"])
        with self.assertRaises(PlaywrightTimeoutError):
            do_search(ctx)
        self.assertEqual(2, ctx.page.click_count)

    def test_uses_configured_search_timeout(self):
        # search_timeout 页面配置能覆盖默认值，不是写死的 30000
        seen_timeouts = []
        ctx = FakeCtx(["ok"])
        ctx.config.search_timeout = 45000
        orig = ctx.page.expect_response
        def spy(predicate, timeout=None):
            seen_timeouts.append(timeout)
            return orig(predicate, timeout=timeout)
        ctx.page.expect_response = spy
        do_search(ctx)
        self.assertEqual([45000], seen_timeouts)


if __name__ == "__main__":
    unittest.main()
