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


class CrudConfigGenerationTests(unittest.TestCase):
    """
    有新增弹窗结构（create_form）时应该生成完整的新增/修改/详情/删除闭环，
    而不是老版本那种需要人工填字段名的 skip 骨架。
    """

    def _report(self, buttons, create_fields):
        return {
            "url": "http://x.test/web/franchise",
            "title": "加盟商管理",
            "form_fields": [],
            "table": {"headers": ["加盟商名称", "负责人", "状态"], "row_count": 3,
                      "column_types": {}, "sample_row": {}},
            "buttons": buttons,
            "pagination": {},
            "list_api": None,
            "create_form": {"title": "新增加盟商", "fields": create_fields},
        }

    def _fields(self):
        return [
            {"label": "加盟商名称", "type": "text", "required": True, "maxlength": 50},
            {"label": "负责人", "type": "text", "required": True, "maxlength": 50},
            {"label": "状态", "type": "select", "required": False, "options": ["生效", "失效"]},
        ]

    def test_full_loop_generated_when_all_row_actions_available(self):
        report = self._report(
            {"create": True, "edit": True, "delete": True, "detail": True},
            self._fields())
        cfg = scanner.to_config(report)
        names = [c["name"] for c in cfg["cases"]]
        self.assertIn("新增-必填校验", names)
        self.assertIn("新增-修改-详情-删除完整闭环", names)

        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        actions = [a for s in loop["steps"] for a in s.keys()]
        self.assertEqual(
            ["create_and_verify", "assert_form_prefilled", "edit_and_verify",
             "assert_detail_matches", "delete_and_verify"],
            actions)

    def test_identity_prefers_field_matching_table_header(self):
        # "加盟商名称"能对上表头，"负责人"也能——但"加盟商名称"排在前面，应该优先选它
        report = self._report({"create": True}, self._fields())
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        params = loop["steps"][0]["create_and_verify"]
        self.assertEqual("加盟商名称", params["identity"])
        self.assertEqual("加盟商名称", params["identity_column"])

    def test_edit_field_excludes_identity(self):
        report = self._report({"create": True, "edit": True}, self._fields())
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        edit_step = next(s["edit_and_verify"] for s in loop["steps"] if "edit_and_verify" in s)
        self.assertNotIn("加盟商名称", edit_step["fields"])   # 不能改用来定位记录的字段
        self.assertIn("负责人", edit_step["fields"])

    def test_no_edit_button_skips_edit_steps_entirely(self):
        report = self._report({"create": True, "delete": True}, self._fields())
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        actions = [a for s in loop["steps"] for a in s.keys()]
        self.assertNotIn("assert_form_prefilled", actions)
        self.assertNotIn("edit_and_verify", actions)
        self.assertIn("delete_and_verify", actions)   # 其他按钮不受影响

    def test_required_fields_all_listed_in_validation_case(self):
        report = self._report({"create": True}, self._fields())
        cfg = scanner.to_config(report)
        case = next(c for c in cfg["cases"] if c["name"] == "新增-必填校验")
        expect = case["steps"][1]["assert_form_errors"]["expect"]
        self.assertEqual(["加盟商名称", "负责人"], expect)   # "状态"不是必填，不该出现

    def test_falls_back_to_skip_skeleton_without_create_form(self):
        # 扫不出弹窗结构（比如非 Element UI 页面）时不能假装生成了闭环
        report = self._report({"create": True}, [])
        report.pop("create_form")
        cfg = scanner.to_config(report)
        names = [c["name"] for c in cfg["cases"]]
        self.assertIn("新增数据（需补充字段）", names)
        skel = next(c for c in cfg["cases"] if c["name"] == "新增数据（需补充字段）")
        self.assertTrue(skel["skip"])

    def test_status_toggle_step_added_when_button_and_column_present(self):
        report = self._report(
            {"create": True, "delete": True, "status_toggle": True},
            self._fields())
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        actions = [a for s in loop["steps"] for a in s.keys()]
        self.assertIn("toggle_status_and_verify", actions)
        # 必须排在 delete_and_verify 前面：清理之前先验证状态流转
        self.assertLess(actions.index("toggle_status_and_verify"),
                        actions.index("delete_and_verify"))

    def test_status_toggle_skipped_without_status_column(self):
        report = self._report({"create": True, "status_toggle": True}, self._fields())
        report["table"]["headers"] = ["加盟商名称", "负责人"]   # 没有"状态"列
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        actions = [a for s in loop["steps"] for a in s.keys()]
        self.assertNotIn("toggle_status_and_verify", actions)

    def test_status_toggle_skipped_without_button(self):
        report = self._report({"create": True}, self._fields())   # 没有 status_toggle 按钮
        cfg = scanner.to_config(report)
        loop = next(c for c in cfg["cases"] if c["name"] == "新增-修改-详情-删除完整闭环")
        actions = [a for s in loop["steps"] for a in s.keys()]
        self.assertNotIn("toggle_status_and_verify", actions)

    def test_no_create_button_generates_nothing_crud(self):
        report = self._report({}, [])
        report.pop("create_form")
        cfg = scanner.to_config(report)
        crud_cases = [c for c in cfg["cases"] if "crud" in c.get("tags", [])]
        self.assertEqual([], crud_cases)


class MultiLanguageConfigTests(unittest.TestCase):
    """languages 配了 switcher_trigger/options 才生成多语言检查用例，零配置不生成。"""

    def setUp(self):
        self.report = {
            "url": "http://x.test/web/country",
            "title": "国家管理",
            "form_fields": [],
            "table": {"headers": ["国家名称"], "row_count": 3,
                      "column_types": {}, "sample_row": {}},
            "buttons": {},
            "pagination": {},
            "list_api": None,
        }

    def test_no_languages_config_generates_nothing(self):
        cfg = scanner.to_config(self.report)
        names = [c["name"] for c in cfg["cases"]]
        self.assertNotIn("多语言检查", names)
        self.assertNotIn("languages", cfg)

    def test_languages_config_generates_switch_and_check_per_language(self):
        languages = {"switcher_trigger": ".lang-switch",
                     "options": {"zh": "中文", "en": "English"}}
        cfg = scanner.to_config(self.report, languages=languages)
        case = next(c for c in cfg["cases"] if c["name"] == "多语言检查")
        self.assertEqual(["i18n"], case["tags"])
        actions = [a for s in case["steps"] for a in s.keys()]
        # 每种语言一轮：切换 -> 搜索 -> 两个检查
        self.assertEqual(actions.count("switch_language"), 2)
        self.assertEqual(actions.count("assert_no_i18n_leak"), 2)
        self.assertEqual(actions.count("assert_no_mixed_language"), 2)
        self.assertEqual(languages, cfg["languages"])

    def test_incomplete_languages_config_is_ignored(self):
        # 只配了 trigger 没配 options（反之亦然）——信息不全，宁可不生成
        cfg = scanner.to_config(self.report, languages={"switcher_trigger": ".lang"})
        names = [c["name"] for c in cfg["cases"]]
        self.assertNotIn("多语言检查", names)


if __name__ == "__main__":
    unittest.main()
