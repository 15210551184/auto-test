import unittest

from autotest.engine import browser


class FakeRequest:
    def __init__(self, resource_type, url="http://example.test/assets/a"):
        self.resource_type = resource_type
        self.url = url


class FakeRoute:
    def __init__(self, resource_type, url="http://example.test/assets/a"):
        self.request = FakeRequest(resource_type, url)
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class FakeContext:
    def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


class ScanContextOptimizationTests(unittest.TestCase):
    def test_heavy_visual_resources_are_blocked(self):
        context = FakeContext()
        browser.optimize_scan_context(context)
        self.assertEqual("**/*", context.pattern)
        for resource_type in ("image", "media", "font"):
            route = FakeRoute(resource_type)
            context.handler(route)
            self.assertEqual("abort", route.action)

    def test_dom_and_api_resources_continue(self):
        context = FakeContext()
        browser.optimize_scan_context(context)
        for resource_type in ("document", "script", "stylesheet", "xhr", "fetch"):
            route = FakeRoute(resource_type)
            context.handler(route)
            self.assertEqual("continue", route.action)

    def test_login_captcha_image_is_not_blocked(self):
        context = FakeContext()
        browser.optimize_scan_context(context)
        route = FakeRoute("image", "http://example.test/api/captcha.png")
        context.handler(route)
        self.assertEqual("continue", route.action)


if __name__ == "__main__":
    unittest.main()
