import json
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

if "yaml" not in sys.modules:
    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda text: json.loads(text)
    yaml.dump = lambda data, **kwargs: json.dumps(data, ensure_ascii=False)
    sys.modules["yaml"] = yaml

from autotest.engine import scanner


def _all_actions(cfg):
    return [a for c in cfg["cases"] for step in c["steps"] for a in step.keys()]


class ToConfigPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "url": "http://x.test/web/country",
            "title": "国家管理",
            "form_fields": [
                {"label": "国家名称", "type": "text", "placeholder": "请输入国家名称"},
                {"label": "状态", "type": "select", "options": ["全部", "生效", "失效"]},
            ],
            "table": {
                "headers": ["序号", "国家编码", "国家名称", "货币符号", "创建时间", "状态"],
                "row_count": 3,
                "column_types": {"创建时间": "date", "货币符号": "money"},
                "sample_row": {"国家名称": "加纳", "货币符号": "GH₵"},
            },
            "buttons": {"search": True, "reset": True, "export": True, "create": True},
            "pagination": {"has_pagination": True, "total": 30},
            "list_api": "/api/list",
        }

    def test_smoke_case_includes_render_check(self):
        cfg = scanner.to_config(self.report)
        smoke = next(c for c in cfg["cases"] if c["name"] == "列表默认加载")
        actions = [a for s in smoke["steps"] for a in s.keys()]
        self.assertIn("assert_no_render_garbage", actions)

    def test_button_patrol_case_generated(self):
        cfg = scanner.to_config(self.report)
        self.assertIn("check_buttons", _all_actions(cfg))

    def test_select_filter_uses_option_sweep(self):
        cfg = scanner.to_config(self.report)
        sel_case = next(c for c in cfg["cases"] if c["name"] == "筛选-状态")
        actions = [a for s in sel_case["steps"] for a in s.keys()]
        self.assertIn("check_select_options", actions)
        self.assertIn("assert_column_all", actions)  # 状态 label 精确匹配到列

    def test_no_unexecutable_todo_placeholders(self):
        # 第一期目标：生成的（未 skip 的）用例不含 TODO 占位
        cfg = scanner.to_config(self.report)
        blob = json.dumps(cfg, ensure_ascii=False)
        active = [c for c in cfg["cases"] if not c.get("skip")]
        active_blob = json.dumps(active, ensure_ascii=False)
        self.assertNotIn("TODO", active_blob)


if __name__ == "__main__":
    unittest.main()
