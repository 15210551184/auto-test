"""
Element UI 适配层。

后台管理系统的 DOM 有固定套路，把这些套路封装在这里，
上层的动作执行器就不用关心 .el-form-item__label 之类的细节。
换成 Ant Design 只要再写一个同接口的适配器。
"""
import re
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page, Locator

from ..i18n_terms import words as _i18n_words


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

        级联选择器（比如「城市」依赖「国家」先选）没选父级时是 disabled 的，
        点击给个短超时——不然 Playwright 会一直重试等它变可点，默认能等 30s。
        """
        item = self._form_item(page, label)
        item.locator(".el-select, .el-input").first.click(timeout=5000)
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
        item.locator(".el-select, .el-input").first.click(timeout=5000)
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

    def set_field_value(self, page: Page, field: Dict[str, Any], value: Any) -> None:
        """
        按扫描出来的字段类型把值填进去。CRUD 闭环验证的统一填表入口——
        fill_form 只会无脑往 input 里塞文本，select/radio/switch 这些非文本
        控件光靠"点输入框打字"填不对，必须按类型分派。
        """
        label, ftype = field["label"], field.get("type", "text")
        if ftype in ("text", "textarea", "number"):
            self.fill(page, label, str(value))
        elif ftype in ("select", "radio"):
            self.select(page, label, option=str(value))
        elif ftype == "checkbox":
            item = self._form_item(page, label)
            opts = value if isinstance(value, list) else [value]
            for opt in opts:
                item.locator(".el-checkbox").filter(has_text=str(opt)).first.click(timeout=3000)
        elif ftype == "date":
            item = self._form_item(page, label)
            inp = item.locator(".el-date-editor input").first
            inp.fill(str(value))
            page.keyboard.press("Enter")
        elif ftype == "date_range":
            start, end = value
            self.date_range(page, label, start, end)
        elif ftype == "switch":
            item = self._form_item(page, label)
            sw = item.locator(".el-switch").first
            cls = sw.get_attribute("class") or ""
            if bool(value) != ("is-checked" in cls):
                sw.click(timeout=3000)
        else:
            raise LookupError(f"字段类型 '{ftype}' 暂不支持自动填写: {label}")

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

    def find_row_by(self, page: Page, column: str, value: str,
                    table: str = ".el-table") -> int:
        """
        按某列的值定位行号，CRUD 闭环验证用它找到"自己刚建的那条记录"。
        用 contains 而不是精确相等——列宽不够时页面会截断显示（"用户Bcl..."），
        精确匹配会把自己刚建的数据也找不到。
        找不到、或者匹配上不止一行（说明搜索条件不够精确）都报错，
        不能瞎猜一行就去改/删——那可能是别人的真实数据。
        """
        rows = self.table_data(page, table)
        hits = [i for i, r in enumerate(rows) if value in str(r.get(column, ""))]
        if not hits:
            raise LookupError(f"列 '{column}' 里没有找到包含 '{value}' 的行，"
                              f"当前 {len(rows)} 行")
        if len(hits) > 1:
            raise LookupError(f"列 '{column}' 里有 {len(hits)} 行都包含 '{value}'，"
                              f"定位不到唯一记录，不敢继续操作")
        return hits[0]

    # 状态切换类按钮的常见文案。和 DESTRUCTIVE 不完全重叠——"设为生效"「启用」
    # 「解冻」这些是把数据从"失效"切回"生效"，对巡检来说没有破坏性，但状态
    # 流转验证需要知道这些文案，好在自己创建的测试记录上点它、验证状态真的变了。
    STATUS_TOGGLE = ["设为失效", "设为生效", "停用", "启用", "禁用", "冻结", "解冻",
                     "上架", "下架"]

    def find_row_toggle_text(self, page: Page, row: int,
                             table: str = ".el-table") -> Optional[str]:
        """
        找这一行操作列里状态切换按钮的具体文案。不同系统措辞不一样（"设为失效"
        还是"停用"），运行时现场探测，配置里不需要写死具体文案。
        """
        rows = self.rows(page, table)
        if rows.count() <= row:
            return None
        tr = rows.nth(row)
        for kw in self.STATUS_TOGGLE:
            try:
                el = tr.locator(
                    f"button:has-text('{kw}'), a:has-text('{kw}'), "
                    f"span:has-text('{kw}'), .el-link:has-text('{kw}')").first
                if el.count() and el.is_visible():
                    return kw
            except Exception:
                continue
        return None

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

    # ---------- CRUD 验证支撑 ----------
    def row_action(self, page: Page, row: int, action: str,
                   table: str = ".el-table") -> None:
        """
        点某一行的操作按钮（查看/编辑/删除）。
        操作列可能直接放按钮，也可能收在「…」下拉里（行窄时常见），两种都试。
        """
        rows = self.rows(page, table)
        if rows.count() <= row:
            raise LookupError(f"表格只有 {rows.count()} 行，取不到第 {row + 1} 行")
        tr = rows.nth(row)

        direct = tr.locator(
            f"button:has-text('{action}'), a:has-text('{action}'), "
            f"span:has-text('{action}'), .el-link:has-text('{action}')")
        for i in range(direct.count()):
            try:
                el = direct.nth(i)
                if el.is_visible():
                    el.click(timeout=3000)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                continue

        # 收在「…」更多菜单里：点开后浮层挂在 body 下，不在行内
        more = tr.locator(".el-dropdown, .el-icon-more, button:has-text('…'), "
                          "button:has-text('...')").first
        if more.count():
            try:
                more.click(timeout=3000)
                page.wait_for_timeout(400)
                menu = page.locator(".el-dropdown-menu:visible").last
                menu.locator(f".el-dropdown-menu__item:has-text('{action}')") \
                    .first.click(timeout=3000)
                page.wait_for_timeout(600)
                return
            except Exception:
                pass
        raise LookupError(f"第 {row + 1} 行找不到「{action}」操作")

    def dialog_field_values(self, page: Page) -> Dict[str, str]:
        """
        读弹窗里每个表单项当前的值，用于验证编辑回显。
        input 取 value，下拉也是渲染成 input 的，所以统一读 input；
        读不到就退回读文本（radio/switch 这类）。
        """
        dlg = self.dialog(page)
        out: Dict[str, str] = {}
        items = dlg.locator(".el-form-item")
        for i in range(items.count()):
            item = items.nth(i)
            try:
                label = item.locator(".el-form-item__label").first.inner_text()
            except Exception:
                continue
            label = label.strip().rstrip(":：").strip().lstrip("*").strip()
            if not label:
                continue
            val = ""
            try:
                inp = item.locator("input, textarea").first
                if inp.count():
                    val = inp.input_value()
            except Exception:
                pass
            if not val:
                try:
                    content = item.locator(".el-form-item__content").first
                    if content.count():
                        val = re.sub(r"\s+", " ", content.inner_text()).strip()
                except Exception:
                    pass
            out[label] = val
        return out

    def detail_values(self, page: Page) -> Dict[str, str]:
        """
        读详情弹窗的「字段：值」。详情不一定用 el-form-item 渲染（常见是
        el-descriptions，或者干脆一堆 div），所以按「冒号分隔」兜底解析文本。
        """
        dlg = self.dialog(page)
        out: Dict[str, str] = {}

        # 1. el-descriptions 这类结构化的，直接按 label/content 配对
        rows = dlg.locator(".el-descriptions-item")
        for i in range(rows.count()):
            it = rows.nth(i)
            try:
                k = it.locator(".el-descriptions-item__label").first.inner_text().strip()
                v = it.locator(".el-descriptions-item__content").first.inner_text().strip()
                if k:
                    out[k.rstrip(":：").strip()] = v
            except Exception:
                continue
        if out:
            return out

        # 2. 兜底：整块文本按行拆，取「xxx：yyy」形式的行
        try:
            text = dlg.inner_text()
        except Exception:
            return out
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"^(.{1,20}?)\s*[:：]\s*(.*)$", line)
            if m:
                k, v = m.group(1).strip(), m.group(2).strip()
                if k:
                    out[k] = v
        return out

    def form_error_labels(self, page: Page) -> List[str]:
        """
        当前弹窗里哪些表单项报了校验错误（用于必填校验断言）。
        返回报错项的 label 列表。
        """
        dlg = self.dialog(page)
        out = []
        items = dlg.locator(".el-form-item.is-error")
        for i in range(items.count()):
            try:
                lb = items.nth(i).locator(".el-form-item__label").first.inner_text()
                lb = lb.strip().rstrip(":：").strip().lstrip("*").strip()
                if lb:
                    out.append(lb)
            except Exception:
                continue
        return out

    def form_error_texts(self, page: Page) -> List[str]:
        dlg = self.dialog(page)
        try:
            return [t.strip() for t in
                    dlg.locator(".el-form-item__error").all_inner_texts() if t.strip()]
        except Exception:
            return []

    # ---------- 按钮巡检 ----------
    # 破坏性操作的关键词，巡检时只确认"存在且可点"，绝不真的点下去。
    # 中英文都要覆盖——纯中文名单在英文界面下识别不出 "Delete"，
    # 会导致巡检真的把危险按钮点下去，这是安全问题不是功能缺失。
    DESTRUCTIVE = _i18n_words("delete", "disable") + [
        "作废", "Void", "清空", "Clear", "注销", "Unregister",
        "解绑", "Unbind", "撤销", "Revoke", "驳回", "Reject",
        "重置密码", "Reset Password",
    ]

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
