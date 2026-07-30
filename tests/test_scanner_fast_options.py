import unittest
from unittest.mock import MagicMock

from autotest.engine.scanner import PageScanner


def _scanner_with_select(select, control, option_texts=None):
    page = MagicMock()
    item = MagicMock()
    item.locator.return_value.first = select
    select.locator.return_value.first = control
    dropdown = MagicMock()
    dropdown.all_inner_texts.return_value = option_texts or []
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


if __name__ == "__main__":
    unittest.main()
