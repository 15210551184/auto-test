import sys
import types
import unittest


# 纯逻辑单测：断言不需要真实浏览器，桩掉 playwright / yaml 即可导入。
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

from autotest.engine.actions import (AssertionFailed, AssertionWarning,
                                     as_col_all,
                                     as_no_console, as_no_failed_req,
                                     as_no_i18n_leak, as_no_mixed_language,
                                     as_no_render_garbage, as_row_count,
                                     do_check_select_options, do_search_select,
                                     do_switch_language)


class FakeUI:
    def __init__(self, rows):
        self._rows = rows

    def table_data(self, page):
        return self._rows


class FakeCtx:
    def __init__(self, rows):
        self.ui = FakeUI(rows)
        self.page = None
        self.vars = {}

    def table_data(self):
        return self.ui.table_data(self.page)


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

    def column_values(self, page, column):
        return []


class FakeConfig:
    list_api = None
    selectors = {}
    search_timeout = 30000


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

    def label_of(self, label):
        return [label]

    def column_of(self, column):
        # 与真实 Context 一致：列名会扩展成多语言候选列表。
        return [column]

    def resolve(self, value):
        return value

    def shot(self, tag):
        return f"screenshots/{tag}.png"


class CheckSelectOptionsTests(unittest.TestCase):
    def test_searchable_select_resolves_query_and_records_selected_value(self):
        ctx = FakeSearchCtx([])
        ctx.resolve = lambda value: "张三" if value == "${manager}" else value
        ctx.ui.search_select = lambda page, label, query: f"{query}（负责人）"

        msg = do_search_select(ctx, label="负责人", query="${manager}")

        self.assertEqual("张三（负责人）", ctx.vars["selected_负责人"])
        self.assertIn("张三", msg)

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
        with self.assertRaisesRegex(AssertionWarning, "无可选项"):
            do_check_select_options(ctx, label="状态")
        self.assertNotIn("selected_状态", ctx.vars)

    def test_undisableable_cascading_select_skips_instead_of_erroring(self):
        # 回归用例：级联选择器（如"城市"依赖"国家"先选）在父级没选时点不开，
        # ui.list_options() 会抛异常（Playwright 点击 disabled 元素超时）。
        # 这种情况应该优雅跳过，不能让整个 step 变成 ERROR 状态。
        class RaisingUI(FakeSelectUI):
            def list_options(self, page, label):
                raise TimeoutError("locator.click: Timeout 5000ms exceeded")
        ctx = FakeSearchCtx([])
        ctx.ui = RaisingUI([])
        with self.assertRaisesRegex(AssertionWarning, "未加载出选项"):
            do_check_select_options(ctx, label="城市")
        self.assertNotIn("selected_城市", ctx.vars)

    def test_each_option_is_checked_against_the_table_immediately(self):
        class VerifyingUI(FakeSelectUI):
            def __init__(self):
                super().__init__(["中国", "韩国"])
                self.selected = None

            def select(self, page, label, option=None, index=None):
                self.selected = option
                return option

            def column_values(self, page, column):
                return ["中国"] if self.selected == "韩国" else ["中国"]

        ctx = FakeSearchCtx([])
        ctx.ui = VerifyingUI()
        with self.assertRaisesRegex(AssertionFailed, "韩国.*不符"):
            do_check_select_options(ctx, label="国家", column="国家")

    def test_empty_filter_result_is_reported_as_no_data_not_failure(self):
        ctx = FakeSearchCtx(["中国", "塞内加尔"])
        msg = do_check_select_options(ctx, label="国家", column="国家")
        self.assertIn("暂无数据：中国、塞内加尔", msg)

    def test_filter_option_and_short_table_alias_are_equivalent(self):
        class AliasUI(FakeSelectUI):
            def __init__(self):
                super().__init__(["即时订单", "预约订单"])
                self.selected = None

            def select(self, page, label, option=None, index=None):
                self.selected = option
                return option

            def column_values(self, page, column):
                return ["即时单"] if self.selected == "即时订单" else ["预约单"]

        ctx = FakeSearchCtx([])
        ctx.ui = AliasUI()
        msg = do_check_select_options(
            ctx,
            label="业务时效类型",
            column="业务时效类型",
        )
        self.assertIn("均正常", msg)

    def test_no_reliable_column_mapping_only_checks_filter_execution(self):
        class NoColumnUI(FakeSelectUI):
            def column_values(self, page, column):
                raise AssertionError("没有显式 column 时不应尝试列值校验")

        ctx = FakeSearchCtx(["是", "否"])
        ctx.ui = NoColumnUI(["是", "否"])
        msg = do_check_select_options(ctx, label="是否代叫")
        self.assertIn("均正常", msg)

    def test_stale_yaml_missing_column_falls_back_to_execution_check(self):
        class MissingColumnUI(FakeSelectUI):
            def column_values(self, page, column):
                raise LookupError("表头没有列 是否代叫")

        ctx = FakeSearchCtx(["是", "否"])
        ctx.ui = MissingColumnUI(["是", "否"])
        msg = do_check_select_options(
            ctx,
            label="是否代叫",
            column="是否代叫",
        )
        self.assertIn("未找到可靠对应列", msg)

    def test_missing_selected_variable_skips_dependent_old_yaml_assertion(self):
        ctx = FakeSearchCtx([])
        ctx.ui.column_values = lambda page, column: ["中国"]
        msg = as_col_all(
            ctx,
            column="国家",
            equals="${selected_国家}",
        )
        self.assertIn("没有可用筛选值", msg)


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


