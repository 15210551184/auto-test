import os
import sys
import tempfile
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

from autotest.engine.models import PageConfig
from autotest.engine.runner import Context


class FakePage:
    def __init__(self):
        self.saved = []

    def on(self, *a, **kw):
        pass

    def screenshot(self, path=None, full_page=False):
        with open(path, "wb") as f:
            f.write(b"fake-png")
        self.saved.append(path)


class ShotPathTests(unittest.TestCase):
    """
    Context.shot() 存截图、返回一个相对路径给报告里的 <img src> 用。这个相对
    路径必须相对 report.html 真正写在哪算，不是相对截图文件本身存在哪个
    目录——批量执行时这两个目录不是同一个（截图存在每个页面自己的子目录，
    汇总报告写在批量任务顶层），算错了报告里的图会全部裂掉。
    """

    def test_single_page_run_report_root_defaults_to_out_dir(self):
        # 单页执行：report.html 就写在 out_dir 里，不传 report_root 应该退回
        # 用 out_dir 算——这是原来就有、必须保持不变的行为。
        with tempfile.TemporaryDirectory() as out_dir:
            ctx = Context(FakePage(), PageConfig(name="x", url="http://x"), out_dir)
            rel = ctx.shot("fail_x")
            # 报告写在 out_dir/report.html，据此解析出来的路径必须真实存在
            resolved = os.path.join(out_dir, rel)
            self.assertTrue(os.path.isfile(resolved))

    def test_batch_run_report_root_is_top_level_dir(self):
        # 批量执行：截图物理存在 page_out（out_dir 的子目录）里，但汇总报告
        # 写在 out_dir 顶层——返回的相对路径必须相对 out_dir 算，不能相对
        # page_out 算，否则报告按自己的位置解析会找不到文件。
        with tempfile.TemporaryDirectory() as out_dir:
            page_out = os.path.join(out_dir, "01_某页面")
            ctx = Context(FakePage(), PageConfig(name="x", url="http://x"),
                         page_out, report_root=out_dir)
            rel = ctx.shot("fail_x")
            # 报告写在 out_dir/report.html，据此解析出来的路径必须真实存在
            resolved = os.path.join(out_dir, rel)
            self.assertTrue(os.path.isfile(resolved))
            # 反例：如果错误地相对 page_out 解析（旧 bug 的行为），会指向一个
            # 不存在的文件——用这条断言把回归钉死
            wrong_resolved = os.path.join(page_out, rel)
            self.assertFalse(os.path.isfile(wrong_resolved))

    def test_batch_run_path_starts_with_page_subdir(self):
        with tempfile.TemporaryDirectory() as out_dir:
            page_out = os.path.join(out_dir, "01_某页面")
            ctx = Context(FakePage(), PageConfig(name="x", url="http://x"),
                         page_out, report_root=out_dir)
            rel = ctx.shot("fail_x")
            self.assertTrue(rel.startswith("01_某页面" + os.sep))


if __name__ == "__main__":
    unittest.main()
