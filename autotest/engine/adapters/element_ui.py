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

        用 wait_for 而不是直接判 count()==0：count() 不会重试，用例一开始
        刚 goto 完，搜索表单可能还没渲染完，count() 立刻返回 0 就误判"找不到"，
        实际只是慢了一点。wait_for 会在超时前持续重试，页面快就立刻返回。
        """
        pattern = re.compile(rf"^\s*{re.escape(label)}\s*[:：]?\s*$")
        item = page.locator(".el-form-item").filter(
            has=page.locator(".el-form-item__label").filter(has_text=pattern)
        )
        try:
            item.first.wait_for(state="attached", timeout=6000)
            return item.first
        except Exception:
            pass
        # 有些页面不用 el-form-item，退化成找 label 的父容器
        xitem = page.locator(
            f"xpath=//*[contains(@class,'el-form-item')][.//label[normalize-space()='{label}']]"
        )
        try:
            xitem.first.wait_for(state="attached", timeout=3000)
            return xitem.first
        except Exception:
            raise LookupError(f"找不到表单项: {label}")

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
    # 一次 evaluate 把整张表读进来，避免 rows×cols 次 .nth().inner_text() 往返——
    # 这是执行期最大的性能热点：capture / 各列断言 / 导出比对全走这里。
    # 固定列会让 Element 多渲染一份表头和表体（主表 + 左/右固定），只取「第一个」
    # header-wrapper / body-wrapper（主表），既去掉重复列又和下标严格对齐。
    _READ_JS = r"""
    (el) => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      const hw = el.querySelector('.el-table__header-wrapper') || el;
      let hc = [...hw.querySelectorAll('th .cell')];
      if (!hc.length) hc = [...hw.querySelectorAll('thead th, th')];
      const headers = hc.map(c => clean(c.innerText));
      const bw = el.querySelector('.el-table__body-wrapper') || el;
      const rows = [...bw.querySelectorAll('tbody tr.el-table__row, tbody tr')]
        .map(tr => [...tr.querySelectorAll('td')].map(td => clean(td.innerText)));
      return {headers, rows};
    }
    """

    def _read(self, page: Page, table: str = ".el-table") -> dict:
        return page.locator(table).first.evaluate(self._READ_JS)

    def headers(self, page: Page, table: str = ".el-table") -> List[str]:
        return [t for t in self._read(page, table)["headers"] if t]

    def rows(self, page: Page, table: str = ".el-table") -> Locator:
        """行的 Locator，仅用于计数/翻页判断；取值请走 table_data/column_values。"""
        tbl = page.locator(table).first
        return tbl.locator(".el-table__body-wrapper").first.locator(
            "tbody tr.el-table__row")

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
        data = self._read(page, table)
        headers = [t for t in data["headers"] if t]
        if column not in headers:
            raise LookupError(f"表头没有列 '{column}'，实际表头: {headers}")
        # 空表头列会被过滤，导致过滤后的下标与 td 下标错位；用原始表头定位真实列位置
        idx = data["headers"].index(column)
        return [cells[idx] if idx < len(cells) else "" for cells in data["rows"]]

    def table_data(self, page: Page, table: str = ".el-table") -> List[dict]:
        """整张表抓成 list[dict]，用于和导出文件比对。一次 evaluate 读完。"""
        data = self._read(page, table)
        raw_headers = data["headers"]
        out = []
        for cells in data["rows"]:
            row = {}
            for j, h in enumerate(raw_headers):
                if h:  # 跳过复选框/展开等空表头列，但下标 j 仍对齐 td
                    row[h] = cells[j] if j < len(cells) else ""
            out.append(row)
        return out

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

    # ---------- 按钮巡检 ----------
    # 破坏性操作的关键词，巡检时只确认"存在且可点"，绝不真的点下去
    DESTRUCTIVE = ["删除", "移除", "停用", "禁用", "作废", "清空", "注销", "解绑",
                   "设为失效", "失效", "撤销", "驳回", "重置密码"]

    def toolbar_button_texts(self, page: Page) -> List[str]:
        """
        枚举页面工具栏上可见的操作按钮文本（去重、保持顺序）。
        刻意排除表格行内按钮、弹窗内按钮、分页器按钮——行内操作属于第二期，
        弹窗/分页按钮点了会产生噪声。用 closest 判断祖先容器，比 CSS 选择器可靠。
        """
        return page.evaluate(
            r"""() => {
              const inExcluded = el => !!el.closest(
                '.el-table, .el-dialog, .el-drawer, .el-pagination, .el-message-box');
              const seen = new Set(), out = [];
              document.querySelectorAll('button, a.el-button, .el-button').forEach(b => {
                const r = b.getBoundingClientRect();
                const st = getComputedStyle(b);
                if (r.width <= 0 || r.height <= 0 || st.visibility === 'hidden' || st.display === 'none') return;
                if (inExcluded(b)) return;
                const t = (b.innerText || '').replace(/\s+/g, '').trim();
                if (t && !seen.has(t)) { seen.add(t); out.push(t); }
              });
              return out;
            }"""
        )

    def button(self, page: Page, text: str) -> Locator:
        """按文本定位工具栏按钮（排除表格行内的同名按钮）。"""
        exact = re.compile(rf"^\s*{re.escape(text)}\s*$")
        return page.locator("button, a.el-button, .el-button").filter(has_text=exact).filter(
            has_not=page.locator("xpath=ancestor::*[contains(@class,'el-table')]")).first

    def dialog_visible(self, page: Page) -> bool:
        for sel in (".el-dialog:visible", ".el-drawer:visible", ".el-message-box:visible"):
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def close_dialog(self, page: Page) -> None:
        """尽量关掉当前可见的弹窗/抽屉/确认框：优先取消/关闭按钮，兜底按 Esc。"""
        for sel in (".el-dialog:visible .el-dialog__headerbtn",
                    ".el-drawer:visible .el-drawer__close-btn",
                    ".el-message-box:visible .el-button:not(.el-button--primary)",
                    ".el-dialog:visible .el-button:has-text('取消')",
                    ".el-dialog:visible .el-button:has-text('关闭')"):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=2000)
                    page.wait_for_timeout(300)
                    if not self.dialog_visible(page):
                        return
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
