"""
菜单爬取。

打开系统首页，展开左侧菜单树，收集所有可访问的页面。
产出一份「系统地图」，供用户在界面上勾选要测哪些页面。

难点在于后台菜单通常是：
  - 多级折叠，父菜单要点开才能看到子菜单
  - 用 JS 路由跳转，<a href> 可能是 "javascript:;" 或根本没有 href
  - 点击后 URL 才变化

所以策略是「点击 + 观察 URL 变化」，而不是解析 href。
"""
import re
import time
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from .login import ensure_logged_in, is_login_page

# 常见的侧边菜单容器，按可靠性排序
MENU_CONTAINERS = [
    ".el-menu",                    # Element UI
    ".ant-menu",                   # Ant Design
    "aside .menu", ".sidebar",
    "nav.sidebar", "#sidebar",
    "[class*='sidebar'] ul",
]

# 菜单项（叶子节点，点了会跳转）
LEAF_SELECTORS = [
    ".el-menu-item",
    ".ant-menu-item",
    "li.menu-item:not(.has-children)",
]

# 可展开的父菜单
SUBMENU_SELECTORS = [
    ".el-submenu__title, .el-sub-menu__title",
    ".ant-menu-submenu-title",
]

# 这些菜单跳过：要么是危险操作，要么不是数据列表页
SKIP_KEYWORDS = ["退出", "登出", "注销", "logout", "个人中心", "修改密码", "帮助"]


def _text(loc) -> str:
    try:
        return re.sub(r"\s+", " ", loc.inner_text()).strip()
    except Exception:
        return ""


def _expand_all(page: Page, rounds: int = 4) -> None:
    """
    反复点开所有折叠的父菜单。
    多轮是因为展开一级后可能露出新的二级折叠项。
    """
    for _ in range(rounds):
        opened = 0
        for sel in SUBMENU_SELECTORS:
            titles = page.locator(sel)
            for i in range(titles.count()):
                t = titles.nth(i)
                try:
                    if not t.is_visible():
                        continue
                    # 已展开的跳过，避免点了又收起
                    parent = t.locator("xpath=..")
                    cls = (parent.get_attribute("class") or "")
                    if "opened" in cls or "is-opened" in cls or "ant-menu-submenu-open" in cls:
                        continue
                    t.click(timeout=2000)
                    opened += 1
                    page.wait_for_timeout(180)
                except Exception:
                    continue
        if opened == 0:
            break


def _menu_path(leaf) -> str:
    """尽量拼出「订单管理 / 订单列表」这样的层级路径"""
    try:
        anc = leaf.locator(
            "xpath=ancestor::*[contains(@class,'el-submenu') or "
            "contains(@class,'el-sub-menu') or contains(@class,'ant-menu-submenu')]"
        )
        parts = []
        for i in range(min(anc.count(), 3)):
            title = anc.nth(i).locator(
                ".el-submenu__title, .el-sub-menu__title, .ant-menu-submenu-title"
            ).first
            t = _text(title)
            if t and t not in parts:
                parts.append(t)
        return " / ".join(parts)
    except Exception:
        return ""


