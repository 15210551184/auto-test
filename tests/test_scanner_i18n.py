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

from autotest.engine.scanner import PageScanner, _merge_positional, to_config


class FakePage:
    def on(self, *a, **kw):
        pass


class MergePositionalTests(unittest.TestCase):
    """
    scan_language_variants 的核心风险是"位置错位就乱认翻译"——canonical 和
    translated 数量对不上时必须整批跳过，不能按最短长度硬凑，否则会把
    「国家」的翻译记成「城市」的。
    """

    def test_aligned_lists_merge_by_position(self):
        variants = {}
        _merge_positional(variants, ["国家", "状态"], ["Country", "Status"], "en")
        self.assertEqual({"国家": {"en": "Country"}, "状态": {"en": "Status"}}, variants)

    def test_mismatched_length_skips_entirely(self):
        variants = {}
        _merge_positional(variants, ["国家", "状态"], ["Country"], "en")
        self.assertEqual({}, variants)

    def test_identical_text_not_recorded_as_translation(self):
        # 某个字段两种语言下文案碰巧一样（比如专有名词），不需要记一条无意义的翻译
        variants = {}
        _merge_positional(variants, ["ID", "状态"], ["ID", "Status"], "en")
        self.assertEqual({"状态": {"en": "Status"}}, variants)

    def test_empty_strings_skipped(self):
        variants = {}
        _merge_positional(variants, ["国家", ""], ["Country", "Something"], "en")
        self.assertEqual({"国家": {"en": "Country"}}, variants)

    def test_accumulates_across_multiple_languages(self):
        variants = {}
        _merge_positional(variants, ["国家"], ["Country"], "en")
        _merge_positional(variants, ["国家"], ["Pays"], "fr")
        self.assertEqual({"国家": {"en": "Country", "fr": "Pays"}}, variants)


class ScanLanguageVariantsOrchestrationTests(unittest.TestCase):
    """
    scan_language_variants 本身只负责编排：切语言 -> 重新扫 label/表头/弹窗
    字段 -> 按位置合并。这里把底层扫描方法打桩，只验证编排逻辑本身。
    """

    def setUp(self):
        self.sc = PageScanner(FakePage())

    def test_no_languages_config_returns_empty(self):
        report = {"form_fields": [{"label": "国家"}], "table": {"headers": ["状态"]}}
        label_v, header_v = self.sc.scan_language_variants(None, report)
        self.assertEqual({}, label_v)
        self.assertEqual({}, header_v)

    def test_incomplete_languages_config_returns_empty(self):
        report = {"form_fields": [], "table": {}}
        label_v, header_v = self.sc.scan_language_variants(
            {"switcher_trigger": ".lang"}, report)
        self.assertEqual({}, label_v)

    def test_merges_labels_and_headers_per_language(self):
        report = {
            "form_fields": [{"label": "国家"}, {"label": "状态"}],
            "table": {"headers": ["国家", "状态"]},
            "buttons": {"create": False},
        }
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"},
                    "scan_languages": ["en"]}

        self.sc.switch_language = lambda langs, code: True
        self.sc.scan_form_labels = lambda: ["Country", "Status"]
        self.sc.scan_table_headers = lambda: ["Country", "Status"]

        label_v, header_v = self.sc.scan_language_variants(languages, report)
        self.assertEqual({"国家": {"en": "Country"}, "状态": {"en": "Status"}}, label_v)
        self.assertEqual({"国家": {"en": "Country"}, "状态": {"en": "Status"}}, header_v)

    def test_failed_switch_skips_that_language_only(self):
        report = {
            "form_fields": [{"label": "国家"}],
            "table": {"headers": []},
            "buttons": {"create": False},
        }
        languages = {"switcher_trigger": ".lang",
                    "options": {"en": "English", "fr": "Français"},
                    "scan_languages": ["en", "fr"]}
        attempted = []

        def fake_switch(langs, code):
            attempted.append(code)
            return code == "fr"   # en 切换失败，fr 成功

        self.sc.switch_language = fake_switch
        self.sc.scan_form_labels = lambda: ["Pays"]
        self.sc.scan_table_headers = lambda: []

        label_v, _ = self.sc.scan_language_variants(languages, report)
        self.assertEqual(["en", "fr"], attempted)
        self.assertEqual({"国家": {"fr": "Pays"}}, label_v)

    def test_merges_dialog_labels_when_create_button_present(self):
        report = {
            "form_fields": [],
            "table": {"headers": []},
            "buttons": {"create": True},
            "create_form": {"fields": [{"label": "名称"}, {"label": "备注"}]},
        }
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"},
                    "scan_languages": ["en"]}

        self.sc.switch_language = lambda langs, code: True
        self.sc.scan_form_labels = lambda: []
        self.sc.scan_table_headers = lambda: []
        self.sc.scan_dialog_labels = lambda: ["Name", "Remark"]

        label_v, _ = self.sc.scan_language_variants(languages, report)
        self.assertEqual({"名称": {"en": "Name"}, "备注": {"en": "Remark"}}, label_v)

    def test_no_dialog_scan_when_no_create_button(self):
        report = {
            "form_fields": [],
            "table": {"headers": []},
            "buttons": {"create": False},
            "create_form": {},
        }
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"},
                    "scan_languages": ["en"]}

        self.sc.switch_language = lambda langs, code: True
        self.sc.scan_form_labels = lambda: []
        self.sc.scan_table_headers = lambda: []

        def boom():
            raise AssertionError("不该在没有新增按钮时扫弹窗")
        self.sc.scan_dialog_labels = boom

        self.sc.scan_language_variants(languages, report)   # 不应抛异常

    def test_options_without_scan_languages_returns_empty(self):
        # options 配了运行时可切换的语言，但没勾 scan_languages ——
        # 扫描阶段默认不做任何多语言合并，且不应调用 switch_language。
        report = {
            "form_fields": [{"label": "国家"}],
            "table": {"headers": ["状态"]},
            "buttons": {"create": False},
        }
        languages = {"switcher_trigger": ".lang",
                    "options": {"en": "English", "fr": "Français"}}

        def boom(langs, code):
            raise AssertionError("没配 scan_languages 不该切语言")
        self.sc.switch_language = boom

        label_v, header_v = self.sc.scan_language_variants(languages, report)
        self.assertEqual({}, label_v)
        self.assertEqual({}, header_v)

    def test_only_scan_languages_subset_is_scanned(self):
        # options 有 3 种语言，scan_languages 只勾了其中 1 种——
        # 只应该为这一种切语言，其余两种不该被 switch_language 碰到。
        report = {
            "form_fields": [{"label": "国家"}],
            "table": {"headers": []},
            "buttons": {"create": False},
        }
        languages = {"switcher_trigger": ".lang",
                    "options": {"en": "English", "fr": "Français", "ar": "阿拉伯语"},
                    "scan_languages": ["fr"]}
        attempted = []

        def fake_switch(langs, code):
            attempted.append(code)
            return True

        self.sc.switch_language = fake_switch
        self.sc.scan_form_labels = lambda: ["Pays"]
        self.sc.scan_table_headers = lambda: []

        label_v, _ = self.sc.scan_language_variants(languages, report)
        self.assertEqual(["fr"], attempted)
        self.assertEqual({"国家": {"fr": "Pays"}}, label_v)


