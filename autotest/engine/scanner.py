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
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml
from playwright.sync_api import sync_playwright, Page
from . import browser as B
from .state import valid_storage_state


class PageScanner:
    def __init__(self, page: Page):
        self.page = page
        self.api_calls: List[str] = []
        page.on("response", self._on_resp)

    def _on_resp(self, resp):
        u = resp.url
        if resp.status == 200 and re.search(r"/(api|web)/", u) and "static" not in u:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" in ct:
                self.api_calls.append(u)

    # ---------- 表单识别 ----------
    def scan_form(self) -> List[Dict[str, Any]]:
        fields = []
        items = self.page.locator(".el-form-item")
        for i in range(items.count()):
            item = items.nth(i)
            try:
                label = item.locator(".el-form-item__label").first.inner_text().strip()
            except Exception:
                continue
            label = label.rstrip(":：").strip()
            if not label:
                continue

            kind, extra = "text", {}
            if item.locator(".el-date-editor").count() > 0:
                kind = "date_range" if item.locator(".el-range-input").count() > 0 else "date"
            elif item.locator(".el-select").count() > 0:
                kind = "select"
                extra["options"] = self._peek_options(item)
            elif item.locator(".el-input__inner").count() > 0:
                ph = item.locator(".el-input__inner").first.get_attribute("placeholder") or ""
                extra["placeholder"] = ph
            else:
                continue
            fields.append({"label": label, "type": kind, **extra})
        return fields

    def _peek_options(self, item) -> List[str]:
        """
        点开一个下拉看看有什么选项。级联选择器（比如「城市」依赖「国家」先选）
        在没选父级之前是 disabled 的——Playwright 点一个 disabled 元素会一直
        重试等它变成可点，默认要等 30s。一个表单里有几个这种级联下拉，扫描
        单页就可能被拖到 150s 超时上限。这里给点击一个短超时，点不动就当没有
        选项处理（本来也生成不出可用的筛选用例），别在一个下拉上死等半分钟。
        """
        try:
            item.locator(".el-select").first.click(timeout=3000)
            self.page.wait_for_timeout(300)
            dd = self.page.locator(".el-select-dropdown:visible").last
            opts = [o.strip() for o in
                    dd.locator(".el-select-dropdown__item").all_inner_texts()]
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)
            return opts[:15]
        except Exception:
            return []

    # ---------- 表格识别 ----------
    def scan_table(self) -> Dict[str, Any]:
        tables = self.page.locator(".el-table, .ant-table, table.data-table, table")
        tbl = None
        for i in range(tables.count()):
            candidate = tables.nth(i)
            try:
                headers = candidate.locator(
                    ".el-table__header-wrapper th .cell, .ant-table-thead th, thead th").all_inner_texts()
                if candidate.is_visible() and any(h.strip() for h in headers):
                    tbl = candidate
                    break
            except Exception:
                continue
        if tbl is None:
            return {}

        # 冻结列（左固定/右固定）会让 Element/Antd 把表头和表体各多渲染一份，
        # 表头、表体必须各自只取「第一个」容器，按下标才能严格一一对应——
        # 之前不限定容器时，两份表头会先被 _unique() 去重掉，但单元格取值
        # 仍按原始（含重复）下标去对，导致取样值整体错位，殃及后面所有列：
        # 搜索用例的种子值、列类型猜测全部跟着错。
        header_cells = tbl.locator(
            ".el-table__header-wrapper, .ant-table-thead, thead").first.locator("th .cell, th")
        rows = tbl.locator(
            ".el-table__body-wrapper, .ant-table-tbody, tbody").first.locator("tr.el-table__row, tr")

        headers = _unique([h.strip() for h in header_cells.all_inner_texts() if h.strip()])

        sample = {}
        if rows.count() > 0:
            cells = rows.nth(0).locator("td")
            n = min(len(headers), cells.count())
            for j in range(n):
                sample[headers[j]] = cells.nth(j).inner_text().strip()[:40]

        return {
            "headers": headers,
            "row_count": rows.count(),
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
        def has(*words):
            return any(self.page.locator(
                f"button:has-text('{w}'), a:has-text('{w}')").count() > 0 for w in words)
        return {
            "search": has("搜索", "查询"),
            "reset": has("重置", "清空"),
            "export": has("导出", "下载"),
            "create": has("新增", "添加", "创建"),
            "edit": has("编辑", "修改"),
            "delete": has("删除"),
            "batch": has("批量"),
        }

    def scan_pagination(self) -> Dict[str, Any]:
        p = self.page.locator(".el-pagination")
        if p.count() == 0:
            return {}
        total_txt = ""
        if p.locator(".el-pagination__total").count() > 0:
            total_txt = p.locator(".el-pagination__total").first.inner_text()
        m = re.search(r"(\d+)", total_txt)
        return {"has_pagination": True, "total": int(m.group(1)) if m else None}

    def guess_list_api(self) -> Optional[str]:
        """从捕获的请求里挑最像列表接口的那个，取路径片段"""
        cands = [u for u in self.api_calls
                 if re.search(r"(list|page|query|search|find)", u, re.I)]
        pick = cands[-1] if cands else (self.api_calls[-1] if self.api_calls else None)
        if not pick:
            return None
        path = urlparse(pick).path
        return path if len(path) > 1 else None


def scan(url: str, storage_state: Optional[str] = None,
         headless: bool = True, wait: int = 3000) -> Dict[str, Any]:
    with sync_playwright() as pw:
        browser = B.launch(pw, headless=headless)
        args = B.context_args()
        state = valid_storage_state(storage_state)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        page = bctx.new_page()
        sc = PageScanner(page)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # 曾经改成"等到第一个 api/json 响应就继续"想省点时间，结果翻车：
        # 页面进来常先打几个无关请求（权限校验/字典/当前用户），一旦命中这种
        # 早到的响应，只再等一点点就去扫表单/按钮，搜索框、筛选项、导出按钮
        # 经常还没渲染完，扫描直接漏掉——生成的用例看着正常，实际少了一大半。
        # 扫描只跑一次，慢 1-2 秒不影响体验，但扫漏了没人会发现，代价划不来。
        # 老老实实固定等，用等待换正确性。
        page.wait_for_timeout(wait)

        report = {
            "url": url,
            "title": page.title(),
            "form_fields": sc.scan_form(),
            "table": sc.scan_table(),
            "buttons": sc.scan_buttons(),
            "pagination": sc.scan_pagination(),
            "list_api": sc.guess_list_api(),
        }
        browser.close()
    return report


# ---------- 生成配置 ----------

def to_config(report: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
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
        smoke.insert(0, {"assert_headers": {"contains": headers[:5]}})
    cases.append({"name": "列表默认加载", "tags": ["smoke"], "steps": smoke})

    # 1b. 工具栏按钮可用性巡检（任何页面都值得测）
    cases.append({"name": "按钮可用性巡检", "tags": ["health"],
                  "steps": [{"check_buttons": None}]})

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
    for f in fields:
        if f["type"] != "select" or not f.get("options"):
            continue
        label = f["label"]
        col = _match_column(label, headers)
        steps = [{"check_select_options": {"label": label}}]
        if col:
            # 循环后 ${selected_label} 停在最后一个选项，据此校验列值
            steps.append({"assert_column_all": {"column": col,
                                                "equals": "${selected_%s}" % label}})
        cases.append({"name": f"筛选-{label}", "tags": ["search"], "steps": steps})

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
        cmp_cols = [h for h in headers if h not in ("序号", "操作", "图片")
                    and ctypes.get(h) in ("money", "date", "phone", "text")][:5]
        cases.append({"name": "导出数据验证", "tags": ["export"], "steps": [
            {"search": None},
            {"capture": "page_data"},
            {"export_and_verify": {
                "compare_with": "page_data",
                "columns": cmp_cols,
                "row_count": "total",
            }},
        ]})

    # 9. 新增（骨架，字段需人工填）
    if btns.get("create"):
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

    cfg: Dict[str, Any] = {
        "name": name or report.get("title") or "自动生成",
        "url": report["url"],
    }
    if report.get("list_api"):
        cfg["list_api"] = report["list_api"]
    if btns.get("export"):
        cfg["export_mode"] = "auto"
    cfg["cases"] = cases
    return cfg


# 这些词单独出现时没有区分度，去掉它们后剩下的部分不足以认定匹配
_WEAK = re.compile(r"(请输入|请选择|是否)")


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


def write_config(cfg: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, width=100)
