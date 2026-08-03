import sys
import types
import unittest

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
    sync_api.TimeoutError = type("TimeoutError", (Exception,), {})
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine.models import Case, Status
from autotest.engine.runner import run_case


class TimeoutResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            raise RuntimeError("configured response did not arrive")
        return False


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
        self.url = "http://x.test/web/account/rechargeRecord"
        self.expect_timeouts = []
        self.waits = []

    def expect_response(self, predicate, timeout=None):
        self.expect_timeouts.append(timeout)
        return TimeoutResponse()

    def goto(self, url, **kwargs):
        self.url = url
        self.ctx.api_log.append({
            "url": "http://x.test/api/vaWeb/account/recharge/record/page?pageNum=1",
            "status": 200,
            "response_body": '{"code":200,"data":{"records":[],"total":0}}',
        })

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class FakeConfig:
    list_api = "/api/vaWeb/business/franchisee/list"
    login = {}
    url = "http://x.test/web/account/rechargeRecord"


class FakeCtx:
    def __init__(self):
        self.config = FakeConfig()
        self.api_log = []
        self.console_errors = []
        self.failed_requests = []
        self.target_language = None
        self.last_api = None
        self.page = FakePage(self)

    def reset_signals(self):
        self.api_log.clear()
        self.console_errors.clear()
        self.failed_requests.clear()


class NavigationListApiRecoveryTests(unittest.TestCase):
    def test_navigation_corrects_stale_dropdown_list_api(self):
        ctx = FakeCtx()

        result = run_case(ctx, Case(name="列表默认加载", steps=[]))

        self.assertEqual(Status.PASS, result.status)
        self.assertEqual(
            "/api/vaWeb/account/recharge/record/page",
            ctx.config.list_api,
        )
        self.assertEqual([5000], ctx.page.expect_timeouts)
        self.assertEqual([500], ctx.page.waits)


if __name__ == "__main__":
    unittest.main()
