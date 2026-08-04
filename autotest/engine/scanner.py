"""
页面扫描器 —— "给个地址就能跑" 的关键。

打开页面，识别搜索表单、表格、按钮、列表接口，
自动生成一份可执行的配置草稿。人工过一遍补业务断言即可。

刻意不做全自动断言：机器能判断"搜索后列值是否匹配"这种结构性规则，
但判断不了"订单金额应该等于实付+抵扣"这种业务规则。
自动生成骨架 + 人工补业务断言，是投入产出比最高的分工。
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml
from playwright.sync_api import sync_playwright, Page
from . import browser as B
from .adapters.element_ui import ElementUIAdapter
from .i18n_terms import words as _i18n_words
from .language_switch import default_language_code, switch_page_language
from .login import ensure_logged_in
from .state import save_storage_state, valid_storage_state


FORM_OPTION_PROBE_BUDGET_MS = 5000
FORM_OPTION_CLICK_TIMEOUT_MS = 700


def _wait_for_scan_ready(page: Page, max_wait_ms: int = 3000) -> int:
    """
    等列表真正可扫描，而不是无条件睡满 3 秒。

    ``domcontentloaded`` 对 Vue/React 后台页远远不够：组件和列表接口通常还
    在后面异步渲染；但固定等待又让本来 300ms 就完成的页面每次白等。这里
    等到“表头已经出现，且已有数据行或明确显示空状态”，其次接受已经渲染
    出搜索表单的页面。到时仍没信号才走满原来的等待预算，准确率不倒退。
    """
    started = time.monotonic()
    timeout = max(100, int(max_wait_ms))
    try:
        page.wait_for_function(
            """() => {
                const visible = el => {
                    if (!el) return false;
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden'
                        && r.width > 0 && r.height > 0;
                };
                const tables = [...document.querySelectorAll(
                    '.el-table, .ant-table, table.data-table, table')].filter(visible);
                const tableReady = tables.some(t => {
                    const headers = t.querySelectorAll(
                        '.el-table__header-wrapper th, .ant-table-thead th, thead th');
                    const rows = t.querySelectorAll(
                        '.el-table__body-wrapper tr.el-table__row, .ant-table-tbody tr, tbody tr');
                    const empty = t.querySelector(
                        '.el-table__empty-block, .ant-empty, .el-empty');
                    const loading = [...t.querySelectorAll(
                        '.el-loading-mask, .ant-spin-spinning')].some(visible);
                    return !loading && headers.length > 0
                        && (rows.length > 0 || visible(empty));
                });
                return tableReady;
            }""",
            timeout=timeout,
            polling=100,
        )
        # 就绪信号出现后给同一轮 Vue patch/表格布局一个很短的收尾窗口。
        page.wait_for_timeout(150)
    except Exception:
        # wait_for_function 已经消耗完整预算，不再额外固定等待一次。
        pass
    return int((time.monotonic() - started) * 1000)


class PageScanner:
    def __init__(self, page: Page):
        self.page = page
        self.api_calls: List[str] = []
        self._api_bodies: Dict[str, str] = {}   # 只存"像列表接口"的候选，避免每个响应体都存
        self._ui = ElementUIAdapter()   # 只借用它关弹窗，不复用别的运行期逻辑
        page.on("response", self._on_resp)
        page.on("requestfinished", self._on_request_finished)

    def _on_resp(self, resp):
        u = resp.url
        if resp.status == 200 and re.search(r"/(api|web)/", u) and "static" not in u:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" in ct:
                self.api_calls.append(u)

    def _on_request_finished(self, request):
        """
        response 事件只代表响应头到达，直接 resp.text() 会等待正文结束；遇到
        流式/异常接口时会把整个同步 Playwright 线程卡死。requestfinished
        保证正文已经接收完成，再为列表候选保存小段样本用于接口匹配。
        """
        try:
            resp = request.response()
            if not resp:
                return
            u = resp.url
            ct = (resp.headers or {}).get("content-type", "")
            if (resp.status == 200 and "json" in ct
                    and re.search(r"(list|page|query|search|find)", u, re.I)):
                self._api_bodies[u] = resp.text()[:8000]
        except Exception:
            pass

    # ---------- 表单识别 ----------
    def scan_form(self) -> List[Dict[str, Any]]:
        # 原实现对每个 form-item 分别 count()/inner_text()/get_attribute()，并逐
        # 个点开下拉。一个 10 个筛选项的页面会产生上百次浏览器往返；空下拉
        # 再进入级联回溯后，整页稳定耗时 30~35 秒。这里一次 evaluate_all()
        # 把标签、类型、placeholder 以及已挂载 popper 的选项一起取回。
        items = self.page.locator(".el-form-item")
        try:
            fields = items.evaluate_all(
                """items => items.map((item, index) => {
                    const clean = s => (s || '').trim()
                        .replace(/[：:]\\s*$/, '').trim();
                    const label = clean(item.querySelector(
                        '.el-form-item__label')?.textContent);
                    if (!label) return null;
                    const date = item.querySelector('.el-date-editor');
                    const select = item.querySelector('.el-select');
                    const input = item.querySelector('.el-input__inner');
                    if (date) {
                        return {label, type: item.querySelector('.el-range-input')
                            ? 'date_range' : 'date', _index: index};
                    }
                    if (select) {
                        const control = select.querySelector('input');
                        const placeholder =
                            control?.getAttribute('placeholder') || '';
                        const disabled = select.classList.contains('is-disabled')
                            || control?.disabled
                            || control?.getAttribute('aria-disabled') === 'true';
                        const ids = [
                            control?.getAttribute('aria-controls'),
                            control?.getAttribute('aria-owns')
                        ].filter(Boolean).flatMap(v => v.split(/\\s+/));
                        const options = [];
                        [...new Set(ids)].forEach(id => {
                            const popper = document.getElementById(id);
                            popper?.querySelectorAll(
                                '.el-select-dropdown__item'
                            ).forEach(o => {
                                const text = clean(o.textContent);
                                if (text && !options.includes(text)) options.push(text);
                            });
                        });
                        return {
                            label, type: 'select', options: options.slice(0, 15),
                            placeholder,
                            _index: index,
                            _disabled: disabled,
                            // Element UI 的 filterable/remote 下拉会开放 input
                            // 输入；但部分旧版/二次封装组件即使支持输入也始终
                            // 保留 readonly，此时用“请输入/搜索”等 placeholder
                            // 识别。普通下拉通常是“请选择”，不会误命中。
                            searchable: !!control && !disabled && (
                                !control.readOnly ||
                                /(请输入|输入|搜索|search|enter|type)/i.test(
                                    placeholder)
                            )
                        };
                    }
                    if (input) {
                        return {label, type: 'text',
                            placeholder: input.getAttribute('placeholder') || '',
                            _index: index};
                    }
                    return null;
                }).filter(Boolean)"""
            )
        except Exception:
            fields = []

        # 只有静态 DOM 里尚无选项的、可交互下拉才点开探测；所有下拉共享
        # 一个整页预算，页面再复杂也不会把这一阶段拖到几十秒。
        deadline = time.monotonic() + FORM_OPTION_PROBE_BUDGET_MS / 1000
        for field in fields:
            if (field["type"] != "select" or field.get("options")
                    or field.get("_disabled")):
                continue
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            item = items.nth(field["_index"])
            field["options"] = self._peek_options(
                item, min(FORM_OPTION_CLICK_TIMEOUT_MS, remaining_ms))

        self._resolve_cascading_selects(fields, deadline=deadline)
        for field in fields:
            field.pop("_index", None)
            field.pop("_disabled", None)
        return fields

    def scan_form_labels(self) -> List[str]:
        """
        跟 scan_form() 完全一样的过滤条件（同一个 item 判定是否算一个字段），
        只取 label 文案，不探测类型/选项——给多语言合并用，不产生"点开
        下拉""级联试选"这些副作用，也不会因为副作用打乱页面状态。
        """
        items = self.page.locator(".el-form-item")
        try:
            return items.evaluate_all(
                """items => items.map(item => {
                    const label = (item.querySelector(
                        '.el-form-item__label')?.textContent || '').trim()
                        .replace(/[：:]\\s*$/, '').trim();
                    const hasField = item.querySelector(
                        '.el-date-editor, .el-select, .el-input__inner');
                    return label && hasField ? label : null;
                }).filter(Boolean)"""
            )
        except Exception:
            return []

    def _form_item_by_label(self, label: str):
        items = self.page.locator(".el-form-item")
        for i in range(items.count()):
            item = items.nth(i)
            try:
                lb = item.locator(".el-form-item__label").first.inner_text().strip()
            except Exception:
                continue
            if lb.rstrip(":：").strip() == label:
                return item
        return None

    def _resolve_cascading_selects(
            self, fields: List[Dict[str, Any]],
            deadline: Optional[float] = None) -> None:
        """
        级联下拉（如"城市"依赖"国家"先选）第一遍扫描是空的——不是没选项，
        是控件在父级没选之前根本不可交互，_peek_options 点不开只能拿到 []。

        这里依样画葫芦：对每个选项为空的下拉，依次尝试先选前面某个下拉的
        第一个真实选项（排除"全部/请选择"这类占位项），等一下让级联接口
        返回，再重新探测一次。测出来了就把"先选谁、选的什么"记进
        depends_on，供 to_config() 在生成用例时补上这一步——不然测的时候
        一样会点在一个 disabled 元素上。
        只试"排在它前面的下拉"，不乱试后面的字段（表单里父级选择器通常在
        依赖它的子级前面，这个假设足够覆盖常见情况，不做多级级联的穷举）。
        """
        placeholder = {"全部", "请选择", "不限"}
        for i, f in enumerate(fields):
            if f["type"] != "select" or f.get("options"):
                continue
            if deadline is not None and time.monotonic() >= deadline:
                break
            # 级联父级通常紧挨在子级前面（国家→城市→区域）。从最近的字段
            # 往前试，常见情况一次命中；原来从表单第一个下拉开始试，字段
            # 多时会先做许多无效选择和 500ms 等待。
            for parent in reversed(fields[:i]):
                if parent["type"] != "select" or not parent.get("options"):
                    continue
                candidates = [o for o in parent["options"] if o not in placeholder]
                if not candidates:
                    continue
                test_option = candidates[0]
                try:
                    remaining_ms = (int((deadline - time.monotonic()) * 1000)
                                    if deadline is not None else 3000)
                    if remaining_ms <= 0:
                        return
                    self._select_option(
                        parent["label"], test_option,
                        timeout_ms=min(FORM_OPTION_CLICK_TIMEOUT_MS, remaining_ms),
                        item_index=parent.get("_index"))
                except Exception:
                    continue
                remaining_ms = (int((deadline - time.monotonic()) * 1000)
                                if deadline is not None else 500)
                if remaining_ms <= 0:
                    return
                self.page.wait_for_timeout(min(300, remaining_ms))
                item = (self.page.locator(".el-form-item").nth(f["_index"])
                        if f.get("_index") is not None
                        else self._form_item_by_label(f["label"]))
                remaining_ms = (int((deadline - time.monotonic()) * 1000)
                                if deadline is not None else
                                FORM_OPTION_CLICK_TIMEOUT_MS)
                retry = (self._peek_options(
                    item, min(FORM_OPTION_CLICK_TIMEOUT_MS, remaining_ms))
                    if item is not None and remaining_ms > 0 else [])
                if retry:
                    f["options"] = retry
                    f["depends_on"] = {"label": parent["label"], "option": test_option}
                    break

    def _select_option(
            self, label: str, option: str,
            timeout_ms: int = 3000,
            item_index: Optional[int] = None) -> None:
        """扫描阶段专用的选择：点开 label 对应的下拉，选中指定文本的选项。"""
        item = (self.page.locator(".el-form-item").nth(item_index)
                if item_index is not None else self._form_item_by_label(label))
        if item is None:
            raise LookupError(label)
        timeout_ms = max(100, int(timeout_ms))
        item.locator(".el-select").first.click(timeout=timeout_ms)
        self.page.wait_for_timeout(min(100, timeout_ms))
        dd = self.page.locator(".el-select-dropdown:visible").last
        dd.locator(".el-select-dropdown__item").filter(
            has_text=option).first.click(timeout=timeout_ms)
        self.page.wait_for_timeout(min(100, timeout_ms))

    def _peek_options(
            self, item,
            timeout_ms: int = FORM_OPTION_CLICK_TIMEOUT_MS) -> List[str]:
        """
        点开一个下拉看看有什么选项。级联选择器（比如「城市」依赖「国家」先选）
        在没选父级之前是 disabled 的——Playwright 点一个 disabled 元素会一直
        重试等它变成可点，默认要等 30s。一个表单里有几个这种级联下拉，扫描
        单页就可能被拖到 150s 超时上限。这里给点击一个短超时，点不动就当没有
        选项处理（本来也生成不出可用的筛选用例），别在一个下拉上死等半分钟。
        """
        try:
            select = item.locator(".el-select").first

            # Element UI/Plus 通常已把下拉项渲染到 body 的 popper 中，只是隐藏
            # 起来；input 的 aria-controls/aria-owns 能直接定位它。直接读取可
            # 省掉每个下拉 300ms 展开 + 150ms 收起的固定等待。
            control = select.locator("input").first
            control.wait_for(state="attached", timeout=min(250, timeout_ms))
            cls = select.get_attribute("class") or ""
            disabled = control.get_attribute("disabled")
            aria_disabled = control.get_attribute("aria-disabled")
            if "is-disabled" in cls or disabled is not None or aria_disabled == "true":
                return []

            popper_ids = ((control.get_attribute("aria-controls") or "") + " "
                          + (control.get_attribute("aria-owns") or "")).split()
            for popper_id in dict.fromkeys(popper_ids):
                safe_id = popper_id.replace("\\", "\\\\").replace('"', '\\"')
                opts = self.page.locator(
                    f'[id="{safe_id}"] .el-select-dropdown__item'
                ).evaluate_all(
                    "els => els.map(e => (e.textContent || '').trim()).filter(Boolean)")
                if opts:
                    return opts[:15]

            # disabled 控件让 Playwright 重试到 timeout 才失败；级联页面里多个
            # disabled 下拉会各白等 3 秒。明确禁用就立即交给级联解析器处理。
            select.click(timeout=timeout_ms)
            self.page.wait_for_timeout(min(100, timeout_ms))
            dd = self.page.locator(".el-select-dropdown:visible").last
            dd.wait_for(state="visible", timeout=timeout_ms)
            opts = dd.locator(".el-select-dropdown__item").evaluate_all(
                "els => els.map(e => (e.textContent || '').trim()).filter(Boolean)")
            self.page.keyboard.press("Escape")
            return opts[:15]
        except Exception:
            return []

    # ---------- 新增/编辑表单结构识别 ----------
    # 这是 CRUD 验证的总开关。字段名、是否必填、长度上限、下拉选项，DOM 里
    # 全是现成的（必填 = .el-form-item.is-required，长度 = input 的 maxlength），
    # 读出来就能自动生成必填校验、边界校验、填表闭环验证，不需要人工配业务规则。

    def _creation_trigger(self):
        """
        按钮文案跟 i18n_terms 的 "create" 词表比对，而不是死认中文"新增"——
        扫描阶段页面语言不一定是中文（比如项目默认英文界面），死认字面量会
        导致 scan_buttons() 判断有新增按钮，但这里点不开弹窗。
        """
        words = _i18n_words("create")
        sel = ", ".join(f"button:has-text('{w}'), a:has-text('{w}')" for w in words)
        return self.page.locator(sel).first

    def scan_form_schema(self) -> Dict[str, Any]:
        """点开新增弹窗扫字段结构，扫完关掉。点不开就返回空，不影响其他扫描。"""
        btn = self._creation_trigger()
        try:
            btn.wait_for(state="attached", timeout=700)
            btn.click(timeout=1500)
        except Exception:
            return {}

        dialog = self.page.locator(
            ".el-dialog__wrapper:visible .el-dialog, .el-drawer:visible").last
        try:
            dialog.wait_for(state="visible", timeout=2500)
        except Exception:
            return {}
        self.page.wait_for_timeout(300)   # 等弹窗里的下拉/字典数据加载完

        title = ""
        try:
            t = dialog.locator(".el-dialog__title, .el-drawer__title").first
            title = t.inner_text(timeout=500).strip()
        except Exception:
            pass

        field_positions = None
        try:
            field_positions = dialog.locator(".el-form-item").evaluate_all(
                """items => items.map((item, index) => {
                    const label = (item.querySelector('.el-form-item__label')
                        ?.textContent || '').trim();
                    return label ? index : null;
                }).filter(index => index !== null)"""
            )
        except Exception:
            pass
        try:
            fields = self._scan_dialog_fields(dialog)
        finally:
            # 扫失败也要关掉，否则挡住后面的扫描；复用适配器的关弹窗逻辑，
            # 不在这里维护第二份（这里和运行期用的是同一套 Element UI 约定）
            self._ui.close_dialog(self.page)
        if not fields:
            return {}
        result = {"title": title, "fields": fields}
        if field_positions is not None:
            result["field_positions"] = field_positions
        return result

    def scan_dialog_labels(self, field_positions: Optional[List[int]] = None) -> List[str]:
        """
        跟 scan_form_schema 走同样的开关弹窗流程，但只取 label 文案、不做
        类型/选项探测——给多语言合并用，过滤条件和 _scan_dialog_fields
        完全一致，保证下标能对齐。
        """
        btn = self._creation_trigger()
        try:
            btn.wait_for(state="attached", timeout=700)
            btn.click(timeout=1500)
        except Exception:
            return []
        dialog = self.page.locator(
            ".el-dialog__wrapper:visible .el-dialog, .el-drawer:visible").last
        try:
            dialog.wait_for(state="visible", timeout=2500)
        except Exception:
            return []
        self.page.wait_for_timeout(300)
        try:
            if field_positions is not None:
                # 按默认语言记录的 DOM 下标读取。译文为空时保留占位，不能
                # filter(Boolean)，否则后面的字段会整体左移或整组被丢弃。
                labels = dialog.locator(".el-form-item").evaluate_all(
                    """(items, positions) => positions.map(index => {
                        const item = items[index];
                        return (item?.querySelector('.el-form-item__label')
                            ?.textContent || '').trim().replace(/[：:]\\s*$/, '')
                            .trim().replace(/^\\*+/, '').trim();
                    })""", field_positions)
            else:
                labels = dialog.locator(".el-form-item").evaluate_all(
                    """items => items.map(item => (
                        item.querySelector('.el-form-item__label')?.textContent || ''
                    ).trim().replace(/[：:]\\s*$/, '').trim()
                      .replace(/^\\*+/, '').trim()).filter(Boolean)"""
                )
        except Exception:
            labels = []
        finally:
            self._ui.close_dialog(self.page)
        return labels

    def _scan_dialog_fields(self, dialog) -> List[Dict[str, Any]]:
        items = dialog.locator(".el-form-item")
        try:
            return items.evaluate_all(
                """items => items.map(item => {
                    const texts = (selector) => [...item.querySelectorAll(selector)]
                        .map(e => (e.textContent || '').trim()).filter(Boolean)
                        .slice(0, 15);
                    const label = (
                        item.querySelector('.el-form-item__label')?.textContent || ''
                    ).trim().replace(/[：:]\\s*$/, '').trim()
                     .replace(/^\\*+/, '').trim();
                    if (!label) return null;
                    const base = {label,
                        required: item.classList.contains('is-required')};
                    if (item.querySelector('.el-upload'))
                        return {...base, type: 'upload', fillable: false};
                    if (item.querySelector('.el-switch'))
                        return {...base, type: 'switch'};
                    if (item.querySelector('.el-radio-group, .el-radio'))
                        return {...base, type: 'radio',
                            options: texts('.el-radio')};
                    if (item.querySelector('.el-checkbox-group'))
                        return {...base, type: 'checkbox',
                            options: texts('.el-checkbox')};
                    if (item.querySelector('.el-date-editor'))
                        return {...base, type: item.querySelector('.el-range-input')
                            ? 'date_range' : 'date'};
                    if (item.querySelector('.el-cascader'))
                        return {...base, type: 'cascader', fillable: false};
                    if (item.querySelector('.el-select')) {
                        const control = item.querySelector('.el-select input');
                        const ids = [
                            control?.getAttribute('aria-controls'),
                            control?.getAttribute('aria-owns')
                        ].filter(Boolean).flatMap(v => v.split(/\\s+/));
                        const options = [];
                        [...new Set(ids)].forEach(id => document.getElementById(id)
                            ?.querySelectorAll('.el-select-dropdown__item')
                            .forEach(o => {
                                const text = (o.textContent || '').trim();
                                if (text && !options.includes(text)) options.push(text);
                            }));
                        return {...base, type: 'select',
                            options: options.slice(0, 15)};
                    }
                    if (item.querySelector('.el-input-number'))
                        return {...base, type: 'number'};
                    const textarea = item.querySelector('textarea');
                    if (textarea) return {...base, type: 'textarea',
                        maxlength: Number(textarea.maxLength) > 0
                            ? Number(textarea.maxLength) : null};
                    const input = item.querySelector('input');
                    if (input) return {...base, type: 'text',
                        maxlength: Number(input.maxLength) > 0
                            ? Number(input.maxLength) : null};
                    return {...base, type: 'unknown', fillable: false};
                }).filter(Boolean)"""
            )
        except Exception:
            return []

    def _field_control(self, item) -> Dict[str, Any]:
        """判断控件类型，顺手读出长度上限/选项这些约束。"""
        def has(sel):
            try:
                return item.locator(sel).count() > 0
            except Exception:
                return False

        # 顺序有讲究：upload/switch 这些内部也可能套 input，必须先判
        if has(".el-upload"):
            return {"type": "upload", "fillable": False}
        if has(".el-switch"):
            return {"type": "switch"}
        if has(".el-radio-group, .el-radio"):
            return {"type": "radio", "options": self._choice_texts(item, ".el-radio")}
        if has(".el-checkbox-group"):
            return {"type": "checkbox", "options": self._choice_texts(item, ".el-checkbox")}
        if has(".el-date-editor"):
            return {"type": "date_range" if has(".el-range-input") else "date"}
        if has(".el-cascader"):
            # 级联选择器（省/市/区那种）层级不定，自动填容易填错，交给人工
            return {"type": "cascader", "fillable": False}
        if has(".el-select"):
            return {"type": "select", "options": self._peek_options(item)}
        if has(".el-input-number"):
            return {"type": "number"}
        if has("textarea"):
            return {"type": "textarea", "maxlength": self._maxlength(item, "textarea")}
        if has("input"):
            return {"type": "text", "maxlength": self._maxlength(item, "input")}
        return {"type": "unknown", "fillable": False}

    @staticmethod
    def _maxlength(item, sel: str) -> Optional[int]:
        try:
            v = item.locator(sel).first.get_attribute("maxlength")
            return int(v) if v and v.isdigit() else None
        except Exception:
            return None

    @staticmethod
    def _choice_texts(item, sel: str) -> List[str]:
        try:
            return [t.strip() for t in item.locator(sel).all_inner_texts() if t.strip()][:15]
        except Exception:
            return []

    # ---------- 表格识别 ----------
    def _locate_table(self):
        """挑出页面上真正的数据表格（可见、表头非空）。找不到就是 None。"""
        tables = self.page.locator(".el-table, .ant-table, table.data-table, table")
        for i in range(tables.count()):
            candidate = tables.nth(i)
            try:
                headers = candidate.locator(
                    ".el-table__header-wrapper th .cell, .ant-table-thead th, thead th").all_inner_texts()
                if candidate.is_visible() and any(h.strip() for h in headers):
                    return candidate
            except Exception:
                continue
        return None

    def _table_snapshot(self) -> Dict[str, Any]:
        """一次浏览器往返读取真实表格，避免 count()/all_inner_texts() 无界等待。"""
        try:
            snapshots = self.page.locator(
                ".el-table, .ant-table, table.data-table, table"
            ).evaluate_all(
                """tables => {
                    const visible = el => {
                        if (!el) return false;
                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                        return s.display !== 'none' && s.visibility !== 'hidden'
                            && r.width > 0 && r.height > 0;
                    };
                    for (const table of tables) {
                        if (!visible(table)) continue;
                        const head = table.querySelector(
                            '.el-table__header-wrapper, .ant-table-thead, thead');
                        let headerEls = head
                            ? [...head.querySelectorAll('th .cell')] : [];
                        if (!headerEls.length && head)
                            headerEls = [...head.querySelectorAll('th')];
                        const headers = headerEls.map(
                            e => (e.textContent || '').trim()).filter(Boolean);
                        if (!headers.length) continue;
                        const body = table.querySelector(
                            '.el-table__body-wrapper, .ant-table-tbody, tbody');
                        const rows = body
                            ? [...body.querySelectorAll('tr.el-table__row, tr')] : [];
                        const cells = rows.length
                            ? [...rows[0].querySelectorAll('td')].map(
                                e => (e.textContent || '').trim().slice(0, 40))
                            : [];
                        return [{headers, row_count: rows.length, cells}];
                    }
                    return [];
                }"""
            )
            return snapshots[0] if snapshots else {}
        except Exception:
            return {}

    def scan_table_headers(self) -> List[str]:
        """只取表头文案，用于多语言合并——跟 scan_table() 用同一套表格定位/
        表头容器逻辑，保证下标能和默认语言那次扫描的 headers 对齐。"""
        return _unique(self._table_snapshot().get("headers", []))

    def scan_table(self) -> Dict[str, Any]:
        snapshot = self._table_snapshot()
        raw_headers = snapshot.get("headers") or []
        if not raw_headers:
            return {}

        # 冻结列（左固定/右固定）会让 Element/Antd 把表头和表体各多渲染一份，
        # 表头、表体必须各自只取「第一个」容器，按下标才能严格一一对应——
        # 之前不限定容器时，两份表头会先被 _unique() 去重掉，但单元格取值
        # 仍按原始（含重复）下标去对，导致取样值整体错位，殃及后面所有列：
        # 搜索用例的种子值、列类型猜测全部跟着错。
        headers = _unique(raw_headers)
        cells = snapshot.get("cells") or []
        sample = {
            raw_headers[j]: cells[j]
            for j in range(min(len(raw_headers), len(cells)))
            if raw_headers[j] and raw_headers[j] not in raw_headers[:j]
        }

        return {
            "headers": headers,
            "row_count": snapshot.get("row_count", 0),
            "column_types": {h: self._guess_type(v) for h, v in sample.items()},
            "sample_row": sample,
        }

    @staticmethod
    def _guess_type(v: str) -> str:
        if not v or v in ("-", "--"):
            return "unknown"
        if "¥" in v or "￥" in v:
            return "money"
        if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", v):
            return "date"
        if re.fullmatch(r"1[3-9]\d{9}", v):
            return "phone"
        if re.fullmatch(r"[\d,]+\.?\d*", v):
            return "number"
        return "text"

    # ---------- 按钮 / 分页 ----------
    def scan_buttons(self) -> Dict[str, bool]:
        terms = {
            "search": _i18n_words("search"),
            "reset": _i18n_words("reset"),
            "export": _i18n_words("export"),
            "create": _i18n_words("create"),
            "edit": _i18n_words("edit"),
            "delete": _i18n_words("delete"),
            "detail": _i18n_words("detail"),
            "status_toggle": _i18n_words("disable", "enable"),
            "batch": _i18n_words("batch"),
        }
        try:
            return self.page.locator("button, a").evaluate_all(
                """(els, terms) => {
                    const texts = els.map(e => (e.textContent || '').trim());
                    return Object.fromEntries(Object.entries(terms).map(
                        ([key, words]) => [key, words.some(
                            word => texts.some(text => text.includes(word))
                        )]
                    ));
                }""", terms)
        except Exception:
            return {key: False for key in terms}

    def scan_pagination(self) -> Dict[str, Any]:
        try:
            values = self.page.locator(".el-pagination").evaluate_all(
                """els => els.slice(0, 1).map(el => ({
                    total: (el.querySelector('.el-pagination__total')
                        ?.textContent || '').trim()
                }))"""
            )
        except Exception:
            values = []
        if not values:
            return {}
        total_txt = values[0].get("total", "")
        m = re.search(r"(\d+)", total_txt)
        return {"has_pagination": True, "total": int(m.group(1)) if m else None}

    def guess_list_api(self, sample_row: Optional[Dict[str, str]] = None) -> Optional[str]:
        """
        从捕获的请求里挑最像列表接口的那个，取路径片段。

        按"最后出现"选有个坑：通知公告、未读消息角标这类全局小组件也会
        打带 list 字样的接口（比如 /system/notice/listTop），而且往往是
        定时轮询的，扫描全程耗时几秒到几十秒，轮询几次就会排到 api_calls
        末尾，把真正这个页面的列表接口顶掉——选出来的 list_api 跟这页业务
        毫无关系，执行时怎么等都等不到匹配的响应。

        三层纠偏，一层比一层弱：
        1. 内容比对——传了表格第一行的样本值（sample_row）就去比候选接口
           的响应体里有没有这些值。这是最硬的证据："响应里真有页面上看到
           的这行数据"，比猜名字/猜时序都可靠，能命中的话直接采信。
        2. 业务词比对——页面自己的地址（如 /web/country/list）跟候选接口
           路径有没有共同的业务词（如 country），没有内容证据时退到这层。
        3. 都没有信号（页面地址提取不出英文业务词、样本值也没在任何候选
           响应体里出现过）就退回原来"直接选最后一个"的老办法。
        """
        cands = [u for u in self.api_calls
                 if re.search(r"(list|page|query|search|find)", u, re.I)]
        if not cands:
            pick = self.api_calls[-1] if self.api_calls else None
        else:
            pick = (self._pick_by_body_match(cands, sample_row)
                    or self._pick_by_page_words(cands)
                    or cands[-1])
        if not pick:
            return None
        path = urlparse(pick).path
        return path if len(path) > 1 else None

    def _pick_by_body_match(self, cands: List[str],
                            sample_row: Optional[Dict[str, str]]) -> Optional[str]:
        """
        谁的响应体里包含的样本值最多就是谁。只用长度 >= 4 的样本值参与比对
        （"启用"/"否"这类短文案太通用，随便什么响应都可能碰巧包含，反而
        会误判；日期、手机号、名称这类值足够独特，命中了才算数）。
        """
        if not sample_row:
            return None
        values = [v.strip() for v in sample_row.values() if v and len(v.strip()) >= 4]
        if not values:
            return None
        best_u, best_score = None, 0
        for u in cands:   # 顺序遍历，同分取后出现的，跟原来"选最后一个"的直觉一致
            body = self._api_bodies.get(u)
            if not body:
                continue
            score = sum(1 for v in values if v in body)
            if score > 0 and score >= best_score:
                best_u, best_score = u, score
        return best_u

    def _pick_by_page_words(self, cands: List[str]) -> Optional[str]:
        page_words = _path_words(urlparse(getattr(self.page, "url", "") or "").path)
        scored = [u for u in cands if page_words & _path_words(urlparse(u).path)]
        return scored[-1] if scored else None

    # ---------- 多语言合并 ----------
    def switch_language(self, languages: Dict[str, Any], code: str) -> bool:
        """
        跟运行期 switch_language 动作同样的交互逻辑：点触发器，点目标语言的
        文字。扫描阶段容错，切不过去就返回 False 跳过这门语言，不影响其它
        语言、也不影响后面别的扫描项。
        """
        try:
            switch_page_language(self.page, languages, code)
            return True
        except Exception:
            return False

    def scan_language_variants(self, languages: Optional[Dict[str, Any]],
                               report: Dict[str, Any]) -> "tuple[Dict, Dict]":
        """
        依次切到配置里的每种语言，把搜索表单 label / 表格表头 / 新增弹窗
        字段 label 按 DOM 位置和默认语言（扫描时最先拿到的 canonical 文案，
        也是所有 case YAML 里继续使用的值）对齐，拼出 label_variants /
        header_variants。

        没配 languages.switcher_trigger/options 就是单语言页面，直接返回
        空字典——不强求每个项目都配多语言。

        languages.scan_languages 决定"扫哪几种"，不给（或空列表）就只用
        默认语言扫出来的文案，不做多语言合并——每多扫一种语言就要重新
        切一次语言、重新扫一遍表单/表头/新增弹窗字段，字段多、弹窗复杂
        的页面这个开销不小（新增弹窗字段一多，光这部分就能占大半个扫描
        预算），不是所有页面都值得为多语言健壮性买这份单，交给项目自己
        按页面权衡：想要哪几种语言的健壮性就配哪几种，不配就是"只扫默认
        语言、越快越好"。这跟 languages.options（运行时能切到的全部语言，
        执行时选语言、"多语言检查"用例遍历翻译正确性都还是用它，不受
        scan_languages 影响）是两回事。
        """
        label_variants: Dict[str, Dict[str, str]] = {}
        header_variants: Dict[str, Dict[str, str]] = {}
        if not (languages and languages.get("switcher_trigger") and languages.get("options")):
            return label_variants, header_variants
        scan_langs = languages.get("scan_languages")
        if not scan_langs:
            return label_variants, header_variants

        canonical_labels = [f["label"] for f in report.get("form_fields", [])]
        canonical_headers = _unique(report.get("table", {}).get("headers", []))
        canonical_dialog_labels = [f["label"] for f in
                                   (report.get("create_form") or {}).get("fields", [])]
        dialog_positions = (report.get("create_form") or {}).get("field_positions")
        has_create = report.get("buttons", {}).get("create", False)
        baseline_code = default_language_code(languages)

        def scan_aligned(scan_fn, canonical, attempts: int = 12):
            """语言切换会触发整页异步重绘，等结构真正与基准页对齐。

            过去固定等待 1 秒后只读一次。批量扫描时第一门外语经常仍处于
            表单卸载/重建之间，得到空数组或残缺数组，于是整门语言的映射被
            静默丢弃；后面的语言因为页面已经热起来反而正常。
            """
            if not canonical:
                return []
            latest = []
            for _ in range(attempts):
                latest = scan_fn()
                if len(latest) == len(canonical):
                    return latest
                self.page.wait_for_timeout(300)
            return latest

        for code in scan_langs:
            # canonical 文案已经在基准语言下扫描完成，重复切回并重新扫描不会
            # 产生任何 variant，反而会多一次容易受 hover 菜单影响的交互。
            if code == baseline_code:
                continue
            if not self.switch_language(languages, code):
                continue
            translated_labels = scan_aligned(
                self.scan_form_labels, canonical_labels)
            translated_headers = scan_aligned(
                self.scan_table_headers, canonical_headers)
            _merge_positional(label_variants, canonical_labels,
                              translated_labels, code)
            _merge_positional(header_variants, canonical_headers,
                              translated_headers, code)
            if has_create and canonical_dialog_labels:
                translated_dialog_labels = (
                    self.scan_dialog_labels(dialog_positions)
                    if dialog_positions is not None else self.scan_dialog_labels()
                )
                _merge_positional(label_variants, canonical_dialog_labels,
                                  translated_dialog_labels, code)

        return label_variants, header_variants

    # 点一下语言切换控件之后，页面上"新冒出来的短文本"抓出来，用前后两次
    # 快照做差集——不依赖任何具体框架的 DOM 结构（Element UI 的下拉、纯手写
    # 的 ul、隐藏 div 切 display 都一样能抓到），比按框架猜选择器稳得多。
    _VISIBLE_LEAF_TEXTS_JS = """
        () => {
          const out = [];
          document.querySelectorAll('body *').forEach(el => {
            if (el.children.length > 0) return;   // 只要叶子节点，避免大容器把一整段文本当成一项
            const text = (el.textContent || '').trim();
            if (!text || text.length > 24) return;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;
            const style = getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') return;
            out.push(text);
          });
          return out;
        }
    """

    def probe_language_options(self, switcher_trigger: str) -> List[str]:
        """
        点开语言切换控件，把弹出菜单里的候选文案读出来——省掉手动 F12 一个个
        看菜单、照抄文案这一步（文案要一字不差，手抄容易错）。

        点击前后各拍一次"当前可见的短文本"快照，取差集当作候选项——不猜
        具体是哪个框架、哪套 class 命名，点开之后新冒出来的字就是候选。

        选择器本身点不到元素（超时）会抛 LookupError——跟"点开了但没有
        新文案出现"（返回空列表）是两种不同的失败，前者是选择器写错了，
        后者更可能是切换控件本身没有可见文字（图标/国旗图片）；分开报错
        才不会两种情况都甩一句"没探测到"让人无从下手。
        """
        try:
            before = set(self.page.evaluate(self._VISIBLE_LEAF_TEXTS_JS))
        except Exception:
            before = set()
        loc = self.page.locator(switcher_trigger).first
        try:
            loc.click(timeout=4000)
        except Exception:
            # 有些自定义下拉触发器（比如 Element UI 的 el-dropdown 自定义内容）
            # 通不过 Playwright 的可操作性检查（元素被判定为"暂不稳定"/"被遮挡"），
            # 但实际上是可以点的——force 绕过这些检查直接在元素中心派发点击事件，
            # 兜底试一次再彻底放弃。
            try:
                loc.click(timeout=2000, force=True)
            except Exception as e:
                raise LookupError(
                    f"选择器 '{switcher_trigger}' 点不到任何元素（普通点击和 force "
                    f"点击都失败）：{type(e).__name__}。当前页面: {self.page.url}"
                ) from e
        self.page.wait_for_timeout(500)
        try:
            after = self.page.evaluate(self._VISIBLE_LEAF_TEXTS_JS)
        except Exception:
            after = []
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass
        seen = set()
        new_texts = []
        for t in after:
            if t not in before and t not in seen:
                seen.add(t)
                new_texts.append(t)
        return new_texts[:15]


def probe_languages(url: str, switcher_trigger: str, storage_state: Optional[str] = None,
                    headless: bool = True, login: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    打开页面、点语言切换控件，把弹出菜单里的候选文案读出来。给项目设置
    「探测语言选项」用，省去手动 F12 一个个抄文案这一步；抄错一个字，
    switch_language 就永远找不到那个菜单项，是个隐蔽但常见的坑。

    传了 login 就走懒登录（cookie 还有效就直接用，过期了才真正登录一次并
    把新 cookie 存回去）——这个函数本来只是"点一下语言菜单"，之前图省事
    直接 page.goto() 完事，没走懒登录；结果 cookie 一过期，自动化会话
    悄悄停在登录页，语言切换控件根本不在登录页上，点什么都点不到，
    报错看着像"选择器写错了"，其实是登录过期，两种情况完全分不清。
    """
    with sync_playwright() as pw:
        browser = B.launch(pw, headless=headless)
        args = B.context_args()
        state = valid_storage_state(storage_state)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        B.optimize_scan_context(bctx)
        page = bctx.new_page()
        sc = PageScanner(page)
        if login:
            did = ensure_logged_in(page, url, login)
            if did and storage_state:
                save_storage_state(bctx, storage_state)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        texts = sc.probe_language_options(switcher_trigger)
        browser.close()
    return texts


