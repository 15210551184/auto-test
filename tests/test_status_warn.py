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
