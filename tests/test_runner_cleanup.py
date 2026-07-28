import sys
import types
import unittest

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine.actions import AssertionFailed, action
from autotest.engine.models import Case, Status, Step
from autotest.engine.runner import run_case


@action("__test_boom")
def _boom(ctx, **kw):
    raise AssertionFailed("故意失败，模拟 edit_and_verify 出错")


class FakePage:
    def goto(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass


class FakeUI:
    def __init__(self):
        self.delete_calls = 0


class FakeConfig:
    list_api = None
    login = {}   # 假值：run_case 里 "if ctx.config.login and ..." 直接短路，不用假 is_login_page
    url = "http://x.test/franchise"


class FakeCtx:
    def __init__(self):
        self.page = FakePage()
        self.config = FakeConfig()
        self.ui = FakeUI()
        self.target_language = None
        self.vars = {"created_identity": None, "created_identity_column": None}
        self.console_errors = []
        self.failed_requests = []

    def reset_signals(self):
        self.console_errors.clear()
        self.failed_requests.clear()

    def selector(self, key):
        return key

    def resolve(self, v):
        return v

    def shot(self, tag):
        return None   # 测试不需要真截图


class CleanupRunsAfterEarlyFailureTests(unittest.TestCase):
    def test_trailing_delete_and_verify_still_runs_after_earlier_failure(self):
        ctx = FakeCtx()
        # 没有待清理记录时 delete_and_verify 会直接返回"跳过"，但关键是
        # 它必须真的被执行到——用一个哨兵替换掉，验证确实被调用了
        called = {"n": 0}

        import autotest.engine.actions as A
        orig = A.REGISTRY["delete_and_verify"]

        def spy_delete(ctx, **kw):
            called["n"] += 1
            return orig(ctx, **kw)

        A.REGISTRY["delete_and_verify"] = spy_delete
        try:
            case = Case(name="新增-修改-详情-删除完整闭环", steps=[
                Step("__test_boom", {}),      # 模拟 edit_and_verify 这类步骤失败
                Step("delete_and_verify", {}),
            ])
            result = run_case(ctx, case)
        finally:
            A.REGISTRY["delete_and_verify"] = orig

        self.assertEqual(Status.FAIL, result.status)   # 案例本身仍然正确报告为失败
        self.assertEqual(1, called["n"])                # 但清理步骤必须被执行过
        # 结果列表里包含失败步骤 + 补跑的清理步骤，两条都在
        self.assertEqual(2, len(result.steps))

    def test_no_trailing_cleanup_step_means_no_extra_run(self):
        ctx = FakeCtx()
        case = Case(name="随便一个用例", steps=[
            Step("__test_boom", {}),
            Step("__test_boom", {}),   # 案例里没有 delete_and_verify，不该凭空跑出一条
        ])
        result = run_case(ctx, case)
        self.assertEqual(Status.FAIL, result.status)
        self.assertEqual(1, len(result.steps))   # 快速失败：第二步没跑，也没有清理步骤可补


if __name__ == "__main__":
    unittest.main()
