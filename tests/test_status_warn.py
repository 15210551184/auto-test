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

from autotest.engine.actions import AssertionFailed, AssertionWarning, action
from autotest.engine.models import CaseResult, PageResult, Status, Step
from autotest.engine.runner import run_step


@action("__test_warn_action")
def _warn_action(ctx, **kw):
    raise AssertionWarning("测试警告")


@action("__test_pass_action")
def _pass_action(ctx, **kw):
    return "ok"


@action("__test_fail_action")
def _fail_action(ctx, **kw):
    raise AssertionFailed("测试失败")


@action("__test_fail_with_detail_action")
def _fail_with_detail_action(ctx, **kw):
    raise AssertionFailed("测试失败", detail={"download": {"label": "x.xlsx", "path": "downloads/x.xlsx"}})


@action("__test_pass_with_detail_action")
def _pass_with_detail_action(ctx, **kw):
    return "ok", {"images": [{"label": "前", "path": "a.png"}, {"label": "后", "path": "b.png"}]}


class FakeCtx:
    def __init__(self):
        self.shot_calls = []

    def shot(self, tag):
        self.shot_calls.append(tag)
        return "shots/x.png"


class RunStepStatusMappingTests(unittest.TestCase):
    def test_warning_maps_to_warn_without_screenshot(self):
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_warn_action", {}))
        self.assertEqual(Status.WARN, r.status)
        self.assertIsNone(r.screenshot)
        self.assertEqual([], ctx.shot_calls)   # 警告不算失败，不需要留截图证据

    def test_failure_still_gets_fail_status_and_screenshot(self):
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_fail_action", {}))
        self.assertEqual(Status.FAIL, r.status)
        self.assertEqual(1, len(ctx.shot_calls))

    def test_pass_is_unaffected(self):
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_pass_action", {}))
        self.assertEqual(Status.PASS, r.status)
        self.assertEqual("ok", r.message)
        self.assertIsNone(r.detail)

    def test_pass_action_can_return_detail_tuple(self):
        # 动作可以返回 (消息, detail) 而不是只有消息——detail 是给报告用的
        # 结构化附件（对比截图/下载链接），run_step 得原样搬进 StepResult。
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_pass_with_detail_action", {}))
        self.assertEqual(Status.PASS, r.status)
        self.assertEqual("ok", r.message)
        self.assertEqual(2, len(r.detail["images"]))

    def test_failure_detail_is_propagated_from_exception(self):
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_fail_with_detail_action", {}))
        self.assertEqual(Status.FAIL, r.status)
        self.assertEqual({"label": "x.xlsx", "path": "downloads/x.xlsx"}, r.detail["download"])

    def test_failure_with_own_evidence_images_does_not_add_duplicate_screenshot(self):
        @action("__test_fail_with_images_action")
        def fail_with_images(ctx, **kw):
            raise AssertionFailed(
                "导出不一致",
                detail={"images": [
                    {"label": "列表页（完整列）", "path": "list.png"},
                    {"label": "导出文件内容", "path": "export.png"},
                ]},
            )

        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_fail_with_images_action", {}))

        self.assertEqual(Status.FAIL, r.status)
        self.assertIsNone(r.screenshot)
        self.assertEqual([], ctx.shot_calls)
        self.assertEqual(2, len(r.detail["images"]))

    def test_failure_without_detail_leaves_it_none(self):
        ctx = FakeCtx()
        r = run_step(ctx, Step("__test_fail_action", {}))
        self.assertIsNone(r.detail)


class PageResultWarnCountingTests(unittest.TestCase):
    def test_warn_case_counts_as_passed_not_failed(self):
        pr = PageResult("测试页", "http://x", cases=[
            CaseResult("c1", Status.PASS),
            CaseResult("c2", Status.WARN),
            CaseResult("c3", Status.FAIL),
        ])
        self.assertEqual(2, pr.passed)   # PASS + WARN
        self.assertEqual(1, pr.failed)


if __name__ == "__main__":
    unittest.main()
