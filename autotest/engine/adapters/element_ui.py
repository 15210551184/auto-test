"""
Element UI 适配层。

后台管理系统的 DOM 有固定套路，把这些套路封装在这里，
上层的动作执行器就不用关心 .el-form-item__label 之类的细节。
换成 Ant Design 只要再写一个同接口的适配器。
"""
import re
from typing import List, Optional

from playwright.sync_api import Page, Locator


class ElementUIAdapter:
    name = "element-ui"

    # ---------- 识别 ----------
    @staticmethod
    def detect(page: Page) -> bool:
        """页面上有没有 el-* 类名，用来自动选适配器"""
        return page.locator(".el-table, .el-form-item, .el-input").count() > 0

    # ---------- 表单 ----------
    def _form_item(self, page: Page, label: str) -> Locator:
        """
        Element 的 label 和控件是兄弟节点，没有 for/id 关联，
        所以要先定位到包含该 label 的 .el-form-item 容器再往下找控件。
        label 文本可能带冒号，用正则兼容。
        """
        pattern = re.compile(rf"^\s*{re.escape(label)}\s*[:：]?\s*$")
        item = page.locator(".el-form-item").filter(
            has=page.locator(".el-form-item__label").filter(has_text=pattern)
        )
        if item.count() == 0:
            # 有些页面不用 el-form-item，退化成找 label 的父容器
            item = page.locator(
                f"xpath=//*[contains(@class,'el-form-item')][.//label[normalize-space()='{label}']]"
            )
        if item.count() == 0:
            raise LookupError(f"找不到表单项: {label}")
        return item.first

    def fill(self, page: Page, label: str, value: str) -> None:
        item = self._form_item(page, label)
        inp = item.locator("input.el-input__inner, textarea.el-textarea__inner").first
        inp.click()
        inp.fill("")
        inp.type(str(value), delay=20)   # 有些页面监听 input 事件做联想，逐字符更稳
        page.keyboard.press("Escape")    # 关掉可能弹出的联想浮层

    def get_input_value(self, page: Page, label: str) -> str:
        item = self._form_item(page, label)
        return item.locator("input.el-input__inner").first.input_value()

    def select(
        self, page: Page, label: str,
        option: Optional[str] = None, index: Optional[int] = None,
    ) -> str:
        """
        下拉浮层挂在 body 下而不是 select 内部，且关闭后 DOM 仍保留（display:none），
        所以必须用 :visible 且取 .last，否则会点到上一次残留的浮层。
        返回实际选中的文本，供断言使用。
        """
        item = self._form_item(page, label)
        item.locator(".el-select, .el-input").first.click()
        page.wait_for_timeout(200)

        dropdown = page.locator(".el-select-dropdown:visible").last
        dropdown.wait_for(state="visible", timeout=5000)
        options = dropdown.locator(".el-select-dropdown__item:not(.is-disabled)")

        if option is not None:
            target = options.filter(has_text=option).first
        else:
            idx = index if index is not None else 0
            if options.count() <= idx:
                raise LookupError(f"{label} 只有 {options.count()} 个选项，取不到第 {idx} 个")
            target = options.nth(idx)

        text = target.inner_text().strip()
        target.click()
        page.wait_for_timeout(200)
        return text

    def list_options(self, page: Page, label: str) -> List[str]:
        """枚举下拉的所有选项，用于遍历筛选测试"""
        item = self._form_item(page, label)
        item.locator(".el-select, .el-input").first.click()
        page.wait_for_timeout(200)
        dropdown = page.locator(".el-select-dropdown:visible").last
        opts = dropdown.locator(".el-select-dropdown__item").all_inner_texts()
        page.keyboard.press("Escape")
        return [o.strip() for o in opts]

    def date_range(self, page: Page, label: str, start: str, end: str) -> None:
        """
        el-date-picker 的范围选择是一个容器里两个 input。
        直接 fill 再按 Enter 比点日历面板可靠得多。
        """
        item = self._form_item(page, label)
        picker = item.locator(".el-date-editor").first
        picker.click()
        page.wait_for_timeout(200)

        inputs = picker.locator("input")
        if inputs.count() >= 2:
            inputs.nth(0).fill(start)
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            inputs.nth(1).fill(end)
            page.keyboard.press("Enter")
        else:
            inputs.first.fill(f"{start} - {end}")
            page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        page.keyboard.press("Escape")

    # ---------- 表格 ----------
    def headers(self, page: Page, table: str = ".el-table") -> List[str]:
        """
        固定列会让 Element 渲染出多份 header（主表 + 左固定 + 右固定），
        只取主表的 header-wrapper，避免列名重复。
        """
        tbl = page.locator(table).first
        hdr = tbl.locator(".el-table__header-wrapper th .cell")
        texts = [t.strip() for t in hdr.all_inner_texts()]
        return [t for t in texts if t]

    def rows(self, page: Page, table: str = ".el-table") -> Locator:
        tbl = page.locator(table).first
        return tbl.locator(".el-table__body-wrapper tbody tr.el-table__row")

    def row_count(self, page: Page, table: str = ".el-table") -> int:
        return self.rows(page, table).count()

    def is_empty(self, page: Page, table: str = ".el-table") -> bool:
        tbl = page.locator(table).first
        return tbl.locator(".el-table__empty-block").is_visible()

    def column_index(self, page: Page, column: str, table: str = ".el-table") -> int:
        hs = self.headers(page, table)
        if column not in hs:
            raise LookupError(f"表头没有列 '{column}'，实际表头: {hs}")
        return hs.index(column)

    def column_values(self, page: Page, column: str, table: str = ".el-table") -> List[str]:
        """
        按列索引取值而不是按文本匹配单元格。
        列多时会横向滚动，但 DOM 里所有 td 都在，不需要滚动。
        """
        idx = self.column_index(page, column, table)
        rows = self.rows(page, table)
        n = rows.count()
        out = []
        for i in range(n):
            cells = rows.nth(i).locator("td")
            if cells.count() <= idx:
                out.append("")
                continue
            out.append(cells.nth(idx).inner_text().strip())
        return out

    def table_data(self, page: Page, table: str = ".el-table") -> List[dict]:
        """整张表抓成 list[dict]，用于和导出文件比对"""
        hs = self.headers(page, table)
        rows = self.rows(page, table)
        data = []
        for i in range(rows.count()):
            cells = rows.nth(i).locator("td")
            row = {}
            for j, h in enumerate(hs):
                row[h] = cells.nth(j).inner_text().strip() if j < cells.count() else ""
            data.append(row)
        return data

    # ---------- 分页 ----------
    def total_count(self, page: Page) -> Optional[int]:
        """从 '共 1234 条' 里抠出总数"""
        el = page.locator(".el-pagination__total")
        if el.count() == 0 or not el.first.is_visible():
            return None
        m = re.search(r"(\d+)", el.first.inner_text())
        return int(m.group(1)) if m else None

    def goto_page(self, page: Page, n: int) -> None:
        page.locator(f".el-pager li.number:text-is('{n}')").first.click()

    def next_page(self, page: Page) -> bool:
        btn = page.locator(".el-pagination button.btn-next").first
        if btn.is_disabled():
            return False
        btn.click()
        return True

    def set_page_size(self, page: Page, size: int) -> None:
        page.locator(".el-pagination .el-select").first.click()
        page.wait_for_timeout(200)
        page.locator(".el-select-dropdown:visible").last.locator(
            f".el-select-dropdown__item:has-text('{size}')"
        ).first.click()

    # ---------- 反馈 ----------
    def message_text(self, page: Page, timeout: int = 5000) -> str:
        """el-message 全局提示，新增/修改后的成功提示"""
        msg = page.locator(".el-message, .el-notification").last
        msg.wait_for(state="visible", timeout=timeout)
        return msg.inner_text().strip()

    def confirm_dialog(self, page: Page, ok: bool = True) -> None:
        box = page.locator(".el-message-box:visible").last
        box.wait_for(state="visible", timeout=5000)
        sel = ".el-button--primary" if ok else ".el-message-box__btns .el-button:not(.el-button--primary)"
        box.locator(sel).first.click()

    def dialog(self, page: Page) -> Locator:
        """当前可见的弹窗（新增/编辑表单通常在这里面）"""
        return page.locator(".el-dialog__wrapper:visible .el-dialog, .el-drawer:visible").last
