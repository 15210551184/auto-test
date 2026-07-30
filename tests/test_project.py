import tempfile
import unittest
from pathlib import Path
import json
import sys
import types

if "yaml" not in sys.modules:
    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda text: json.loads(text)
    yaml.dump = lambda data, **kwargs: json.dumps(data, ensure_ascii=False)
    sys.modules["yaml"] = yaml

from autotest.engine import project


class RemovePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_projects_dir = project.PROJECTS_DIR
        project.PROJECTS_DIR = Path(self.tmp.name)
        project.create_project("测试系统", "http://example.test", {})
        data = project.load_project("测试系统")
        data["pages"] = [
            {"name": "用户管理", "selected": True},
            {"name": "角色管理", "selected": False},
        ]
        project.save_project(data)
        project.page_config_path("测试系统", "用户管理").write_text("name: 用户管理\n", encoding="utf-8")

    def tearDown(self):
        project.PROJECTS_DIR = self.original_projects_dir
        self.tmp.cleanup()

    def test_remove_page_removes_menu_entry_and_generated_config(self):
        self.assertTrue(project.remove_page("测试系统", "用户管理"))

        pages = project.load_project("测试系统")["pages"]
        self.assertEqual(["角色管理"], [page["name"] for page in pages])
        self.assertFalse(project.page_config_path("测试系统", "用户管理").exists())

    def test_remove_unknown_page_keeps_project_unchanged(self):
        self.assertFalse(project.remove_page("测试系统", "不存在"))
        self.assertEqual(2, len(project.load_project("测试系统")["pages"]))

    def test_remove_pages_removes_only_requested_pages_and_configs(self):
        self.assertEqual(1, project.remove_pages("测试系统", ["用户管理", "不存在"]))
        self.assertEqual(["角色管理"], [p["name"] for p in project.load_project("测试系统")["pages"]])

    def test_incremental_merge_preserves_existing_and_adds_new_page(self):
        data = project.load_project("测试系统")
        data["pages"][0]["scan_timeout"] = 240
        project.save_project(data)

        result = project.merge_pages("测试系统", [
            {"name": "用户管理", "group": "", "url": "/users",
             "recommended": False},
            {"name": "菜单管理", "group": "系统管理", "url": "/menus",
             "recommended": True},
        ])

        self.assertEqual({"total": 3, "added": 1}, result)
        pages = project.load_project("测试系统")["pages"]
        user = next(p for p in pages if p["name"] == "用户管理")
        menu = next(p for p in pages if p["name"] == "菜单管理")
        self.assertTrue(user["selected"])
        self.assertEqual(240, user["scan_timeout"])
        self.assertTrue(menu["selected"])

    def test_incremental_merge_deduplicates_same_url(self):
        data = project.load_project("测试系统")
        data["pages"][0]["url"] = "http://example.test/users?tab=1"
        project.save_project(data)

        result = project.merge_pages("测试系统", [
            {"name": "用户列表新名称", "group": "系统管理",
             "url": "http://example.test/users?tab=2", "recommended": True},
        ])

        self.assertEqual({"total": 2, "added": 0}, result)


class SetScanLanguagesTests(unittest.TestCase):
    """
    控制台用勾选框调 languages.scan_languages，不用手改 project.yaml
    ——这里验证的是持久化逻辑本身：只收 options 里真实存在的语言码，
    空勾选清掉这个字段（等于回到"只扫默认语言"），没配 languages.options
    的项目调用直接失败。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_projects_dir = project.PROJECTS_DIR
        project.PROJECTS_DIR = Path(self.tmp.name)
        project.create_project("多语言系统", "http://example.test", {})
        data = project.load_project("多语言系统")
        data["languages"] = {
            "switcher_trigger": ".lang",
            "options": {"zh": "中文", "en": "English", "fr": "Français"},
        }
        project.save_project(data)

    def tearDown(self):
        project.PROJECTS_DIR = self.original_projects_dir
        self.tmp.cleanup()

    def test_sets_scan_languages_to_picked_subset(self):
        self.assertTrue(project.set_scan_languages("多语言系统", ["en", "fr"]))
        langs = project.load_project("多语言系统")["languages"]
        self.assertEqual(["en", "fr"], langs["scan_languages"])
        self.assertEqual({"zh": "中文", "en": "English", "fr": "Français"}, langs["options"])

    def test_unknown_codes_are_dropped(self):
        self.assertTrue(project.set_scan_languages("多语言系统", ["en", "ar", "zz"]))
        langs = project.load_project("多语言系统")["languages"]
        self.assertEqual(["en"], langs["scan_languages"])

    def test_empty_codes_clears_scan_languages(self):
        project.set_scan_languages("多语言系统", ["en"])
        self.assertTrue(project.set_scan_languages("多语言系统", []))
        langs = project.load_project("多语言系统")["languages"]
        self.assertNotIn("scan_languages", langs)

    def test_fails_when_project_has_no_language_options(self):
        project.create_project("单语言系统", "http://example.test", {})
        self.assertFalse(project.set_scan_languages("单语言系统", ["en"]))

    def test_fails_for_unknown_project(self):
        self.assertFalse(project.set_scan_languages("不存在的系统", ["en"]))
