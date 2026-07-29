import sys
import tempfile
import types
import unittest
from pathlib import Path

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

from autotest.engine import export_verify as EV
from autotest.engine.actions import AssertionFailed


class FakeUI:
    def __init__(self, headers, total=None):
        self._headers = headers
        self._total = total

    def headers(self, page):
        return self._headers

    def total_count(self, page):
        return self._total


class FakeCtx:
    def __init__(self, report_root, ui_headers, total=None, export_mode="direct"):
        self.ui = FakeUI(ui_headers, total)
        self.page = None
        self.report_root = report_root
        self.config = types.SimpleNamespace(export_mode=export_mode)
        self.data = {}
        self.shot_calls = []

    def shot(self, tag):
        self.shot_calls.append(tag)
        return f"screenshots/{tag}.png"


class ExportDetailAttachmentTests(unittest.TestCase):
    """
    导出验证失败时，报告里光一句"缺少列 X/Y"判断不了是导出真漏了还是
    工具自己表头识别多了——这里验证 verify_export() 把导出文件本身（可
    下载链接）和当时的页面截图都挂到 AssertionFailed.detail 上，
    run_step() 才能把它们搬进 StepResult 供 report.py 渲染。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_path = Path(self.tmp.name) / "city_1785312571891.xlsx"
        self.fake_path.write_bytes(b"x" * 4096)
        self._orig_download_direct = EV._download_direct
        self._orig_read_table = EV._read_table
        EV._download_direct = lambda ctx, timeout: str(self.fake_path)

    def tearDown(self):
        EV._download_direct = self._orig_download_direct
        EV._read_table = self._orig_read_table
        self.tmp.cleanup()

    def test_failure_attaches_download_link(self):
        EV._read_table = lambda path: [{"国家": "中国"}]   # 缺"状态"这一列
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家", "状态"])

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(ctx)

        detail = cm.exception.detail
        self.assertEqual("导出文件 city_1785312571891.xlsx", detail["download"]["label"])
        self.assertEqual("city_1785312571891.xlsx", detail["download"]["path"])
        # 页面截图不用在这里另外截——run_step() 对任何失败都会自动截一张，
        # 这里再截就是重复的两张几乎一样的图。
        self.assertEqual([], ctx.shot_calls)

    def test_download_path_relative_to_report_root(self):
        # 报告 html 跟下载文件不在同一目录时（批量执行常见），链接要相对
        # report_root 算，不是相对下载文件自己所在目录。
        report_root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(report_root, ignore_errors=True))
        EV._read_table = lambda path: [{"国家": "中国"}]
        ctx = FakeCtx(report_root, ui_headers=["国家", "状态"])

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(ctx)

        rel = cm.exception.detail["download"]["path"]
        self.assertFalse(Path(rel).is_absolute())
        self.assertTrue((Path(report_root) / rel).resolve() == self.fake_path.resolve())

    def test_success_also_attaches_download_link(self):
        # 通过的导出也该能下载下来看一眼，不是只有失败才留证据。
        EV._read_table = lambda path: [{"国家": "中国", "状态": "启用"}]
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家", "状态"], total=1)

        msg, detail = EV.verify_export(ctx)

        self.assertIn("导出验证通过", msg)
        self.assertEqual("city_1785312571891.xlsx", detail["download"]["path"])
        self.assertEqual([], ctx.shot_calls)   # 通过不需要留截图，只留文件链接


if __name__ == "__main__":
    unittest.main()
