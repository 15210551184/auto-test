import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "yaml" not in sys.modules:
    _yaml_stub = types.ModuleType("yaml")
    _yaml_stub.safe_load = lambda text: (json.loads(text) if text and text.strip() else {})
    _yaml_stub.dump = lambda data, **kwargs: json.dumps(data, ensure_ascii=False)
    sys.modules["yaml"] = _yaml_stub

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

import yaml
from autotest.engine import batch, project, scanner


class RedetectListApiTests(unittest.TestCase):
    """
    只重新探测一个页面的 list_api、不整页重新扫描——真实需求是"猜错了
    改一下就好，别为了一个字段把表单/表头/按钮/弹窗全重扫一遍，还把
    手工加的业务断言冲掉"。这里打桩 scanner.redetect_list_api()（真正
    驱动浏览器的部分），只验证 batch.redetect_list_api() 自己的编排逻辑：
    只改 list_api 一个字段，配置文件其余内容原样保留。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_projects_dir = project.PROJECTS_DIR
        project.PROJECTS_DIR = Path(self.tmp.name)
        project.create_project("测试系统", "http://example.test", {})
        data = project.load_project("测试系统")
        data["pages"] = [{"name": "国家管理",
                          "url": "http://example.test/web/country/list",
                          "selected": True}]
        project.save_project(data)
        self.cfg_path = project.page_config_path("测试系统", "国家管理")
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(yaml.dump({
            "name": "国家管理",
            "url": "http://example.test/web/country/list",
            "list_api": "/api/vaWeb/system/notice/listTop",
            "cases": [{"name": "手工加的断言",
                      "steps": [{"assert_row_count": {"min": 1}}]}],
        }), encoding="utf-8")
        self._orig_redetect = scanner.redetect_list_api

    def tearDown(self):
        project.PROJECTS_DIR = self.original_projects_dir
        self.tmp.cleanup()
        scanner.redetect_list_api = self._orig_redetect

    def test_patches_only_list_api_field(self):
        scanner.redetect_list_api = (
            lambda url, storage_state=None, login=None, **kw: "/api/vaWeb/system/country/list")
        r = batch.redetect_list_api("测试系统", "国家管理")
        self.assertTrue(r["changed"])
        self.assertEqual("/api/vaWeb/system/notice/listTop", r["old"])
        self.assertEqual("/api/vaWeb/system/country/list", r["new"])

        cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual("/api/vaWeb/system/country/list", cfg["list_api"])
        # 没让它碰的字段必须原样保留——尤其是用户手工加的用例
        self.assertEqual([{"name": "手工加的断言",
                          "steps": [{"assert_row_count": {"min": 1}}]}], cfg["cases"])
        self.assertEqual("国家管理", cfg["name"])

    def test_no_candidate_found_keeps_old_value(self):
        scanner.redetect_list_api = (
            lambda url, storage_state=None, login=None, **kw: None)
        r = batch.redetect_list_api("测试系统", "国家管理")
        self.assertFalse(r["changed"])
        self.assertIsNone(r["new"])
        cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual("/api/vaWeb/system/notice/listTop", cfg["list_api"])

    def test_same_value_reports_unchanged(self):
        scanner.redetect_list_api = (
            lambda url, storage_state=None, login=None, **kw: "/api/vaWeb/system/notice/listTop")
        r = batch.redetect_list_api("测试系统", "国家管理")
        self.assertFalse(r["changed"])

    def test_missing_page_config_raises(self):
        data = project.load_project("测试系统")
        data["pages"].append({"name": "还没生成过用例的页面",
                              "url": "http://example.test/web/x", "selected": True})
        project.save_project(data)
        with self.assertRaises(ValueError):
            batch.redetect_list_api("测试系统", "还没生成过用例的页面")

    def test_unknown_page_raises(self):
        with self.assertRaises(ValueError):
            batch.redetect_list_api("测试系统", "不存在的页面")

    def test_unknown_project_raises(self):
        with self.assertRaises(ValueError):
            batch.redetect_list_api("不存在的系统", "国家管理")


if __name__ == "__main__":
    unittest.main()
