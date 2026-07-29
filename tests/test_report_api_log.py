import tempfile
import unittest
from pathlib import Path

from autotest.engine.models import CaseResult, PageResult, Status, StepResult
from autotest.engine.report import _api_log_html, render


class ApiLogHtmlTests(unittest.TestCase):
    def test_empty_calls_render_nothing(self):
        self.assertEqual("", _api_log_html([]))

    def test_renders_method_url_status_duration(self):
        html_out = _api_log_html([{
            "method": "GET", "url": "http://x.test/api/list?a=1",
            "status": 200, "duration_ms": 123,
            "request_headers": {"Cookie": "[已隐藏]"},
            "request_body": None, "response_body": '{"ok":true}',
        }])
        self.assertIn("接口调用（1 次）", html_out)
        self.assertIn("GET", html_out)
        self.assertIn("http://x.test/api/list?a=1", html_out)
        self.assertIn("200", html_out)
        self.assertIn("123ms", html_out)
        self.assertIn("[已隐藏]", html_out)

    def test_failed_status_gets_error_class(self):
        html_out = _api_log_html([{
            "method": "GET", "url": "http://x.test/api/x", "status": 500,
            "duration_ms": 10, "request_headers": {}, "request_body": None,
            "response_body": "boom",
        }])
        self.assertIn('class="status err"', html_out)

    def test_request_body_shown_only_when_present(self):
        with_body = _api_log_html([{
            "method": "POST", "url": "http://x.test/api/save", "status": 200,
            "duration_ms": 5, "request_headers": {}, "request_body": '{"name":"a"}',
            "response_body": "{}",
        }])
        self.assertIn("请求参数", with_body)
        without_body = _api_log_html([{
            "method": "GET", "url": "http://x.test/api/list", "status": 200,
            "duration_ms": 5, "request_headers": {}, "request_body": None,
            "response_body": "{}",
        }])
        self.assertNotIn("请求参数", without_body)

    def test_html_special_chars_are_escaped(self):
        # 响应体/URL 里出现真实业务数据时可能带 <script> 之类字符——不转义
        # 会直接在报告页面里被当成 HTML 执行，是个 XSS 口子。
        html_out = _api_log_html([{
            "method": "GET", "url": "http://x.test/api?x=<script>alert(1)</script>",
            "status": 200, "duration_ms": 1, "request_headers": {},
            "request_body": None, "response_body": "<img src=x onerror=alert(1)>",
        }])
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertNotIn("<img src=x onerror=alert(1)>", html_out)
        self.assertIn("&lt;script&gt;", html_out)


class RenderIncludesApiLogTests(unittest.TestCase):
    def test_case_without_api_calls_has_no_section(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.html"
            case = CaseResult("列表默认加载", Status.PASS,
                              [StepResult("assert_row_count", {}, Status.PASS)])
            render([PageResult("国家管理", "http://x.test", [case])], str(out))
            text = out.read_text(encoding="utf-8")
            self.assertNotIn("接口调用", text)

    def test_case_with_api_calls_renders_section(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.html"
            case = CaseResult("搜索-国家名称", Status.PASS,
                              [StepResult("search", {}, Status.PASS)],
                              api_calls=[{
                                  "method": "GET", "url": "http://x.test/api/country/list",
                                  "status": 200, "duration_ms": 234,
                                  "request_headers": {"Authorization": "[已隐藏]"},
                                  "request_body": None, "response_body": "{}",
                              }])
            render([PageResult("国家管理", "http://x.test", [case])], str(out))
            text = out.read_text(encoding="utf-8")
            self.assertIn("接口调用（1 次）", text)
            self.assertIn("[已隐藏]", text)


class CollapsibleReportTests(unittest.TestCase):
    """
    页面区块（跟用例一样）现在也是 <details>，能收起/展开；顶部有
    "全部展开"/"全部收起"按钮一次性切换所有 details（页面级 + 用例级）。
    """

    def _render(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.html"
            case = CaseResult("列表默认加载", Status.PASS,
                              [StepResult("assert_row_count", {}, Status.PASS)])
            render([PageResult("国家管理", "http://x.test/country", [case])], str(out))
            return out.read_text(encoding="utf-8")

    def test_page_section_is_a_details_element(self):
        text = self._render()
        self.assertIn('<details class="page" open><summary>国家管理', text)

    def test_toolbar_has_expand_and_collapse_all_buttons(self):
        text = self._render()
        self.assertIn("全部展开", text)
        self.assertIn("全部收起", text)
        self.assertIn("document.querySelectorAll('details')", text)

    def test_page_link_click_does_not_toggle_collapse(self):
        # 点页面标题里的链接应该只是跳转，不应该顺带把这个 <details> 收起来
        text = self._render()
        self.assertIn('onclick="event.stopPropagation()"', text)


if __name__ == "__main__":
    unittest.main()