def crawl_menu(page: Page, home_url: str, max_pages: int = 60,
               probe: bool = True) -> List[Dict]:
    """
    爬取菜单，返回页面列表。

    probe=True 时会逐个点击菜单、记录跳转后的 URL，并粗略判断
    该页是否是「带表格的列表页」（值得测的那种）。
    这一步慢但值得——否则用户拿到一堆没有表格的页面也没法测。
    """
    page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    _expand_all(page)

    # 收集叶子菜单的文本和层级
    leaves = []
    seen_text: Set[str] = set()
    for sel in LEAF_SELECTORS:
        items = page.locator(sel)
        for i in range(items.count()):
            it = items.nth(i)
            try:
                if not it.is_visible():
                    continue
            except Exception:
                continue
            name = _text(it)
            if not name or name in seen_text:
                continue
            if any(k in name for k in SKIP_KEYWORDS):
                continue
            seen_text.add(name)
            leaves.append({"name": name, "group": _menu_path(it)})
        if leaves:
            break   # 第一个匹配到的选择器就是这套 UI 的，不用再试别的

    if not probe:
        return [dict(l, url="", has_table=None) for l in leaves][:max_pages]

    # 逐个点击，记录 URL
    out = []
    seen_url: Set[str] = set()
    for leaf in leaves[:max_pages]:
        name = leaf["name"]
        try:
            page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(900)
            _expand_all(page, rounds=3)

            target = page.locator(
                f"{','.join(LEAF_SELECTORS)}"
            ).filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"))
            if target.count() == 0:
                target = page.get_by_text(name, exact=True)
            if target.count() == 0:
                continue

            before = page.url
            target.first.click(timeout=4000)
            page.wait_for_timeout(1800)
            url = page.url

            # URL 没变有两种情况：
            #   1. 点了没反应（无效菜单）—— 应该丢弃
            #   2. 当前就停在这个菜单对应的页面 —— 是有效页面，不能丢
            # 用「点完之后菜单项是否处于选中态」来区分。
            if url == before:
                try:
                    cls = target.first.get_attribute("class") or ""
                    active = "is-active" in cls or "active" in cls or "selected" in cls
                except Exception:
                    active = False
                # 起始页本身也算数：首轮进来时 URL 就等于目标页
                if not active and before.rstrip("/") != home_url.rstrip("/"):
                    continue
            if is_login_page(page):
                continue
            # 去掉 hash 里的随机参数，避免同一页面重复
            key = url.split("?")[0]
            if key in seen_url:
                continue
            seen_url.add(key)

            info = _probe_page(page)
            out.append({
                "name": name,
                "group": leaf["group"],
                "url": url,
                **info,
            })
        except Exception:
            continue

    return out


def _probe_page(page: Page) -> Dict:
    """粗略判断这一页值不值得测"""
    try:
        table = page.locator(".el-table, .ant-table, table.data-table")
        has_table = table.count() > 0 and table.first.is_visible()
    except Exception:
        has_table = False

    cols, rows = 0, 0
    if has_table:
        try:
            cols = len([t for t in table.first.locator(
                ".el-table__header-wrapper th .cell, .ant-table-thead th"
            ).all_inner_texts() if t.strip()])
            rows = table.first.locator(
                ".el-table__body-wrapper tbody tr.el-table__row, .ant-table-tbody tr"
            ).count()
        except Exception:
            pass

    def has_btn(*words):
        for w in words:
            try:
                if page.locator(f"button:has-text('{w}'), a:has-text('{w}')").count() > 0:
                    return True
            except Exception:
                pass
        return False

    return {
        "has_table": has_table,
        "columns": cols,
        "rows": rows,
        "has_search": has_btn("搜索", "查询"),
        "has_export": has_btn("导出", "下载"),
        "has_create": has_btn("新增", "添加", "创建"),
        # 有表格 + 有搜索 = 典型的数据列表页，最值得测
        "recommended": bool(has_table and cols >= 2),
    }


def discover(home_url: str, login_cfg: Optional[dict] = None,
             storage_state: Optional[str] = None,
             max_pages: int = 60, probe: bool = True,
             on_progress=None) -> List[Dict]:
    """对外入口：登录 → 爬菜单 → 返回页面列表"""
    import os

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        args = {"viewport": {"width": 1600, "height": 900},
                "locale": "zh-CN", "ignore_https_errors": True}
        if storage_state and os.path.exists(storage_state):
            args["storage_state"] = storage_state
        bctx = browser.new_context(**args)
        bctx.set_default_timeout(20000)
        page = bctx.new_page()

        if login_cfg:
            did = ensure_logged_in(page, home_url, login_cfg)
            if did and storage_state:
                bctx.storage_state(path=storage_state)
            if on_progress:
                on_progress("已重新登录" if did else "复用已有登录态")

        if on_progress:
            on_progress("正在展开菜单…")
        pages = crawl_menu(page, home_url, max_pages=max_pages, probe=probe)

        if storage_state:
            bctx.storage_state(path=storage_state)
        browser.close()

    return pages
