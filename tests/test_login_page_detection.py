import unittest

from autotest.engine.login import (
    LOGIN_PASSWORD_PROBE_TIMEOUT_MS,
    is_login_page,
)


class FakePassword:
    def __init__(self, attached=False, visible=False):
        self.first = self
        self.attached = attached
        self.visible = visible
        self.wait_args = None

    def wait_for(self, **kwargs):
        self.wait_args = kwargs
        if not self.attached:
            raise TimeoutError("not attached")

    def is_visible(self):
        return self.visible


class FakePage:
    def __init__(self, url, password=None):
        self.url = url
        self.password = password or FakePassword()

    def locator(self, selector):
        self.selector = selector
        return self.password


class LoginPageDetectionTest(unittest.TestCase):
    def test_url_hint_returns_immediately(self):
        page = FakePage("https://example.test/login")
        self.assertTrue(is_login_page(page))
        self.assertIsNone(page.password.wait_args)

    def test_business_page_uses_bounded_password_probe(self):
        page = FakePage("https://example.test/drivers")
        self.assertFalse(is_login_page(page))
        self.assertEqual({
            "state": "attached",
            "timeout": LOGIN_PASSWORD_PROBE_TIMEOUT_MS,
        }, page.password.wait_args)

    def test_visible_password_field_is_login_page(self):
        page = FakePage(
            "https://example.test/account",
            FakePassword(attached=True, visible=True),
        )
        self.assertTrue(is_login_page(page))

    def test_hidden_password_field_is_not_login_page(self):
        page = FakePage(
            "https://example.test/profile",
            FakePassword(attached=True, visible=False),
        )
        self.assertFalse(is_login_page(page))


if __name__ == "__main__":
    unittest.main()
