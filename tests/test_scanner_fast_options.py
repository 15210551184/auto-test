import unittest
from unittest.mock import MagicMock, patch

from autotest.engine.scanner import (
    FORM_OPTION_PROBE_BUDGET_MS,
    PageScanner,
)


def _scanner_with_select(select, control, option_texts=None):
    page = MagicMock()
    item = MagicMock()
    item.locator.return_value.first = select
    select.locator.return_value.first = control
    dropdown = MagicMock()
    dropdown.evaluate_all.return_value = option_texts or []
    page.locator.return_value = dropdown
    scanner = PageScanner.__new__(PageScanner)
    scanner.page = page
    return scanner, item, page


class FastSelectOptionScanTest(unittest.TestCase):
    def test_reads_associated_popper_without_clicking(self):
        select = MagicMock()
        control = MagicMock()
        control.count.return_value = 1
        control.get_attribute.side_effect = lambda name: {
            "disabled": None,
            "aria-disabled": "false",
            "aria-controls": "country-options",
            "aria-owns": None,
        }.get(name)
        select.get_attribute.return_value = "el-select"
        scanner, item, page = _scanner_with_select(
            select, control, ["中国", "塞内加尔"])

        self.assertEqual(["中国", "塞内加尔"], scanner._peek_options(item))
        page.locator.assert_called_once_with(
            '[id="country-options"] .el-select-dropdown__item')
        select.click.assert_not_called()

    def test_disabled_select_returns_immediately(self):
        select = MagicMock()
        control = MagicMock()
        control.count.return_value = 1
        control.get_attribute.side_effect = lambda name: {
            "disabled": "",
            "aria-disabled": "true",
        }.get(name)
        select.get_attribute.return_value = "el-select is-disabled"
        scanner, item, page = _scanner_with_select(select, control)

        self.assertEqual([], scanner._peek_options(item))
        select.click.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_scan_form_uses_one_dom_snapshot(self):
        page = MagicMock()
        items = MagicMock()
        items.evaluate_all.return_value = [
            {"label": "国家名称", "type": "text",
             "placeholder": "请输入国家名称", "_index": 0},
            {"label": "状态", "type": "select",
             "options": ["生效", "失效"], "placeholder": "请选择状态", "_index": 1,
             "_disabled": False, "searchable": False},
        ]
        page.locator.return_value = items
        scanner = PageScanner.__new__(PageScanner)
        scanner.page = page

        with patch.object(scanner, "_resolve_cascading_selects") as cascading:
            fields = scanner.scan_form()

        self.assertEqual([
            {"label": "国家名称", "type": "text",
             "placeholder": "请输入国家名称"},
            {"label": "状态", "type": "select",
             "options": ["生效", "失效"], "placeholder": "请选择状态",
             "searchable": False},
        ], fields)
        self.assertEqual(1, items.evaluate_all.call_count)
        items.nth.assert_not_called()
        cascading.assert_called_once()

    def test_all_interactive_selects_share_one_page_budget(self):
        page = MagicMock()
        items = MagicMock()
        items.evaluate_all.return_value = [
            {"label": f"筛选{i}", "type": "select", "options": [],
             "_index": i, "_disabled": False}
            for i in range(20)
        ]
        page.locator.return_value = items
        scanner = PageScanner.__new__(PageScanner)
        scanner.page = page

        # 第一个 monotonic 建 deadline；之后每个下拉消耗 1 秒。达到 5 秒
        # 总预算后必须停止，不能按 20 个控件各等一次超时。
        ticks = iter([0, 0, 1, 2, 3, 4, 5])
        with patch("autotest.engine.scanner.time.monotonic",
                   side_effect=lambda: next(ticks)), \
             patch.object(scanner, "_peek_options", return_value=[]) as peek, \
             patch.object(scanner, "_resolve_cascading_selects"):
            scanner.scan_form()

        self.assertLess(peek.call_count, 20)
        self.assertLessEqual(peek.call_count, FORM_OPTION_PROBE_BUDGET_MS // 1000)


if __name__ == "__main__":
    unittest.main()
