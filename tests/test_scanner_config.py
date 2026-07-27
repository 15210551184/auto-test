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


class CascadingSelectConfigTests(unittest.TestCase):
    """
    级联下拉（如"城市"依赖"国家"先选）扫描阶段探测到 depends_on 后，
    生成的用例必须先选父级再操作子级，否则执行时会点在一个 disabled
    元素上，和扫描阶段踩的坑一样。
    """

    def setUp(self):
        self.report = {
            "url": "http://x.test/web/franchise",
            "title": "加盟商管理",
            "form_fields": [
                {"label": "国家", "type": "select", "options": ["全部", "中国", "韩国"]},
                {"label": "城市", "type": "select", "options": ["北京市", "上海市"],
                 "depends_on": {"label": "国家", "option": "中国"}},
            ],
            "table": {"headers": ["城市", "加盟商名称"], "row_count": 3,
                      "column_types": {}, "sample_row": {}},
            "buttons": {"search": True, "reset": False, "export": False, "create": False},
            "pagination": {},
            "list_api": None,
        }

    def test_dependent_select_case_selects_parent_first(self):
        cfg = scanner.to_config(self.report)
        case = next(c for c in cfg["cases"] if c["name"].startswith("筛选-城市"))
        first_step = case["steps"][0]
        self.assertEqual({"select": {"label": "国家", "option": "中国"}}, first_step)
        actions = [a for s in case["steps"] for a in s.keys()]
        self.assertIn("check_select_options", actions)

    def test_dependent_select_case_name_mentions_parent(self):
        cfg = scanner.to_config(self.report)
        case = next(c for c in cfg["cases"] if c["name"].startswith("筛选-城市"))
        self.assertIn("国家", case["name"])

    def test_independent_select_case_has_no_prerequisite_step(self):
        cfg = scanner.to_config(self.report)
        case = next(c for c in cfg["cases"] if c["name"] == "筛选-国家")
        first_step = case["steps"][0]
        self.assertIn("check_select_options", first_step)


if __name__ == "__main__":
    unittest.main()
