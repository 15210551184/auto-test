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

from autotest.engine.i18n_terms import words
from autotest.engine.adapters.element_ui import ElementUIAdapter


class WordsHelperTests(unittest.TestCase):
    def test_single_category(self):
        self.assertIn("删除", words("delete"))
        self.assertIn("Delete", words("delete"))

    def test_multiple_categories_merge_without_duplicates(self):
        w = words("disable", "enable")
        self.assertIn("停用", w)
        self.assertIn("启用", w)
        self.assertEqual(len(w), len(set(w)))

    def test_unknown_category_returns_empty(self):
        self.assertEqual([], words("not_a_real_category"))


class DestructiveKeywordsCoverAllLanguagesTests(unittest.TestCase):
    """
    回归用例：check_buttons 巡检靠这份名单判断"这个按钮别真点下去"。
    之前只有中文，界面切成英文后 'Delete' 认不出来，巡检会真的点下去——
    这是安全问题。锁死中英文都要能命中。
    """

    def test_chinese_delete_recognized(self):
        self.assertTrue(any("删除" in w for w in ElementUIAdapter.DESTRUCTIVE))

    def test_english_delete_recognized(self):
        self.assertIn("Delete", ElementUIAdapter.DESTRUCTIVE)

    def test_english_disable_recognized(self):
        self.assertIn("Disable", ElementUIAdapter.DESTRUCTIVE)

    def test_no_duplicate_entries(self):
        self.assertEqual(len(ElementUIAdapter.DESTRUCTIVE), len(set(ElementUIAdapter.DESTRUCTIVE)))


if __name__ == "__main__":
    unittest.main()
