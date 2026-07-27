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

from autotest.engine.actions import (AssertionFailed, as_detail_matches,
                                     as_form_errors, as_form_prefilled,
                                     do_create_and_verify, do_delete_and_verify,
                                     do_edit_and_verify)


class FakeLocator:
    def __init__(self, on_click=None):
        self._on_click = on_click

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self._on_click:
            self._on_click()

    def wait_for(self, state=None, timeout=None):
        pass


class FakePage:
    def __init__(self):
        self.calls = []

    def locator(self, sel):
        self.calls.append(sel)
        return FakeLocator()

    def wait_for_timeout(self, ms):
        pass


class FakeUI:
    """
    模拟 CRUD 闭环需要的适配器方法。table 是"当前列表"的可变引用，
    set_field_value / row_action 只记录调用，不真的操作 DOM。
    """

    def __init__(self, table, dialog_values=None, message="操作成功"):
        self.table = table
        self.dialog_values = dialog_values or {}
        self.message = message
        self.set_calls = []
        self.filled = {}     # label -> 最近一次填的值，测试用它拼出"提交后的行"
        self.row_actions = []
        self.confirmed = None
        self._dialog_visible = False

    def dialog(self, page):
        return FakeLocator()

    def fill(self, page, label, value):
        self.filled[label] = value

    def set_field_value(self, page, field, value):
        self.set_calls.append((field.get("type"), field["label"], value))
        self.filled[field["label"]] = value

    def table_data(self, page):
        return self.table

    def find_row_by(self, page, column, value):
        hits = [i for i, r in enumerate(self.table) if value in str(r.get(column, ""))]
        if not hits:
            raise LookupError(f"未找到包含 '{value}' 的行")
        if len(hits) > 1:
            raise LookupError("匹配上不止一行")
        return hits[0]

    def row_action(self, page, row, action):
        self.row_actions.append((row, action))

    def message_text(self, page, timeout=5000):
        return self.message

    def dialog_field_values(self, page):
        return self.dialog_values

    def detail_values(self, page):
        return self.dialog_values

    def dialog_visible(self, page):
        return self._dialog_visible

    def close_dialog(self, page):
        self._dialog_visible = False

    def confirm_dialog(self, page, ok=True):
        self.confirmed = ok

    def form_error_labels(self, page):
        return self._errors

    def form_error_texts(self, page):
        return []


class FakeConfig:
    list_api = None
    selectors = {"create_btn": "button:create", "submit_btn": "button:submit",
                "search_btn": "button:search"}


class FakeCrudCtx:
    def __init__(self, table, dialog_values=None, message="操作成功"):
        self.page = FakePage()
        self.ui = FakeUI(table, dialog_values, message)
        self.vars = {}
        self.config = FakeConfig()
        self.console_errors = []
        self.failed_requests = []

    def selector(self, key):
        return self.config.selectors.get(key, key)

    def resolve(self, v):
        return v


FIELDS = [
    {"label": "国家", "type": "select", "required": True, "options": ["请选择", "中国"]},
    {"label": "加盟商名称", "type": "text", "required": True, "maxlength": 50},
    {"label": "负责人", "type": "text", "required": True, "maxlength": 50},
]


class CreateAndVerifyTests(unittest.TestCase):
    """
    do_create_and_verify 内部会调用模块级的 do_search；这里打桩成"用已经
    填过的值（ctx.ui.filled，由 FakeUI.fill/set_field_value 记录）拼出
    提交后列表应该显示的那一行"，模拟真实系统提交成功后能搜到这条记录。
    """

    def _patch_search(self, row_transform=None):
        import autotest.engine.actions as A
        self._orig_search = A.do_search

        def stub_search(_ctx):
            row = dict(_ctx.ui.filled)
            if row_transform:
                row_transform(row)
            _ctx.ui.table = [row]
            return "执行搜索"

        A.do_search = stub_search

    def _unpatch_search(self):
        import autotest.engine.actions as A
        A.do_search = self._orig_search

    def test_success_path_stores_identity_and_schema_for_followups(self):
        ctx = FakeCrudCtx(table=[])
        self._patch_search()
        try:
            msg = do_create_and_verify(ctx, fields=FIELDS, identity="加盟商名称")
        finally:
            self._unpatch_search()

        self.assertIn("通过", msg)
        self.assertIn("created_identity", ctx.vars)
        self.assertEqual("加盟商名称", ctx.vars["created_identity_column"])
        self.assertEqual(FIELDS, ctx.vars["created_field_schema"])
        # identity 字段的值必须真的被填过，且是自动化前缀（除非命中语义生成器）
        self.assertIn(ctx.vars["created_identity"], ctx.vars["created_fields"].values())

    def test_missing_column_in_list_is_not_counted_as_mismatch(self):
        # 表格里没有"负责人"这一列（比如列表没展示这个字段），不该被判成不一致
        ctx = FakeCrudCtx(table=[])
        self._patch_search(row_transform=lambda row: row.pop("负责人", None))
        try:
            msg = do_create_and_verify(ctx, fields=FIELDS, identity="加盟商名称")
        finally:
            self._unpatch_search()
        self.assertIn("通过", msg)


