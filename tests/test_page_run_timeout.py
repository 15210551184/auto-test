import unittest

from autotest.engine.actions import action
from autotest.engine.models import Status, Step
from autotest.engine.runner import run_step


@action("__test_timeout_budget")
def _timeout_budget(ctx, timeout=90000, **kw):
    ctx.seen_timeout = timeout
    return "ok"


class FakePage:
    def set_default_timeout(self, timeout):
        self.default_timeout = timeout


class FakeCtx:
    def __init__(self, remaining):
        self.page = FakePage()
        self.remaining = remaining

    def remaining_case_ms(self):
        return self.remaining

    def set_phase(self, phase):
        pass

    def shot(self, tag):
        return None


class CaseRunTimeoutTests(unittest.TestCase):
    def test_action_default_timeout_is_capped_to_case_budget(self):
        ctx = FakeCtx(1234)
        result = run_step(ctx, Step("__test_timeout_budget", {}))
        self.assertEqual(Status.PASS, result.status)
        self.assertEqual(1234, ctx.seen_timeout)
        self.assertEqual(1234, ctx.page.default_timeout)

    def test_playwright_timeout_is_not_relaxed_beyond_thirty_seconds(self):
        ctx = FakeCtx(120000)
        result = run_step(ctx, Step("__test_timeout_budget", {}))
        self.assertEqual(Status.PASS, result.status)
        self.assertEqual(30000, ctx.page.default_timeout)

    def test_expired_case_does_not_start_next_step(self):
        ctx = FakeCtx(0)
        result = run_step(ctx, Step("__test_timeout_budget", {}))
        self.assertEqual(Status.ERROR, result.status)
        self.assertIn("超过 150 秒", result.message)
