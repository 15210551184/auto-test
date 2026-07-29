import json
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

if "yaml" not in sys.modules:
    # 和其他测试文件用同一份桩，避免哪个测试先跑就把 sys.modules['yaml']
    # 钉成一个不完整的桩，导致后跑的测试文件缺 yaml.dump 报错。
    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda text: json.loads(text)
    yaml.dump = lambda data, **kwargs: json.dumps(data, ensure_ascii=False)
    sys.modules["yaml"] = yaml

from autotest.engine.explain import explain_config


def _cfg(cases):
    return {"name": "测试页", "url": "http://x.test", "cases": cases}


class ExplainConfigTests(unittest.TestCase):
    def test_known_action_produces_readable_sentence(self):
        cfg = _cfg([{"name": "搜索", "steps": [
            {"fill": {"label": "国家名称", "value": "加纳"}},
            {"search": None},
        ]}])
        out = explain_config(cfg)
        steps = out["cases"][0]["steps"]
        self.assertIn("国家名称", steps[0])
        self.assertIn("加纳", steps[0])
        self.assertIn("搜索", steps[1])

    def test_unknown_action_falls_back_without_crashing(self):
        cfg = _cfg([{"name": "怪用例", "steps": [{"some_future_action": {"x": 1}}]}])
        out = explain_config(cfg)
        self.assertIn("some_future_action", out["cases"][0]["steps"][0])

    def test_selected_placeholder_is_humanized_not_raw(self):
        cfg = _cfg([{"name": "筛选", "steps": [
            {"assert_column_all": {"column": "状态", "equals": "${selected_状态}"}},
        ]}])
        line = explain_config(cfg)["cases"][0]["steps"][0]
        self.assertNotIn("${", line)
        self.assertIn("状态", line)

    def test_button_alias_translated_to_friendly_name(self):
        cfg = _cfg([{"name": "重置", "steps": [{"click": "reset_btn"}]}])
        line = explain_config(cfg)["cases"][0]["steps"][0]
        self.assertIn("重置", line)
        self.assertNotIn("reset_btn", line)

    def test_skip_flag_is_surfaced(self):
        cfg = _cfg([{"name": "新增", "skip": True, "steps": [{"click": "create_btn"}]}])
        self.assertTrue(explain_config(cfg)["cases"][0]["skip"])

    def test_malformed_step_does_not_break_whole_case(self):
        # step 不是单键字典（Step.from_raw 会报错）时，兜底给出提示而不是整体抛异常
        cfg = _cfg([{"name": "坏用例", "steps": [{"a": 1, "b": 2}]}])
        out = explain_config(cfg)
        self.assertEqual(1, len(out["cases"][0]["steps"]))
        self.assertIn("解析失败", out["cases"][0]["steps"][0])

    def test_random_placeholder_humanized(self):
        cfg = _cfg([{"name": "填表", "steps": [
            {"fill": {"label": "邮箱", "value": "auto_${random}@test.com"}},
        ]}])
        line = explain_config(cfg)["cases"][0]["steps"][0]
        self.assertNotIn("${", line)
        self.assertIn("随机", line)


if __name__ == "__main__":
    unittest.main()
