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

from autotest.engine.models import Case, Status, Step
from autotest.engine.runner import run_case


class FakePage:
    def goto(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass

    def reload(self, *a, **kw):
        pass


class FakeConfig:
    list_api = None
    login = {}
    url = "http://x.test/franchise"


class FakeCtx:
    """
    只关心 run_case 是否在跑用例自己的步骤之前，先按 target_language 切了
    语言——不需要真的驱动浏览器，REGISTRY["switch_language"] 在测试里被
    换成一个记录调用的桩。
    """

    def __init__(self, target_language=None):
        self.page = FakePage()
        self.config = FakeConfig()
        self.target_language = target_language
        self.vars = {}
        self.console_errors = []
        self.failed_requests = []
        self.api_log = []

    def reset_signals(self):
        self.console_errors.clear()
        self.failed_requests.clear()
        self.api_log.clear()


class TargetLanguageSwitchTests(unittest.TestCase):
    def setUp(self):
        import autotest.engine.runner as runner_mod
        self._orig_registry = dict(runner_mod.REGISTRY)
        self.addCleanup(lambda: runner_mod.REGISTRY.clear() or runner_mod.REGISTRY.update(self._orig_registry))
        self.calls = []

        def fake_switch(ctx, to=None, **kw):
            self.calls.append(to)
            return f"已切换到 {to}"

        runner_mod.REGISTRY["switch_language"] = fake_switch
        runner_mod.REGISTRY["__noop"] = lambda ctx, **kw: "ok"

    def test_no_target_language_skips_switch(self):
        ctx = FakeCtx(target_language=None)
        case = Case(name="搜索-国家", steps=[Step.from_raw({"__noop": None})], tags=[])
        run_case(ctx, case)
        self.assertEqual([], self.calls)

    def test_target_language_switches_before_case_steps(self):
        ctx = FakeCtx(target_language="en")
        case = Case(name="搜索-国家", steps=[Step.from_raw({"__noop": None})], tags=[])
        result = run_case(ctx, case)
        self.assertEqual(["en"], self.calls)
        self.assertEqual(Status.PASS, result.status)

    def test_switch_failure_reports_error_without_running_steps(self):
        import autotest.engine.runner as runner_mod

        def failing_switch(ctx, to=None, **kw):
            raise LookupError(f"未知语言 '{to}'")

        runner_mod.REGISTRY["switch_language"] = failing_switch
        ran = []
        runner_mod.REGISTRY["__noop"] = lambda ctx, **kw: ran.append(1)

        ctx = FakeCtx(target_language="fr")
        case = Case(name="搜索-国家", steps=[Step.from_raw({"__noop": None})], tags=[])
        result = run_case(ctx, case)
        self.assertEqual(Status.ERROR, result.status)
        self.assertIn("切换语言失败", result.error)
        self.assertEqual([], ran)   # 切换失败不该继续跑用例自己的步骤

    def test_switch_timeout_refreshes_and_retries_once(self):
        import autotest.engine.runner as runner_mod
        attempts = []

        def flaky_switch(ctx, to=None, **kw):
            attempts.append(to)
            if len(attempts) == 1:
                raise RuntimeError("Locator.click: Timeout 5000ms exceeded")
            return "ok"

        runner_mod.REGISTRY["switch_language"] = flaky_switch
        ctx = FakeCtx(target_language="fr")
        case = Case(name="导出", steps=[Step.from_raw({"__noop": None})], tags=[])

        result = run_case(ctx, case)

        self.assertEqual(["fr", "fr"], attempts)
        self.assertEqual(Status.PASS, result.status)


if __name__ == "__main__":
    unittest.main()
