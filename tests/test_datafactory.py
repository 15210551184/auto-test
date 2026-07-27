import re
import unittest

from autotest.engine import datafactory as DF


class SemanticDetectionTests(unittest.TestCase):
    def test_phone_field_detected_by_label(self):
        self.assertEqual("phone", DF.semantic_of("加盟商联系方式"))
        self.assertEqual("phone", DF.semantic_of("手机号"))

    def test_unrelated_label_has_no_semantic(self):
        self.assertIsNone(DF.semantic_of("加盟商名称"))


class ValueForTests(unittest.TestCase):
    def test_phone_semantic_produces_valid_looking_mobile(self):
        v = DF.value_for({"label": "联系方式", "type": "text"})
        self.assertRegex(v, r"^1\d{10}$")

    def test_plain_text_gets_auto_prefix(self):
        v = DF.value_for({"label": "负责人", "type": "text"})
        self.assertTrue(v.startswith(DF.AUTO_PREFIX))

    def test_maxlength_is_respected(self):
        v = DF.value_for({"label": "备注", "type": "text", "maxlength": 5})
        self.assertLessEqual(len(v), 5)

    def test_select_picks_first_real_option_not_placeholder(self):
        v = DF.value_for({"label": "国家", "type": "select",
                          "options": ["请选择", "中国", "俄罗斯"]})
        self.assertEqual("中国", v)

    def test_select_with_only_placeholders_returns_none(self):
        v = DF.value_for({"label": "X", "type": "select", "options": ["请选择", "全部"]})
        self.assertIsNone(v)

    def test_upload_field_is_skipped(self):
        self.assertIsNone(DF.value_for({"label": "头像", "type": "upload", "fillable": False}))

    def test_cascader_field_is_skipped(self):
        self.assertIsNone(DF.value_for({"label": "地区", "type": "cascader", "fillable": False}))

    def test_date_range_returns_two_dates(self):
        v = DF.value_for({"label": "创建时间", "type": "date_range"})
        self.assertEqual(2, len(v))
        for d in v:
            self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")

    def test_switch_returns_bool(self):
        self.assertIs(True, DF.value_for({"label": "启用", "type": "switch"}))


class FillValuesTests(unittest.TestCase):
    def setUp(self):
        self.fields = [
            {"label": "国家", "type": "select", "required": True,
             "options": ["请选择", "中国"]},
            {"label": "名称", "type": "text", "required": True, "maxlength": 50},
            {"label": "备注", "type": "textarea", "required": False, "maxlength": 200},
            {"label": "头像", "type": "upload", "required": False, "fillable": False},
        ]

    def test_full_fill_covers_all_fillable_fields(self):
        vals = DF.fill_values(self.fields)
        self.assertIn("国家", vals)
        self.assertIn("名称", vals)
        self.assertIn("备注", vals)
        self.assertNotIn("头像", vals)   # 填不了的字段不应该出现在结果里

    def test_required_only_skips_optional_fields(self):
        vals = DF.fill_values(self.fields, only_required=True)
        self.assertIn("国家", vals)
        self.assertIn("名称", vals)
        self.assertNotIn("备注", vals)


class OverlongValueTests(unittest.TestCase):
    def test_exceeds_maxlength(self):
        v = DF.overlong_value({"type": "text", "maxlength": 10})
        self.assertGreater(len(v), 10)

    def test_no_maxlength_returns_none(self):
        self.assertIsNone(DF.overlong_value({"type": "text"}))

    def test_non_text_type_returns_none(self):
        self.assertIsNone(DF.overlong_value({"type": "select", "maxlength": 10}))


class IsAutoDataTests(unittest.TestCase):
    def test_auto_prefixed_value_recognized(self):
        self.assertTrue(DF.is_auto_data("auto_x9f2"))

    def test_real_looking_value_not_flagged(self):
        # 铁律：清理时绝不能把真实业务数据误判成自动化造的数据
        self.assertFalse(DF.is_auto_data("北京出行二队"))
        self.assertFalse(DF.is_auto_data("张三丰"))


if __name__ == "__main__":
    unittest.main()
