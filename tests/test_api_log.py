import sys
import tempfile
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

from autotest.engine.models import PageConfig
from autotest.engine.runner import Context, _API_LOG_LIMIT


class FakePage:
    """真实 Playwright Page 的极简替身：只要能注册/触发事件回调。"""

    def __init__(self):
        self._handlers = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def trigger(self, event, *args):
        for h in list(self._handlers.get(event, [])):
            h(*args)


class FakeRequest:
    def __init__(self, method="GET", url="http://x.test/api/country/list?name=a",
                headers=None, post_data=None):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.post_data = post_data


class FakeApiResponse:
    def __init__(self, request, status=200, content_type="application/json;charset=utf-8",
                text='{"rows":[]}'):
        self.request = request
        self.status = status
        self.url = request.url
        self.headers = {"content-type": content_type}
        self._text = text

    def text(self):
        return self._text


def _make_ctx():
    tmp = tempfile.mkdtemp()
    return Context(FakePage(), PageConfig(name="x", url="http://x.test"), tmp)


class ApiLogRedactionTests(unittest.TestCase):
    """
    接口调用记录会整份写进报告文件（会被保存/分享），Cookie/Authorization
    这类请求头是登录凭证本身，原样记下去等于把会话令牌写进一份不受权限
    控制的静态文件——必须在记录时就替换掉，不能指望事后再清理。
    """

    def test_cookie_and_authorization_are_redacted(self):
        ctx = _make_ctx()
        req = FakeRequest(headers={"Cookie": "session=secret123", "Authorization": "Bearer tok",
                                   "X-Custom": "keep-me"})
        ctx.page.trigger("request", req)
        ctx.page.trigger("response", FakeApiResponse(req))
        self.assertEqual(1, len(ctx.api_log))
        headers = ctx.api_log[0]["request_headers"]
        self.assertEqual("[已隐藏]", headers["Cookie"])
        self.assertEqual("[已隐藏]", headers["Authorization"])
        self.assertEqual("keep-me", headers["X-Custom"])

    def test_redaction_is_case_insensitive(self):
        ctx = _make_ctx()
        req = FakeRequest(headers={"cookie": "session=secret123"})
        ctx.page.trigger("request", req)
        ctx.page.trigger("response", FakeApiResponse(req))
        self.assertEqual("[已隐藏]", ctx.api_log[0]["request_headers"]["cookie"])


class ApiLogFilteringTests(unittest.TestCase):
    def test_non_json_response_not_logged(self):
        ctx = _make_ctx()
        req = FakeRequest(url="http://x.test/static/logo.png")
        ctx.page.trigger("request", req)
        ctx.page.trigger("response", FakeApiResponse(req, content_type="image/png", text=""))
        self.assertEqual(0, len(ctx.api_log))

    def test_json_response_captures_core_fields(self):
        ctx = _make_ctx()
        req = FakeRequest(method="POST", url="http://x.test/api/country/save",
                          post_data='{"name":"x"}')
        ctx.page.trigger("request", req)
        ctx.page.trigger("response", FakeApiResponse(req, status=200, text='{"ok":true}'))
        entry = ctx.api_log[0]
        self.assertEqual("POST", entry["method"])
        self.assertEqual(200, entry["status"])
        self.assertEqual('{"name":"x"}', entry["request_body"])
        self.assertEqual('{"ok":true}', entry["response_body"])
        self.assertIsNotNone(entry["duration_ms"])

    def test_limit_caps_entries_per_case(self):
        ctx = _make_ctx()
        for i in range(_API_LOG_LIMIT + 10):
            req = FakeRequest(url=f"http://x.test/api/list?i={i}")
            ctx.page.trigger("request", req)
            ctx.page.trigger("response", FakeApiResponse(req))
        self.assertEqual(_API_LOG_LIMIT, len(ctx.api_log))


class ApiLogResetTests(unittest.TestCase):
    def test_reset_signals_clears_api_log(self):
        ctx = _make_ctx()
        req = FakeRequest()
        ctx.page.trigger("request", req)
        ctx.page.trigger("response", FakeApiResponse(req))
        self.assertEqual(1, len(ctx.api_log))
        ctx.reset_signals()
        self.assertEqual(0, len(ctx.api_log))


if __name__ == "__main__":
    unittest.main()