class FormErrorsTests(unittest.TestCase):
    def test_expected_fields_all_reported_passes(self):
        ctx = FakeCrudCtx(table=[])
        ctx.ui._errors = ["国家", "加盟商名称"]
        msg = as_form_errors(ctx, expect=["国家", "加盟商名称"])
        self.assertIn("通过", msg)

    def test_missing_expected_error_fails(self):
        ctx = FakeCrudCtx(table=[])
        ctx.ui._errors = ["国家"]   # "加盟商名称"该报错但没报
        with self.assertRaises(AssertionFailed):
            as_form_errors(ctx, expect=["国家", "加盟商名称"])

    def test_no_errors_at_all_without_expect_fails(self):
        ctx = FakeCrudCtx(table=[])
        ctx.ui._errors = []
        with self.assertRaises(AssertionFailed):
            as_form_errors(ctx)


class DeleteSafetyTests(unittest.TestCase):
    """铁律：只删自己创建的数据。这几条是整个 CRUD 闭环里风险最高的一段。"""

    def test_refuses_to_delete_without_auto_prefix(self):
        ctx = FakeCrudCtx(table=[{"加盟商名称": "北京出行二队"}])
        ctx.vars["created_identity"] = "北京出行二队"   # 没有 auto_ 前缀，像真实数据
        ctx.vars["created_identity_column"] = "加盟商名称"
        with self.assertRaises(AssertionFailed):
            do_delete_and_verify(ctx)
        self.assertEqual([], ctx.ui.row_actions)   # 必须完全没碰过删除操作

    def test_deletes_auto_prefixed_record_and_confirms_gone(self):
        table = [{"加盟商名称": "auto_x9f2"}]
        ctx = FakeCrudCtx(table=table)
        ctx.vars["created_identity"] = "auto_x9f2"
        ctx.vars["created_identity_column"] = "加盟商名称"

        def fake_row_action(page, row, action):
            table.clear()   # 模拟删除后列表清空
            ctx.ui.row_actions.append((row, action))

        ctx.ui.row_action = fake_row_action
        msg = do_delete_and_verify(ctx)
        self.assertIn("清理完成", msg)
        self.assertEqual(1, len(ctx.ui.row_actions))

    def test_no_pending_record_is_a_noop_not_a_failure(self):
        ctx = FakeCrudCtx(table=[])
        msg = do_delete_and_verify(ctx)   # 没有 created_identity，直接跳过
        self.assertIn("跳过", msg)


class EditUsesCorrectFieldTypeTests(unittest.TestCase):
    """
    回归用例：edit_and_verify 曾经会漏掉字段类型信息，把 select/date 都当成
    text 硬填——这里锁定它必须从 created_field_schema 里正确取到类型。
    """

    def test_edit_uses_schema_type_not_default_text(self):
        ctx = FakeCrudCtx(table=[{"国家": "中国", "负责人": "张三"}])
        ctx.vars["created_identity"] = "auto_x9f2"
        ctx.vars["created_identity_column"] = "负责人"
        ctx.vars["created_field_schema"] = FIELDS   # 国家是 select 类型

        def fake_find_row_by(page, column, value):
            return 0
        ctx.ui.find_row_by = fake_find_row_by
        ctx.ui.table = [{"国家": "俄罗斯", "负责人": "auto_x9f2"}]

        do_edit_and_verify(ctx, fields={"国家": "俄罗斯"})
        kinds = [t for t, label, v in ctx.ui.set_calls if label == "国家"]
        self.assertIn("select", kinds)   # 不能退化成默认的 "text"


class FormPrefilledTests(unittest.TestCase):
    def test_matching_values_pass(self):
        ctx = FakeCrudCtx(
            table=[{"负责人": "auto_a1", "国家": "中国"}],
            dialog_values={"负责人": "auto_a1", "国家": "中国"})
        ctx.vars["created_identity"] = "auto_a1"
        ctx.vars["created_identity_column"] = "负责人"
        msg = as_form_prefilled(ctx)
        self.assertIn("通过", msg)

    def test_mismatched_prefill_fails(self):
        # 列表显示"中国"，但编辑框回显成了别的——典型的回显 bug
        ctx = FakeCrudCtx(
            table=[{"负责人": "auto_a1", "国家": "中国"}],
            dialog_values={"负责人": "auto_a1", "国家": "俄罗斯"})
        ctx.vars["created_identity"] = "auto_a1"
        ctx.vars["created_identity_column"] = "负责人"
        with self.assertRaises(AssertionFailed):
            as_form_prefilled(ctx)


class DetailMatchesTests(unittest.TestCase):
    def test_matching_detail_passes(self):
        ctx = FakeCrudCtx(
            table=[{"负责人": "auto_a1", "状态": "生效"}],
            dialog_values={"负责人": "auto_a1", "状态": "生效"})
        ctx.vars["created_identity"] = "auto_a1"
        ctx.vars["created_identity_column"] = "负责人"
        msg = as_detail_matches(ctx)
        self.assertIn("通过", msg)

    def test_stale_detail_value_fails(self):
        # 列表已经显示新状态，详情弹窗还是旧值——"列表对但详情是老值"的典型场景
        ctx = FakeCrudCtx(
            table=[{"负责人": "auto_a1", "状态": "失效"}],
            dialog_values={"负责人": "auto_a1", "状态": "生效"})
        ctx.vars["created_identity"] = "auto_a1"
        ctx.vars["created_identity_column"] = "负责人"
        with self.assertRaises(AssertionFailed):
            as_detail_matches(ctx)


if __name__ == "__main__":
    unittest.main()