def _merge_positional(variants: Dict[str, Dict[str, str]], canonical: List[str],
                      translated: List[str], code: str) -> None:
    """
    按下标对齐 canonical 和 translated 两个列表。数量对不上（比如某种语言下
    少渲染了一个字段）就整批跳过，宁可缺失也不错配——错配会把「国家」的
    翻译记成「城市」的，之后按翻译反查 canonical 全部乱套。
    """
    if len(canonical) != len(translated):
        return
    for c, t in zip(canonical, translated):
        if not c or not t or c == t:
            continue
        variants.setdefault(c, {})[code] = t


def scan(url: str, storage_state: Optional[str] = None,
         headless: bool = True, wait: int = 3000,
         languages: Optional[Dict[str, Any]] = None,
         login: Optional[Dict[str, Any]] = None,
         include_crud: bool = True,
         include_i18n: bool = True,
         on_phase=None) -> Dict[str, Any]:
    """
    传了 login 就走懒登录（cookie 还有效就直接用，过期了才真正登录一次并
    把新 cookie 存回去）——不传就是纯用 storage_state 里的 cookie，cookie
    过期会悄悄停在登录页扫描：真实事故是批量扫描 40 个页面，扫到中间某个
    页面时 cookie 刚好过期，页面被重定向到登录页，扫描器在登录页上"正常"
    扫完（表单/按钮识别不到东西，list_api 兜底捡到登录页自己的验证码图片
    接口），生成一份看着能跑、实际全是空壳的配置——不报错，比直接失败更
    危险，因为不会有人第一时间发现这份配置是废的。
    """
    scan_started = time.monotonic()
    timings: Dict[str, int] = {}

    def phase(name: str) -> None:
        if on_phase:
            on_phase(name)

    with sync_playwright() as pw:
        phase("启动浏览器")
        phase_started = time.monotonic()
        browser = B.launch(pw, headless=headless)
        args = B.context_args()
        state = valid_storage_state(storage_state)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        B.optimize_scan_context(bctx)
        page = bctx.new_page()
        sc = PageScanner(page)
        phase("登录并加载页面")
        if login:
            did = ensure_logged_in(page, url, login)
            if did and storage_state:
                save_storage_state(bctx, storage_state)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # 登录沿用默认 30 秒容忍慢接口；进入结构扫描后所有交互统一收紧，
        # 即使遗漏了某个动作级 timeout，也不会再单点白等半分钟。
        page.set_default_timeout(5000)
        phase("等待页面就绪")
        ready_ms = _wait_for_scan_ready(page, wait)
        # storage_state/localStorage 可能记住上次停留的英文或法文。扫描结果的
        # canonical 列名必须稳定，先切回项目约定的中文基准再读取页面结构。
        baseline_language = default_language_code(languages or {})
        if baseline_language:
            try:
                changed = switch_page_language(page, languages, baseline_language)
                if changed:
                    ready_ms += _wait_for_scan_ready(page, wait)
            except Exception:
                # 基准切换失败不让整页扫描报废；后面的多语言扫描会再次尝试，
                # 且执行报告会给出明确失败信息。
                pass
        timings["页面加载"] = int((time.monotonic() - phase_started) * 1000)
        timings["就绪等待"] = ready_ms

        phase_started = time.monotonic()
        phase("识别表格")
        table = sc.scan_table()
        timings["表格"] = int((time.monotonic() - phase_started) * 1000)

        # 主列表接口必须在探测下拉选项之前确定。scan_form() 会逐个打开国家、
        # 城市、加盟商等下拉框，这些控件也会请求 */list；如果等表单扫完再猜，
        # 最后出现的下拉候选接口很容易覆盖页面加载时真正的主列表接口。
        # “账户充值记录”被误识别成 /business/franchisee/list 就是这种情况。
        list_api = sc.guess_list_api(sample_row=table.get("sample_row"))

        phase_started = time.monotonic()
        phase("识别筛选项")
        form_fields = sc.scan_form()
        timings["筛选项"] = int((time.monotonic() - phase_started) * 1000)
        phase_started = time.monotonic()
        phase("识别按钮、分页和接口")
        buttons = sc.scan_buttons()
        pagination = sc.scan_pagination()
        timings["按钮/分页/API"] = int((time.monotonic() - phase_started) * 1000)
        report = {
            "url": url,
            "title": page.title(),
            "form_fields": form_fields,
            "table": table,
            "buttons": buttons,
            "pagination": pagination,
            "list_api": list_api,
        }
        # 表单结构放最后扫：它要点开弹窗，对页面状态的改动最大，
        # 放前面会影响表格/按钮的识别
        if include_crud and report["buttons"].get("create"):
            phase_started = time.monotonic()
            phase("识别新增弹窗")
            report["create_form"] = sc.scan_form_schema()
            timings["CRUD表单"] = int((time.monotonic() - phase_started) * 1000)
        # 多语言合并放最后：要来回切换语言、重新扫表单/表头，对页面状态
        # 改动更大，放前面会干扰上面这些默认语言下的扫描结果
        label_variants, header_variants = ({}, {})
        if include_i18n:
            phase_started = time.monotonic()
            phase("识别多语言结构")
            label_variants, header_variants = sc.scan_language_variants(languages, report)
            timings["多语言"] = int((time.monotonic() - phase_started) * 1000)
        if label_variants:
            report["label_variants"] = label_variants
        if header_variants:
            report["header_variants"] = header_variants
        timings["总计"] = int((time.monotonic() - scan_started) * 1000)
        report["scan_timings_ms"] = timings
        phase("关闭浏览器")
        browser.close()
    return report


