import sys
import types
import unittest


# 纯逻辑单测：断言不需要真实浏览器，桩掉 playwright / yaml 即可导入。
if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine.actions import (AssertionFailed, AssertionWarning,
                                     as_no_console, as_no_failed_req,
                                     as_no_render_garbage, as_row_count,
                                     do_check_select_options)


class FakeUI:
    def __init__(self, rows):
        self._rows = rows

    def table_data(self, page):
        return self._rows


class FakeCtx:
    def __init__(self, rows):
        self.ui = FakeUI(rows)
        self.page = None


class RenderGarbageTests(unittest.TestCase):
    def test_clean_table_passes(self):
        ctx = FakeCtx([
            {"国家名称": "加纳", "货币符号": "GH₵", "创建时间": "2026-07-14 10:00:19"},
            {"国家名称": "中国", "货币符号": "¥", "创建时间": "2026-07-24 14:16:26"},
        ])
        self.assertIn("渲染检查通过", as_no_render_garbage(ctx))

    def test_empty_table_is_skipped(self):
        self.assertIn("跳过", as_no_render_garbage(FakeCtx([])))

    def test_object_object_is_flagged(self):
        ctx = FakeCtx([{"名称": "[object Object]"}])
        with self.assertRaises(AssertionFailed):
            as_no_render_garbage(ctx)

    def test_undefined_and_nan_flagged(self):
        with self.assertRaises(AssertionFailed):
            as_no_render_garbage(FakeCtx([{"金额": "undefined"}]))
        with self.assertRaises(AssertionFailed):
            as_no_render_garbage(FakeCtx([{"数量": "NaN"}]))

    def test_raw_timestamp_in_time_column_flagged(self):
        ctx = FakeCtx([{"创建时间": "1690000000000"}])
        with self.assertRaises(AssertionFailed):
            as_no_render_garbage(ctx)

    def test_raw_number_outside_time_column_is_ok(self):
        # 非时间列的长数字（如订单号）不应误报
        ctx = FakeCtx([{"订单号": "1690000000000"}])
        self.assertIn("渲染检查通过", as_no_render_garbage(ctx))

    def test_columns_filter_limits_scan(self):
        # 只检查指定列时，其他列的垃圾值不触发失败
        ctx = FakeCtx([{"名称": "正常", "备注": "[object Object]"}])
        self.assertIn("渲染检查通过", as_no_render_garbage(ctx, columns=["名称"]))


class FakeLocator:
    @property
    def first(self):
        return self

    def click(self, timeout=None):
        pass


class FakeSelectUI:
    """模拟下拉：list_options 返回候选项，select 按官方 adapter 语义返回选中文本。"""

    def __init__(self, options):
        self._options = options

    def list_options(self, page, label):
        return self._options

    def select(self, page, label, option=None, index=None):
        return option


class FakeConfig:
    list_api = None
    selectors = {}


class FakeSearchCtx:
    def __init__(self, options):
        self.ui = FakeSelectUI(options)
        self.page = types.SimpleNamespace(
            locator=lambda sel: FakeLocator(),
            wait_for_timeout=lambda ms: None,
        )
        self.vars = {}
        self.console_errors = []
        self.config = FakeConfig()

    def selector(self, key):
        return "button:has-text('搜索')"


class CheckSelectOptionsTests(unittest.TestCase):
    def test_selected_var_is_set_for_downstream_assertion(self):
        # 回归用例：check_select_options 曾经漏掉 ctx.vars 赋值，导致生成的
        # 「筛选-X」用例里 ${selected_X} 永远解析不出来、断言必然失败——
        # 这是「让人以为系统有 bug，其实是工具自己的问题」的典型假失败。
        ctx = FakeSearchCtx(["生效", "失效"])
        do_check_select_options(ctx, label="状态")
        self.assertIn("selected_状态", ctx.vars)
        self.assertEqual(ctx.vars["selected_状态"], "失效")  # 停在最后一个选项

    def test_placeholder_options_are_excluded(self):
        ctx = FakeSearchCtx(["全部", "请选择", "开启", "关闭"])
        do_check_select_options(ctx, label="是否包车")
        self.assertEqual(ctx.vars["selected_是否包车"], "关闭")

    def test_no_real_options_returns_skip_message(self):
        ctx = FakeSearchCtx(["全部"])
        msg = do_check_select_options(ctx, label="状态")
        self.assertIn("无可选项", msg)
        self.assertNotIn("selected_状态", ctx.vars)


class FakeSignalCtx:
    """带 console_errors/failed_requests 信号的假 ctx，用于测试严重程度降级。"""

    def __init__(self, rows=None, row_count=0, console_errors=None, failed_requests=None):
        self.ui = FakeUI(rows or [])
        self.ui.row_count = lambda page: row_count
        self.page = None
        self.console_errors = console_errors or []
        self.failed_requests = failed_requests or []


class SeverityDowngradeTests(unittest.TestCase):
    """
    回归用例：控制台报错/失败请求不该在页面内容本身正常时拖垮整条用例
    （只在真正内容出问题的断言失败时，才作为诊断信息带出来）。
    """

    def test_console_error_is_warning_not_failure(self):
        ctx = FakeSignalCtx(console_errors=["error: Failed to load resource: 404"])
        with self.assertRaises(AssertionWarning):
            as_no_console(ctx)
        # 明确不是 AssertionFailed
        try:
            as_no_console(ctx)
        except AssertionWarning:
            pass
        except AssertionFailed:
            self.fail("控制台报错不应该判成 AssertionFailed")

    def test_failed_request_is_warning_not_failure(self):
        ctx = FakeSignalCtx(failed_requests=["404 http://x/flag.png"])
        with self.assertRaises(AssertionWarning):
            as_no_failed_req(ctx)

    def test_no_errors_passes_cleanly(self):
        ctx = FakeSignalCtx()
        self.assertIn("无控制台报错", as_no_console(ctx))
        self.assertIn("无失败请求", as_no_failed_req(ctx))

    def test_content_failure_carries_console_error_as_context(self):
        # 页面真出问题（行数不对）时，失败信息里应该带上当时的控制台报错做线索
        ctx = FakeSignalCtx(row_count=0, console_errors=["error: 404 not found"])
        with self.assertRaises(AssertionFailed) as cm:
            as_row_count(ctx, min=1)
        self.assertIn("404", str(cm.exception))

    def test_content_failure_without_signals_has_no_dangling_context(self):
        ctx = FakeSignalCtx(row_count=0)
        with self.assertRaises(AssertionFailed) as cm:
            as_row_count(ctx, min=1)
        self.assertNotIn("可能相关", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
