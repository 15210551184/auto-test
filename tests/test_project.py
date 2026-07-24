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