def redetect_list_api(url: str, storage_state: Optional[str] = None,
                      headless: bool = True, wait: int = 3000,
                      login: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    只重新探测 list_api，不跑完整扫描。

    list_api 猜错是最常见的"只有一个字段不对"场景（同源全局小组件轮询、
    URL 命名巧合让最后一条候选换了人），为了修这一个字段等一次完整扫描
    （还要识别表单、按钮、分页、新增弹窗，配了多语言还要来回切换语言）
    没必要——那些结构本来就没错。这里只做 scan_table()（拿样本值给
    guess_list_api() 做内容比对）+ guess_list_api()，其余什么都不碰，
    调用方（batch.redetect_list_api）只把探测到的新值写回已有配置文件的
    list_api 字段，配置里其它任何内容都不动。
    """
    with sync_playwright() as pw:
        browser = B.launch(pw, headless=headless)
        args = B.context_args()
        state = valid_storage_state(storage_state)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        B.optimize_scan_context(bctx)
        page = bctx.new_page()
        sc = PageScanner(page)
        if login:
            did = ensure_logged_in(page, url, login)
            if did and storage_state:
                save_storage_state(bctx, storage_state)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _wait_for_scan_ready(page, wait)
        table = sc.scan_table()
        api = sc.guess_list_api(sample_row=table.get("sample_row"))
        browser.close()
    return api


def redetect_list_apis(pages: List[Dict[str, str]],
                       storage_state: Optional[str] = None,
                       headless: bool = True, wait: int = 3000,
                       login: Optional[Dict[str, Any]] = None,
                       on_page=None) -> List[Dict[str, Any]]:
    """在同一个浏览器上下文里批量重新探测多个页面的 ``list_api``。

    全局重探不能简单地循环 :func:`redetect_list_api`：那会为每个页面重新
    启动 Chromium、重新校验登录，几十个页面既慢又可能触发并发登录限制。
    这里复用一个浏览器上下文和 cookie，每页只新建一个 Page，页面之间不
    共享监听器和候选请求。单页失败只记录错误，不中断后续页面。
    """
    items = [dict(item) for item in pages]
    results: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = B.launch(pw, headless=headless)
        args = B.context_args()
        state = valid_storage_state(storage_state)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        B.optimize_scan_context(bctx)
        try:
            total = len(items)
            for index, item in enumerate(items, 1):
                name, url = item.get("name", ""), item.get("url", "")
                if on_page:
                    on_page(index, total, name, "running", None)
                page = bctx.new_page()
                result = {"name": name, "url": url, "api": None, "error": None}
                try:
                    sc = PageScanner(page)
                    if login:
                        did = ensure_logged_in(page, url, login)
                        if did and storage_state:
                            save_storage_state(bctx, storage_state)
                    else:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.set_default_timeout(5000)
                    _wait_for_scan_ready(page, wait)
                    table = sc.scan_table()
                    result["api"] = sc.guess_list_api(
                        sample_row=table.get("sample_row"))
                except Exception as exc:
                    result["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                results.append(result)
                if on_page:
                    on_page(index, total, name, "done", result)
        finally:
            browser.close()
    return results


# ---------- 生成配置 ----------

def to_config(report: Dict[str, Any], name: Optional[str] = None,
             languages: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields = report["form_fields"]
    table = report.get("table") or {}
    headers = _unique(table.get("headers", []))
    btns = report.get("buttons", {})
    ctypes = table.get("column_types", {})

    cases: List[Dict[str, Any]] = []

    # 1. 冒烟：页面能打开、表格有数据、渲染正常、无报错
    # assert_no_failed_request 和 assert_no_console_error 经常一起触发同一个问题
    # （比如图片挂了）——控制台消息不带 URL，failed_request 能报出具体链接是哪个。
    smoke = [{"assert_row_count": {"min": 1}},
             {"assert_no_render_garbage": None},
             {"assert_no_console_error": None},
             {"assert_no_failed_request": None}]
    if headers:
        # 表头断言必须覆盖扫描到的完整表格。过去这里只取前 5 列，导致宽表
        # 右侧的关键业务列（如手机号、金额、操作）完全没有进入生成配置，
        # 页面即使漏列或错列也会被冒烟用例误判为正常。
        smoke.insert(0, {"assert_headers": {"contains": headers}})
    cases.append({"name": "列表默认加载", "tags": ["smoke"], "steps": smoke})

    # 1b. 工具栏按钮可用性巡检（任何页面都值得测）
    cases.append({"name": "按钮可用性巡检", "tags": ["health"],
                  "steps": [{"check_buttons": None}]})
    # 行内“查看/详情”会进入菜单扫描发现不了的二级页面。运行时动态遍历
    # 详情内部 Tab，覆盖“充值明细/我的接单/评价”等只读子列表与查询。
    if btns.get("detail"):
        cases.append({"name": "详情页与内部页签巡检", "tags": ["health"],
                      "steps": [{"check_detail_tabs": None}]})

    # 2. 每个文本框生成一条搜索用例
    #    取表格里已有的值做搜索词，保证一定能搜出结果
    sample = table.get("sample_row", {})
    for f in fields:
        if f["type"] != "text":
            continue
        label = f["label"]
        col = _match_column(label, headers)
        seed = sample.get(col) if col else None
        if not seed or len(seed) > 20:
            seed = "${random}"
        steps = [{"fill": {"label": label, "value": seed}}, {"search": None}]
        if col and seed != "${random}":
            steps.append({"assert_column_all": {"column": col, "contains": seed}})
        else:
            steps.append({"assert_row_count": {"min": 0}})
        cases.append({"name": f"搜索-{label}", "tags": ["search"], "steps": steps})

    # 3. 下拉筛选：遍历每个选项逐一筛选，抓"某个选项筛选后报错"
    fields_by_label = {f.get("label"): f for f in fields if f.get("label")}

    def dependency_chain(field: Dict[str, Any]) -> List[Dict[str, str]]:
        """递归补齐国家 -> 城市 -> 服务类型这类多级联动前置步骤。"""
        chain: List[Dict[str, str]] = []
        seen = set()
        dep = field.get("depends_on")
        while dep and dep.get("label") not in seen:
            seen.add(dep["label"])
            chain.append(dep)
            parent = fields_by_label.get(dep["label"], {})
            dep = parent.get("depends_on")
        chain.reverse()
        return chain

    for f in fields:
        if f["type"] != "select":
            continue
        label = f["label"]
        col = _match_column(label, headers)
        if not f.get("options"):
            # 扫描时没有枚举到选项，不代表页面没有这个筛选项。远程联想下拉
            # 往往只有执行时输入查询词才返回选项；普通异步下拉也可能只是扫描
            # 窗口内尚未加载完成。因此无条件生成用例，把“展开/等待/选择”推迟
            # 到运行阶段。能从表格首行取到对应值时顺便作为远程查询词。
            seed = sample.get(col) if col else None
            params = {"label": label}
            if col:
                params["column"] = col
            if seed:
                query = str(seed).strip()
                if query and len(query) <= 40:
                    params["query"] = query
            cases.append({
                "name": f"筛选-{label}（运行时取选项）",
                "tags": ["search"],
                "steps": [{"check_select_options": params}],
            })
            continue
        steps = []
        deps = dependency_chain(f)
        # 这个用例名字用的局部变量绝不能叫 name——to_config() 自己的
        # 形参也叫 name（页面名字），for 循环不会开新作用域，同名局部变量
        # 会直接把形参覆盖掉，导致函数末尾 cfg["name"] 拿到的是"最后一个
        # 下拉筛选用例的名字"而不是页面名字（真实出现过的 bug：页面配置
        # 文件的 name: 字段变成了"筛选-状态"这种用例名）。
        case_name = f"筛选-{label}"
        if deps:
            # 级联下拉：这个字段没选父级就是 disabled 的，扫描时已经验证过
            # "先选父级再选它"能测出选项。多级联动必须从最上游依次选择，
            # 不能只选直接父级（城市本身可能还依赖国家）。
            for dep in deps:
                steps.append({"select": {
                    "label": dep["label"], "option": dep["option"]}})
                steps.append({"wait": 500})
            case_name = f"筛选-{label}（联动{'/'.join(d['label'] for d in deps)}）"
        # 动作内部逐个选项搜索后立即核对列值；不再只检查最后一个选项，更
        # 不会因为最后一个选项返回空表而“跳过断言”形成假通过。
        params = {"label": label}
        if col:
            params["column"] = col
        steps.append({"check_select_options": params})
        cases.append({"name": case_name, "tags": ["search"], "steps": steps})

    # 4. 日期范围
    for f in fields:
        if f["type"] != "date_range":
            continue
        label = f["label"]
        col = next((h for h in headers if ctypes.get(h) == "date"), None)
        steps = [{"date_range": {"label": label,
                                 "start": "${days_ago_7}", "end": "${today}"}},
                 {"search": None}]
        if col:
            steps.append({"assert_column_range": {
                "column": col, "start": "${days_ago_7}",
                "end": "${today} 23:59:59", "kind": "date"}})
        cases.append({"name": f"时间筛选-{label}", "tags": ["search"], "steps": steps})

    # 5. 重置
    text_labels = [f["label"] for f in fields if f["type"] == "text"]
    if btns.get("reset") and text_labels:
        cases.append({"name": "重置条件", "tags": ["search"], "steps": [
            {"fill": {"label": text_labels[0], "value": "ZZ_NOT_EXIST"}},
            {"search": None},
            {"click": "reset_btn"},
            {"wait": 500},
            {"assert_inputs_empty": {"labels": text_labels}},
        ]})

    # 6. 分页
    if report.get("pagination", {}).get("has_pagination"):
        cases.append({"name": "分页默认加载", "tags": ["list"], "steps": [
            {"assert_row_count": {"min": 1}},
        ]})

    # 7. 前后端一致性
    # 接口字段映射无法从 DOM 可靠推导；不生成带 TODO 的不可执行用例。

    # 8. 导出
    if btns.get("export"):
        # 之前这里截了前 5 列（[:5]），真实事故：某页面第 7 列"加盟商"导出的是
        # 内部 ID、页面上显示的是名称——这种"导出字段值本身就映射错了"的问题
        # 正是这条值比对该抓的，但因为排在第 5 列之后，从来没被比过，「抽样比对
        # N 个字段一致」看着全绿，实际上从没检查过这一列。字段值比对是纯字符串
        # 比较，几十列也没有明显开销，没有理由只挑排在前面的几列，能比的全比。
        cmp_cols = [h for h in headers if h not in ("序号", "操作", "图片")
                    and ctypes.get(h) in ("money", "date", "phone", "text")]
        cases.append({"name": "导出数据验证", "tags": ["export"], "steps": [
            {"search": None},
            {"capture": "page_data"},
            {"export_and_verify": {
                "compare_with": "page_data",
                "columns": cmp_cols,
                "row_count": "total",
            }},
        ]})

    # 9. 新增/修改/详情/删除闭环——有新增弹窗结构（Element UI）才能全自动生成，
    # 扫不出结构（比如非 Element UI 页面）就退回骨架，标 skip 等人工补。
    # 铁律：只动自己创建的数据；identity 字段用来在列表里唯一定位这条记录，
    # 优先选能对上表格列名的文本字段，保证后面 find_row_by 真的能用。
    create_fields = (report.get("create_form") or {}).get("fields", [])
    identity_field = _pick_identity(create_fields, headers)

    if create_fields and identity_field:
        identity = identity_field["label"]
        identity_column = _match_column(identity, headers) or identity
        required_labels = [f["label"] for f in create_fields if f.get("required")]

        if required_labels:
            cases.append({"name": "新增-必填校验", "tags": ["crud"], "steps": [
                {"click": "create_btn"},
                {"assert_form_errors": {"expect": required_labels}},
            ]})

        loop_steps = [{"create_and_verify": {
            "fields": create_fields, "identity": identity,
            "identity_column": identity_column}}]
        if btns.get("edit"):
            edit_field = _pick_edit_field(create_fields, identity)
            if edit_field:
                # 没有另一个能改的文本字段就跳过——不强行打开一个改不了
                # 什么、还得想办法关掉的编辑弹窗
                loop_steps.append({"assert_form_prefilled": None})
                loop_steps.append({"edit_and_verify": {
                    "fields": {edit_field["label"]: "${random}"}}})
        if btns.get("detail"):
            loop_steps.append({"assert_detail_matches": None})
        if btns.get("status_toggle") and "状态" in headers:
            # 只在表头真有"状态"这一列时才生成——没有就不知道验哪一列，
            # 生成了也只会白白失败
            loop_steps.append({"toggle_status_and_verify": {"column": "状态"}})
        if btns.get("delete"):
            loop_steps.append({"delete_and_verify": None})

        cases.append({"name": "新增-修改-详情-删除完整闭环", "tags": ["crud"],
                      "steps": loop_steps})
    elif btns.get("create"):
        cases.append({"name": "新增数据（需补充字段）", "tags": ["crud"],
                      "skip": True, "steps": [
            {"click": "create_btn"},
            {"wait": 800},
            {"fill_form": {"fields": {"字段名": "auto_${random}"}}},
            {"click": "submit_btn"},
            {"assert_message": {"contains": "成功"}},
            {"wait_api": None},
            {"assert_in_list": {"column": "列名", "value": "${form_字段名}"}},
        ]})

    # 多语言检查：项目配了 languages.switcher_trigger/options 才生成，
    # 零配置生成不出来——语言切换控件没有统一 DOM 约定，猜不出来
    if languages and languages.get("switcher_trigger") and languages.get("options"):
        i18n_steps = []
        for code in languages["options"]:
            i18n_steps.append({"switch_language": {"to": code}})
            i18n_steps.append({"search": None})
            i18n_steps.append({"assert_no_i18n_leak": None})
            i18n_steps.append({"assert_no_mixed_language": {"expect": code}})
        cases.append({"name": "多语言检查", "tags": ["i18n"], "steps": i18n_steps})

    cfg: Dict[str, Any] = {
        "name": name or report.get("title") or "自动生成",
        "url": report["url"],
    }
    if report.get("list_api"):
        cfg["list_api"] = report["list_api"]
    if btns.get("export"):
        cfg["export_mode"] = "auto"
    if languages:
        cfg["languages"] = languages
    if report.get("label_variants"):
        cfg["label_variants"] = report["label_variants"]
    if report.get("header_variants"):
        cfg["header_variants"] = report["header_variants"]
    cfg["cases"] = cases
    return cfg


# 这些词单独出现时没有区分度，去掉它们后剩下的部分不足以认定匹配
_WEAK = re.compile(r"(请输入|请选择|是否)")

# 页面地址、接口路径里到处都有的通用词，不代表业务域，参与匹配只会
# 让不相关的候选也"命中"，必须先剔除
_LIST_API_STOPWORDS = {"list", "page", "query", "search", "find",
                       "web", "api", "index", "home", "vaweb"}


def _path_words(path: str) -> set:
    """从 URL 路径里取小写英文单词集合，去掉通用词，剩下的当业务域标记"""
    return set(re.findall(r"[a-z]+", path.lower())) - _LIST_API_STOPWORDS


def _unique(values: List[str]) -> List[str]:
    """保持顺序去重，固定列与主表重复渲染时只保留一次。"""
    seen = set()
    return [v for v in values if v and not (v in seen or seen.add(v))]


def _match_column(label: str, headers: List[str]) -> Optional[str]:
    """
    把搜索项 label 映射到表格列名。宁可返回 None，也不要错配。

    错配比不配危险得多：把「订单状态」配到「订单金额」列上，
    会生成一条永远失败的断言，让人以为系统有 bug。
    所以只接受精确匹配和完整包含，不做去词后的模糊匹配。
    """
    lb = _WEAK.sub("", label).strip()
    if not lb:
        return None

    # 1. 精确
    if lb in headers:
        return lb
    # 2. label 是列名的完整子串，或反之（如「手机号」vs「下单人手机号」）
    #    要求较短的一方至少 3 个字符，避免「号」「人」这类噪声命中
    best = None
    for h in headers:
        if lb == h:
            return h
        short, long_ = (lb, h) if len(lb) <= len(h) else (h, lb)
        if len(short) >= 3 and short in long_:
            # 取最短的候选，「手机号」应配「手机号」而不是「乘车人手机号」
            if best is None or len(h) < len(best):
                best = h
    return best


def _pick_identity(fields: List[Dict[str, Any]], headers: List[str]) -> Optional[Dict[str, Any]]:
    """
    挑一个字段用来在列表里唯一定位新建的记录：必须是文本类型（好填、好按
    contains 比对），优先选能对上表格列名的——保证 find_row_by 真能用它去找，
    不然生成的闭环用例上来就会因为找不到列而失败。
    """
    texts = [f for f in fields
            if f.get("type") == "text" and f.get("fillable", True)]
    for f in texts:
        if _match_column(f["label"], headers):
            return f
    return texts[0] if texts else None


def _pick_edit_field(fields: List[Dict[str, Any]],
                     identity_label: str) -> Optional[Dict[str, Any]]:
    """挑一个非 identity 的文本字段做修改验证——改的不是定位用的字段，
    改完那条记录还能用原来的 identity 值搜到。"""
    for f in fields:
        if f.get("type") == "text" and f["label"] != identity_label \
                and f.get("fillable", True):
            return f
    return None


def write_config(cfg: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, width=100)
