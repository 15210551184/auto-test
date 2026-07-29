import json
import tempfile
import unittest
from pathlib import Path

from autotest.engine.report import build_run_label, render_meta


class BuildRunLabelTests(unittest.TestCase):
    """
    历史报告列表要一眼看出"这份报告测了什么"，不用点开报告去猜——标签
    要把页面数、勾选的类别、执行语言都拼进一句话摘要里。
    """

    def test_no_tags_means_all_categories(self):
        label = build_run_label(5, only_tags=None, language_display=None)
        self.assertEqual("5 个页面 · 全部类别 · 默认语言", label)

    def test_tags_joined_with_chinese_labels(self):
        label = build_run_label(3, only_tags=["smoke", "search"], language_display=None)
        self.assertEqual("3 个页面 · 冒烟+搜索筛选 · 默认语言", label)

    def test_unknown_tag_falls_back_to_raw_code(self):
        # 以后加了新 tag 但没来得及补中文对照，也不能让整个标签生成报错
        label = build_run_label(1, only_tags=["brand_new_tag"], language_display=None)
        self.assertIn("brand_new_tag", label)

    def test_language_display_included(self):
        label = build_run_label(5, only_tags=["crud"], language_display="English")
        self.assertEqual("5 个页面 · 新增/修改/删除 · English", label)


class RenderMetaTests(unittest.TestCase):
    def test_writes_expected_json_structure(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "meta.json"
            render_meta(str(out), project="出行管理系统", page_count=5,
                       only_tags=["smoke", "search"], exclude_tags=["i18n"],
                       language="en", language_display="English")
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("出行管理系统", data["project"])
            self.assertEqual(["smoke", "search"], data["tags"])
            self.assertEqual(["i18n"], data["exclude_tags"])
            self.assertEqual("en", data["language"])
            self.assertEqual("English", data["language_display"])
            self.assertEqual("5 个页面 · 冒烟+搜索筛选 · English", data["label"])

    def test_defaults_produce_all_categories_default_language(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "meta.json"
            render_meta(str(out), page_count=1)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("1 个页面 · 全部类别 · 默认语言", data["label"])


if __name__ == "__main__":
    unittest.main()