class ToConfigPropagatesVariantsTests(unittest.TestCase):
    def test_label_and_header_variants_copied_into_cfg(self):
        report = {
            "url": "http://x.test/web/country",
            "title": "国家管理",
            "form_fields": [],
            "table": {"headers": []},
            "buttons": {},
            "pagination": {},
            "list_api": None,
            "label_variants": {"国家": {"en": "Country"}},
            "header_variants": {"状态": {"en": "Status"}},
        }
        cfg = to_config(report)
        self.assertEqual({"国家": {"en": "Country"}}, cfg["label_variants"])
        self.assertEqual({"状态": {"en": "Status"}}, cfg["header_variants"])

    def test_absent_variants_not_added_to_cfg(self):
        report = {
            "url": "http://x.test/web/country",
            "title": "国家管理",
            "form_fields": [],
            "table": {"headers": []},
            "buttons": {},
            "pagination": {},
            "list_api": None,
        }
        cfg = to_config(report)
        self.assertNotIn("label_variants", cfg)
        self.assertNotIn("header_variants", cfg)


class FakeMenuLocator:
    """极简 locator 桩：click 记调用，能配置成普通点击/force 点击各自失败。"""

    def __init__(self, raise_on_click=False, raise_on_force_click=False):
        self._raise_on_click = raise_on_click
        self._raise_on_force_click = raise_on_force_click

    @property
    def first(self):
        return self

    def click(self, timeout=None, force=False):
        if force:
            if self._raise_on_force_click:
                raise TimeoutError("locator.click: Timeout exceeded (force)")
            return
        if self._raise_on_click:
            raise TimeoutError("locator.click: Timeout exceeded")


class FakeMenuPage:
    """
    evaluate() 按调用顺序依次返回 snapshots 里的值——第一次是点击前的
    "当前可见文本"快照，第二次是点击后的快照，探测逻辑对这两次快照做差集。
    """

    def __init__(self, snapshots, raise_on_trigger_click=False, raise_on_force_click=False):
        self._snapshots = list(snapshots)
        self._raise_on_trigger_click = raise_on_trigger_click
        self._raise_on_force_click = raise_on_force_click
        self.keyboard = types.SimpleNamespace(press=lambda k: None)
        self.url = "http://x.test/web/index"

    def on(self, *a, **kw):
        pass

    def locator(self, sel):
        return FakeMenuLocator(raise_on_click=self._raise_on_trigger_click,
                               raise_on_force_click=self._raise_on_force_click)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return self._snapshots.pop(0)


