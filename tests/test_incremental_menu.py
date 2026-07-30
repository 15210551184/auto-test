import unittest

from autotest.engine.crawler import _incremental_leaves


class IncrementalMenuFilterTest(unittest.TestCase):
    def test_skips_existing_path_and_route_but_keeps_new_menu(self):
        leaves = [
            {"name": "用户管理", "group": "系统管理",
             "menu_route": "/web/system/user"},
            {"name": "用户列表新名称", "group": "新分组",
             "menu_route": "/web/system/user?from=menu"},
            {"name": "角色管理", "group": "系统管理",
             "menu_route": "/web/system/role"},
        ]
        existing = [
            {"name": "用户管理", "group": "系统管理",
             "url": "http://example.test/web/system/user"},
        ]

        fresh, skipped = _incremental_leaves(
            leaves, "http://example.test/web/index", existing)

        self.assertEqual(2, skipped)
        self.assertEqual(["角色管理"], [leaf["name"] for leaf in fresh])

    def test_empty_existing_pages_keeps_every_leaf(self):
        leaves = [{"name": "国家管理", "group": "基础数据",
                   "menu_route": "/web/country"}]
        fresh, skipped = _incremental_leaves(
            leaves, "http://example.test/web/index", [])
        self.assertEqual(leaves, fresh)
        self.assertEqual(0, skipped)


if __name__ == "__main__":
    unittest.main()
