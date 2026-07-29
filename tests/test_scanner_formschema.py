import sys
import types
import unittest

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.TimeoutError = type('TimeoutError', (Exception,), {})
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine.scanner import PageScanner


class FakeLoc:
    """极简 locator 桩：count() 按选择器是否在 present 集合里返回 0/1。"""

    def __init__(self, present, sel, attrs=None, texts=None):
        self._hit = sel in present
        self._attrs = attrs or {}
        self._sel = sel
        self._texts = texts or []

    def count(self):
        return 1 if self._hit else 0

    @property
    def first(self):
        return self

    def get_attribute(self, name):
        return self._attrs.get((self._sel, name))

    def all_inner_texts(self):
        return self._texts


class FakeItem:
    def __init__(self, present, attrs=None, texts=None):
        self.present = set(present)
        self.attrs = attrs or {}
        self.texts = texts or {}

    def locator(self, sel):
        return FakeLoc(self.present, sel, self.attrs, self.texts.get(sel))


class FakePage:
    def on(self, *a, **kw):
        pass


class FieldControlPriorityTests(unittest.TestCase):
    """
    _field_control 靠一串 if/elif 判断控件类型，顺序判错会产生隐蔽 bug——
    比如 el-upload 内部也可能套一个 <input>，如果 upload 判断排在 input 后面，
    上传字段会被误判成普通文本框，后面自动填表会往上传控件里塞随机字符串。
    """

    def setUp(self):
        self.sc = PageScanner(FakePage())

    def test_upload_wins_even_if_input_also_present(self):
        item = FakeItem({".el-upload", "input"})
        self.assertEqual({"type": "upload", "fillable": False},
                         self.sc._field_control(item))

    def test_switch_wins_over_input(self):
        item = FakeItem({".el-switch", "input"})
        self.assertEqual("switch", self.sc._field_control(item)["type"])

    def test_radio_group_detected_with_options(self):
        # has() 探测用组合选择器 ".el-radio-group, .el-radio"；实际取选项文本
        # 时 _choice_texts 用的是单独的 ".el-radio"，两处选择器不一样
        item = FakeItem({".el-radio-group, .el-radio"},
                        texts={".el-radio": [" 是 ", "否", ""]})
        f = self.sc._field_control(item)
        self.assertEqual("radio", f["type"])
        self.assertEqual(["是", "否"], f["options"])   # 空文本过滤掉、去掉前后空格

    def test_cascader_marked_unfillable(self):
        item = FakeItem({".el-cascader"})
        self.assertEqual({"type": "cascader", "fillable": False},
                         self.sc._field_control(item))

    def test_date_range_vs_single_date(self):
        single = FakeItem({".el-date-editor"})
        self.assertEqual("date", self.sc._field_control(single)["type"])
        ranged = FakeItem({".el-date-editor", ".el-range-input"})
        self.assertEqual("date_range", self.sc._field_control(ranged)["type"])

    def test_textarea_maxlength_read(self):
        item = FakeItem({"textarea"}, attrs={("textarea", "maxlength"): "200"})
        f = self.sc._field_control(item)
        self.assertEqual("textarea", f["type"])
        self.assertEqual(200, f["maxlength"])

    def test_input_maxlength_read(self):
        item = FakeItem({"input"}, attrs={("input", "maxlength"): "50"})
        f = self.sc._field_control(item)
        self.assertEqual("text", f["type"])
        self.assertEqual(50, f["maxlength"])

    def test_input_without_maxlength_attr_is_none(self):
        item = FakeItem({"input"})
        f = self.sc._field_control(item)
        self.assertIsNone(f["maxlength"])

    def test_no_recognizable_control_is_unknown(self):
        item = FakeItem(set())
        self.assertEqual({"type": "unknown", "fillable": False},
                         self.sc._field_control(item))


if __name__ == "__main__":
    unittest.main()