class ProbeLanguageOptionsTests(unittest.TestCase):
    """
    探测语言选项：点开切换控件，把点击前后"新冒出来的可见文本"读出来，
    省掉手动 F12 照抄的步骤——抄错一个字 switch_language 就永远匹配不上。
    差集而非固定选择器，是因为语言切换菜单没有统一的框架/DOM 约定。
    """

    def test_reads_new_texts_after_click(self):
        before = ["搜索", "重置", "新增"]
        after = ["搜索", "重置", "新增", "中文", "English"]
        page = FakeMenuPage([before, after])
        sc = PageScanner(page)
        texts = sc.probe_language_options(".lang-switch")
        self.assertEqual(["中文", "English"], texts)

    def test_trigger_click_failure_raises_lookup_error(self):
        # 选择器写错、普通点击和 force 点击都点不到——这跟"点开了但没冒出
        # 新文字"是两种不同的失败原因，前者得报出来让人去改选择器，
        # 不能也悄悄返回空列表。
        page = FakeMenuPage([[]], raise_on_trigger_click=True, raise_on_force_click=True)
        sc = PageScanner(page)
        with self.assertRaises(LookupError):
            sc.probe_language_options(".lang-switch")

    def test_force_click_fallback_succeeds_when_normal_click_fails(self):
        # el-dropdown 这类自定义触发器常年过不了 Playwright 的可操作性检查，
        # 但其实是能点的——普通点击失败后应该自动退回 force 点击再试一次。
        before = ["搜索"]
        after = ["搜索", "中文", "English"]
        page = FakeMenuPage([before, after], raise_on_trigger_click=True,
                            raise_on_force_click=False)
        sc = PageScanner(page)
        texts = sc.probe_language_options(".lang-select")
        self.assertEqual(["中文", "English"], texts)

    def test_nothing_new_appeared_returns_empty(self):
        same = ["搜索", "重置"]
        page = FakeMenuPage([same, same])   # 点开了，但页面上没有新文本出现
        sc = PageScanner(page)
        self.assertEqual([], sc.probe_language_options(".lang-switch"))

    def test_dedups_repeated_new_text(self):
        before = []
        after = ["中文", "中文", "English"]
        page = FakeMenuPage([before, after])
        sc = PageScanner(page)
        self.assertEqual(["中文", "English"], sc.probe_language_options(".lang-switch"))

    def test_truncates_to_fifteen(self):
        before = []
        after = [f"L{i}" for i in range(20)]
        page = FakeMenuPage([before, after])
        sc = PageScanner(page)
        self.assertEqual(15, len(sc.probe_language_options(".lang-switch")))


class GuessListApiTests(unittest.TestCase):
    """
    guess_list_api 原来直接挑 api_calls 里"最后一个"命中 list/page/query/
    search/find 的 URL。真实事故：某系统全局通知公告小组件轮询
    /system/notice/listTop，扫描一个跟通知毫无关系的页面（国家管理）时，
    这条轮询请求排到了 api_calls 末尾，把真正的列表接口顶掉——生成的
    list_api 配置执行时永远等不到匹配响应。
    """

    def _sc_with_calls(self, url, calls):
        page = FakeMenuPage([[], []])
        page.url = url
        sc = PageScanner(page)
        sc.api_calls = list(calls)
        return sc

    def test_prefers_call_matching_page_url_business_word(self):
        sc = self._sc_with_calls(
            "http://x.test/web/country/list",
            ["http://x.test/api/vaWeb/system/country/list",
             "http://x.test/api/vaWeb/system/notice/listTop"])
        self.assertEqual("/api/vaWeb/system/country/list", sc.guess_list_api())

    def test_polling_widget_after_real_call_no_longer_wins(self):
        # 复现真实顺序：业务接口先打，通知轮询排在后面（甚至打好几次）
        sc = self._sc_with_calls(
            "http://x.test/web/country/list",
            ["http://x.test/api/vaWeb/system/country/list",
             "http://x.test/api/vaWeb/system/notice/listTop",
             "http://x.test/api/vaWeb/system/notice/listTop"])
        self.assertEqual("/api/vaWeb/system/country/list", sc.guess_list_api())

    def test_no_business_word_overlap_falls_back_to_last_match(self):
        # 页面地址提取不出业务词（比如纯拼音/ID），退回原来"选最后一个"
        sc = self._sc_with_calls(
            "http://x.test/web/1234",
            ["http://x.test/api/foo/list", "http://x.test/api/bar/list"])
        self.assertEqual("/api/bar/list", sc.guess_list_api())

    def test_no_list_like_candidate_falls_back_to_last_call(self):
        sc = self._sc_with_calls(
            "http://x.test/web/country/list",
            ["http://x.test/api/vaWeb/system/country/detail"])
        self.assertEqual("/api/vaWeb/system/country/detail", sc.guess_list_api())

    def test_no_calls_returns_none(self):
        sc = self._sc_with_calls("http://x.test/web/country/list", [])
        self.assertIsNone(sc.guess_list_api())


if __name__ == "__main__":
    unittest.main()
