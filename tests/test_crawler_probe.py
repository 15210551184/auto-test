import unittest
from unittest.mock import patch

from autotest.engine import crawler


class FakeProbePage:
    def __init__(self, dom_timeout=False):
        self.url = "https://example.test/final"
        self.dom_timeout = dom_timeout
        self.goto_args = None
        self.default_timeout = None
        self.nav_timeout = None
        self.closed = False

    def set_default_timeout(self, value):
        self.default_timeout = value

    def set_default_navigation_timeout(self, value):
        self.nav_timeout = value

    def goto(self, url, **kwargs):
        self.goto_args = (url, kwargs)

    def wait_for_load_state(self, state, timeout):
        if self.dom_timeout:
            raise TimeoutError("still loading")

    def wait_for_timeout(self, milliseconds):
        pass

    def close(self, run_before_unload=False):
        self.closed = True


class FakeOwnerPage:
    def __init__(self, probe_page):
        self.context = type("Context", (), {
            "new_page": lambda _self: probe_page,
        })()


class FakeHeaders:
    def __init__(self, should_timeout=False):
        self.first = self
        self.should_timeout = should_timeout
        self.wait_args = None

    def wait_for(self, **kwargs):
        self.wait_args = kwargs
        if self.should_timeout:
            raise TimeoutError("no table")


class FakeStructurePage:
    def __init__(self, should_timeout=False):
        self.headers = FakeHeaders(should_timeout)
        self.settle_wait = None

    def locator(self, selector):
        self.selector = selector
        return self.headers

    def wait_for_timeout(self, milliseconds):
        self.settle_wait = milliseconds


class BoundedMenuProbeTest(unittest.TestCase):
    def test_probe_uses_commit_and_closes_isolated_page(self):
        probe = FakeProbePage()
        logs = []
        with patch.object(crawler, "is_login_page", return_value=False), \
             patch.object(crawler, "_probe_page",
                          return_value={"has_table": True}) as inspect:
            url, info, login = crawler._visit_and_probe(
                FakeOwnerPage(probe), "https://example.test/menu", logs.append)

        self.assertEqual("https://example.test/final", url)
        self.assertEqual({"has_table": True}, info)
        self.assertFalse(login)
        self.assertEqual("commit", probe.goto_args[1]["wait_until"])
        self.assertEqual(crawler.PROBE_NAV_TIMEOUT_MS,
                         probe.goto_args[1]["timeout"])
        self.assertEqual(crawler.PROBE_ACTION_TIMEOUT_MS,
                         probe.default_timeout)
        self.assertEqual(crawler.PROBE_NAV_TIMEOUT_MS, probe.nav_timeout)
        self.assertTrue(probe.closed)
        inspect.assert_called_once_with(probe, logs.append)

    def test_dom_timeout_falls_back_to_current_content(self):
        probe = FakeProbePage(dom_timeout=True)
        logs = []
        with patch.object(crawler, "is_login_page", return_value=False), \
             patch.object(crawler, "_probe_page", return_value={}):
            crawler._visit_and_probe(
                FakeOwnerPage(probe), "https://example.test/menu", logs.append)

        self.assertTrue(any("仍在加载" in line for line in logs))
        self.assertTrue(probe.closed)

    def test_login_page_is_not_inspected(self):
        probe = FakeProbePage()
        with patch.object(crawler, "is_login_page", return_value=True), \
             patch.object(crawler, "_probe_page") as inspect:
            _, info, login = crawler._visit_and_probe(
                FakeOwnerPage(probe), "https://example.test/menu")

        self.assertEqual({}, info)
        self.assertTrue(login)
        inspect.assert_not_called()
        self.assertTrue(probe.closed)

    def test_waits_for_table_headers_with_strict_budget(self):
        page = FakeStructurePage()
        self.assertTrue(crawler._wait_for_probe_structure(page))
        self.assertEqual({
            "state": "attached",
            "timeout": crawler.PROBE_STRUCTURE_BUDGET_MS,
        }, page.headers.wait_args)
        self.assertEqual(300, page.settle_wait)

    def test_page_without_table_falls_back_after_budget(self):
        page = FakeStructurePage(should_timeout=True)
        self.assertFalse(crawler._wait_for_probe_structure(page))
        self.assertIsNone(page.settle_wait)


if __name__ == "__main__":
    unittest.main()
