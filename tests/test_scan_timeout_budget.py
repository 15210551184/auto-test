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

from autotest.engine.batch import LANG_SCAN_BUDGET_SEC, SCAN_TIMEOUT_SEC, _scan_timeout_for


class ScanTimeoutBudgetTests(unittest.TestCase):
    """
    scan_language_variants() 会为项目配的每种语言切一次、重新扫一遍表单/
    表头/弹窗字段，纯 CRUD 页面配了好几种语言也会因为这部分多花不少时间。
    超时预算得按语言数量成正比增加，不能所有页面共用同一个只按"不带
    多语言"估出来的固定上限，不然配了语言的项目会大批量假性超时。
    """

    def test_no_languages_config_uses_base_timeout(self):
        self.assertEqual(SCAN_TIMEOUT_SEC, _scan_timeout_for(None))
        self.assertEqual(SCAN_TIMEOUT_SEC, _scan_timeout_for({}))

    def test_languages_without_options_uses_base_timeout(self):
        self.assertEqual(SCAN_TIMEOUT_SEC, _scan_timeout_for({"switcher_trigger": ".lang"}))

    def test_budget_scales_with_language_count(self):
        languages = {"switcher_trigger": ".lang",
                    "options": {"zh": "中文", "en": "English", "fr": "Français", "ar": "阿拉伯语"}}
        expected = SCAN_TIMEOUT_SEC + 4 * LANG_SCAN_BUDGET_SEC
        self.assertEqual(expected, _scan_timeout_for(languages))

    def test_custom_base_timeout_respected(self):
        languages = {"options": {"en": "English"}}
        self.assertEqual(200 + LANG_SCAN_BUDGET_SEC, _scan_timeout_for(languages, base=200))


if __name__ == "__main__":
    unittest.main()
