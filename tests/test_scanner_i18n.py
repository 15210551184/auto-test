import sys
import types
import unittest

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
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
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"}}

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
                    "options": {"en": "English", "fr": "Français"}}
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
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"}}

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
        languages = {"switcher_trigger": ".lang", "options": {"en": "English"}}

        self.sc.switch_language = lambda langs, code: True
        self.sc.scan_form_labels = lambda: []
        self.sc.scan_table_headers = lambda: []

        def boom():
            raise AssertionError("不该在没有新增按钮时扫弹窗")
        self.sc.scan_dialog_labels = boom

        self.sc.scan_language_variants(languages, report)   # 不应抛异常


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
    """极简 locator 桩：click 记调用，all_inner_texts 按选择器返回预设文案。"""

    def __init__(self, texts_by_sel, sel="", raise_on_click=False):
        self._texts_by_sel = texts_by_sel
        self._sel = sel
        self._raise_on_click = raise_on_click

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self._raise_on_click:
            raise TimeoutError("locator.click: Timeout exceeded")

    def all_inner_texts(self):
        return self._texts_by_sel.get(self._sel, [])


class FakeMenuPage:
    def __init__(self, texts_by_sel, raise_on_trigger_click=False):
        self._texts_by_sel = texts_by_sel
        self._raise_on_trigger_click = raise_on_trigger_click
        self.keyboard = types.SimpleNamespace(press=lambda k: None)

    def on(self, *a, **kw):
        pass

    def locator(self, sel):
        raise_click = self._raise_on_trigger_click and sel == ".lang-switch"
        return FakeMenuLocator(self._texts_by_sel, sel, raise_on_click=raise_click)

    def wait_for_timeout(self, ms):
        pass


class ProbeLanguageOptionsTests(unittest.TestCase):
    """
    探测语言选项：点开切换控件，把弹出菜单里的文案读出来，省掉手动 F12
    照抄的步骤——抄错一个字 switch_language 就永远匹配不上。
    """

    def test_reads_and_dedups_menu_texts(self):
        menu_sel = (".el-dropdown-menu:visible .el-dropdown-menu__item, "
                   ".el-dropdown-menu:visible li, "
                   ".el-select-dropdown:visible .el-select-dropdown__item, "
                   ".el-menu--popup:visible .el-menu-item, "
                   ".el-popper:visible li, .el-popper:visible a")
        page = FakeMenuPage({menu_sel: [" 中文 ", "English", "中文", ""]})
        sc = PageScanner(page)
        texts = sc.probe_language_options(".lang-switch")
        self.assertEqual(["中文", "English"], texts)

    def test_trigger_not_found_returns_empty(self):
        page = FakeMenuPage({}, raise_on_trigger_click=True)
        sc = PageScanner(page)
        self.assertEqual([], sc.probe_language_options(".lang-switch"))

    def test_no_matching_menu_structure_returns_empty(self):
        page = FakeMenuPage({})   # 点开了，但没有任何选择器命中
        sc = PageScanner(page)
        self.assertEqual([], sc.probe_language_options(".lang-switch"))

    def test_truncates_to_ten(self):
        menu_sel = (".el-dropdown-menu:visible .el-dropdown-menu__item, "
                   ".el-dropdown-menu:visible li, "
                   ".el-select-dropdown:visible .el-select-dropdown__item, "
                   ".el-menu--popup:visible .el-menu-item, "
                   ".el-popper:visible li, .el-popper:visible a")
        many = [f"L{i}" for i in range(15)]
        page = FakeMenuPage({menu_sel: many})
        sc = PageScanner(page)
        self.assertEqual(10, len(sc.probe_language_options(".lang-switch")))


if __name__ == "__main__":
    unittest.main()
