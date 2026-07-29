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

from autotest.engine.models import Case
from autotest.engine.runner import filter_cases_by_tags


def _case(name, tags):
    return Case(name=name, steps=[], tags=tags)


class FilterCasesByTagsTests(unittest.TestCase):
    def setUp(self):
        self.cases = [
            _case("列表默认加载", ["smoke"]),
            _case("按钮巡检", ["health"]),
            _case("搜索-国家名称", ["search"]),
            _case("多语言检查", ["i18n"]),
            _case("新增-修改-详情-删除完整闭环", ["crud"]),
        ]

    def test_no_filters_returns_everything(self):
        self.assertEqual(self.cases, filter_cases_by_tags(self.cases))

    def test_only_tags_keeps_matching(self):
        out = filter_cases_by_tags(self.cases, only_tags=["smoke", "health"])
        self.assertEqual(["列表默认加载", "按钮巡检"], [c.name for c in out])

    def test_exclude_tags_drops_matching_keeps_rest(self):
        # 这是用户要的场景：不想跑多语言检查，其余照常全跑
        out = filter_cases_by_tags(self.cases, exclude_tags=["i18n"])
        names = [c.name for c in out]
        self.assertNotIn("多语言检查", names)
        self.assertEqual(4, len(out))

    def test_only_and_exclude_combined(self):
        out = filter_cases_by_tags(self.cases, only_tags=["smoke", "health", "i18n"],
                                   exclude_tags=["i18n"])
        self.assertEqual(["列表默认加载", "按钮巡检"], [c.name for c in out])

    def test_new_future_tags_survive_exclude_without_extra_config(self):
        # exclude 存在的意义：以后加新标签不用记得去改 include 名单
        cases = self.cases + [_case("以后加的新用例", ["brand_new_tag"])]
        out = filter_cases_by_tags(cases, exclude_tags=["i18n"])
        self.assertIn("以后加的新用例", [c.name for c in out])


if __name__ == "__main__":
    unittest.main()
