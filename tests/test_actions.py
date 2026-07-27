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

from autotest.engine.actions import AssertionFailed, as_no_render_garbage


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


if __name__ == "__main__":
    unittest.main()