class FakeLangPage:
    def __init__(self):
        self.calls = []

    def locator(self, sel):
        self.calls.append(("locator", sel))
        return self

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.calls.append("click")

    def wait_for_timeout(self, ms):
        pass

    def get_by_text(self, text, exact=False):
        self.calls.append(("get_by_text", text))
        return self


class FakeLangCtx:
    def __init__(self, languages):
        self.page = FakeLangPage()
        self.config = types.SimpleNamespace(languages=languages)
        self.vars = {}


class SwitchLanguageTests(unittest.TestCase):
    def test_missing_config_raises_lookup_error(self):
        ctx = FakeLangCtx({})
        with self.assertRaises(LookupError):
            do_switch_language(ctx, to="en")

    def test_unknown_language_raises_lookup_error(self):
        ctx = FakeLangCtx({"switcher_trigger": ".lang-switch", "options": {"zh": "中文"}})
        with self.assertRaises(LookupError):
            do_switch_language(ctx, to="fr")

    def test_successful_switch_records_current_lang(self):
        ctx = FakeLangCtx({"switcher_trigger": ".lang-switch",
                           "options": {"zh": "中文", "en": "English"}})
        msg = do_switch_language(ctx, to="en")
        self.assertEqual("en", ctx.vars["_current_lang"])
        self.assertIn("English", msg)
        self.assertIn(("get_by_text", "English"), ctx.page.calls)


class I18nLeakTests(unittest.TestCase):
    def test_dotted_key_leak_detected(self):
        ctx = FakeCtx([{"名称": "common.search"}])
        with self.assertRaises(AssertionFailed):
            as_no_i18n_leak(ctx)

    def test_template_placeholder_leak_detected(self):
        ctx = FakeCtx([{"名称": "{{ menu.title }}"}])
        with self.assertRaises(AssertionFailed):
            as_no_i18n_leak(ctx)

    def test_normal_text_passes(self):
        ctx = FakeCtx([{"名称": "加纳"}, {"名称": "Ghana"}])
        self.assertIn("通过", as_no_i18n_leak(ctx))

    def test_empty_table_skipped(self):
        self.assertIn("跳过", as_no_i18n_leak(FakeCtx([])))


class MixedLanguageTests(unittest.TestCase):
    def test_chinese_residue_flagged_when_expecting_english(self):
        ctx = FakeCtx([{"国家名称": "加纳"}])
        with self.assertRaises(AssertionFailed):
            as_no_mixed_language(ctx, expect="en")

    def test_english_only_passes(self):
        ctx = FakeCtx([{"国家名称": "Ghana"}])
        self.assertIn("通过", as_no_mixed_language(ctx, expect="en"))

    def test_zh_expectation_always_skips(self):
        ctx = FakeCtx([{"国家名称": "加纳"}])
        self.assertIn("跳过", as_no_mixed_language(ctx, expect="zh"))

    def test_uses_current_lang_var_when_expect_not_passed(self):
        ctx = FakeCtx([{"国家名称": "加纳"}])
        ctx.vars["_current_lang"] = "en"
        with self.assertRaises(AssertionFailed):
            as_no_mixed_language(ctx)

    def test_no_lang_known_raises_lookup_error(self):
        ctx = FakeCtx([{"国家名称": "Ghana"}])
        with self.assertRaises(LookupError):
            as_no_mixed_language(ctx)


if __name__ == "__main__":
    unittest.main()
