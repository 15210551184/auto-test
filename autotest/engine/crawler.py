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
import json
import re
import time
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from . import browser as B
from . import progress
from .i18n_terms import words as _i18n_words
from .login import ensure_logged_in, is_login_page
from .state import save_storage_state, valid_storage_state

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
SKIP_KEYWORDS = _i18n_words("logout") + [
    "个人中心", "修改密码", "帮助", "Profile", "Change Password", "Help",
]


def _text(loc) -> str:
    try:
        return re.sub(r"\s+", " ", loc.inner_text()).strip()
    except Exception:
        return ""


def _progress(callback, message: str) -> None:
    if callback:
        callback(message)


def _menu_route(item) -> Optional[str]:
    """读取菜单组件的路由 index；Vue 2 的 prop 不一定会渲染为 DOM 属性。"""
    for attr in ("index", "data-index", "href"):
        try:
            value = item.get_attribute(attr)
            if value and value not in ("#", "javascript:;"):
                return value
        except Exception:
            continue
    try:
        value = item.evaluate("""el => {
          const vm = el.__vue__;
          return vm && (vm.index || (vm.$props && vm.$props.index));
        }""")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def _route_url(home_url: str, route: Optional[str]) -> Optional[str]:
    """将菜单路由转换为同一单页应用下的完整 URL。"""
    if not route or route.startswith(("javascript:", "#")):
        return None
    home = urlparse(home_url)
    if route.startswith(("http://", "https://")):
        target = urlparse(route)
        return route if target.netloc == home.netloc else None
    prefix = home.path.rsplit("/", 1)[0].rstrip("/")
    path = route if route.startswith(prefix + "/") else f"{prefix}/{route.lstrip('/')}"
    return f"{home.scheme}://{home.netloc}{path}"


def _collect_visible_leaves(page: Page, leaves: List[Dict], seen: Set[str]) -> int:
    """收集当前展开分支的叶子菜单。"""
    added = 0
    for sel in LEAF_SELECTORS:
        items = page.locator(sel)
        for i in range(items.count()):
            item = items.nth(i)
            try:
                if not item.is_visible():
                    continue
            except Exception:
                continue
            name, group = _text(item), _menu_path(item)
            key = f"{group}\0{name}"
            if not name or key in seen or any(k in name for k in SKIP_KEYWORDS):
                continue
            seen.add(key)
            leaves.append({"name": name, "group": group,
                           "menu_index": item.get_attribute("index") or item.get_attribute("data-index"),
                           "menu_route": _menu_route(item),
                           "parent_indexes": _menu_indexes(item)})
            added += 1
        if added:
            break
    return added


def _collect_all_leaves(page: Page, on_progress=None) -> List[Dict]:
    """逐个打开分支并立即采集，兼容手风琴菜单。"""
    leaves: List[Dict] = []
    seen_leaves: Set[str] = set()
    opened: Set[str] = set()
    _collect_visible_leaves(page, leaves, seen_leaves)
    while True:
        candidate = None
        for sel in SUBMENU_SELECTORS:
            titles = page.locator(sel)
            for i in range(titles.count()):
                title = titles.nth(i)
                try:
                    name = _text(title)
                    if title.is_visible() and name and name not in opened:
                        candidate = (title, name)
                        break
                except Exception:
                    continue
            if candidate:
                break
        if not candidate:
            return leaves
        title, name = candidate
        opened.add(name)
        _progress(on_progress, f"展开菜单 {len(opened)}：{name}")
        try:
            title.click(timeout=3000)
            page.wait_for_timeout(250)
            added = _collect_visible_leaves(page, leaves, seen_leaves)
            _progress(on_progress, f"  已发现 {len(leaves)} 个页面" + (f"（本分支新增 {added}）" if added else ""))
        except Exception as exc:
            _progress(on_progress, f"  跳过菜单 {name}：{type(exc).__name__}")


def _menu_indexes(leaf) -> List[str]:
    """读取 Element UI/Ant 菜单祖先的稳定 index 属性。"""
    try:
        ancestors = leaf.locator(
            "xpath=ancestor::*[contains(@class,'el-submenu') or "
            "contains(@class,'el-sub-menu') or contains(@class,'ant-menu-submenu')]")
        return [idx for i in range(ancestors.count())
                if (idx := (ancestors.nth(i).get_attribute("index") or
                            ancestors.nth(i).get_attribute("data-index")))]
    except Exception:
        return []


def _open_menu_path(page: Page, group: str, parent_indexes: Optional[List[str]] = None) -> None:
    """在重新加载首页后按层级重新展开目标叶子所在的分支。"""
    if parent_indexes:
        opened = 0
        for index in parent_indexes:
            owner = page.locator(f"[index={json.dumps(index)}], [data-index={json.dumps(index)}]")
            title = owner.locator(",".join(SUBMENU_SELECTORS)).first
            try:
                if title.count() and title.is_visible():
                    title.click(timeout=3000)
                    page.wait_for_timeout(220)
                    opened += 1
                    continue
            except Exception:
                pass
        if opened == len(parent_indexes):
            return
    for name in filter(None, group.split(" / ")):
        title = page.locator(",".join(SUBMENU_SELECTORS)).filter(
            has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"))
        for i in range(title.count()):
            item = title.nth(i)
            try:
                if item.is_visible():
                    item.click(timeout=3000)
                    page.wait_for_timeout(220)
                    break
            except Exception:
                continue


def _find_visible_menu_item(page: Page, name: str):
    """在手风琴菜单中逐分支展开，返回可见的目标菜单项。"""
    exact = re.compile(rf"^\s*{re.escape(name)}\s*$")

    def find_leaf():
        items = page.locator(",".join(LEAF_SELECTORS)).filter(has_text=exact)
        for i in range(items.count()):
            item = items.nth(i)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
        return None

    item = find_leaf()
    if item:
        return item
    opened: Set[str] = set()
    while True:
        candidate = None
        for selector in SUBMENU_SELECTORS:
            titles = page.locator(selector)
            for i in range(titles.count()):
                title = titles.nth(i)
                try:
                    key = _text(title)
                    if key and key not in opened and title.is_visible():
                        candidate = (title, key)
                        break
                except Exception:
                    continue
            if candidate:
                break
        if not candidate:
            return None
        title, key = candidate
        opened.add(key)
        try:
            title.click(timeout=3000)
            page.wait_for_timeout(250)
        except Exception:
            continue
        item = find_leaf()
        if item:
            return item


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
               probe: bool = True, on_progress=None) -> List[Dict]:
    """
    爬取菜单，返回页面列表。

    probe=True 时会逐个点击菜单、记录跳转后的 URL，并粗略判断
    该页是否是「带表格的列表页」（值得测的那种）。
    这一步慢但值得——否则用户拿到一堆没有表格的页面也没法测。
    """
    page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    # 收集阶段总数未知，发一条不定量进度让界面有反馈
    progress.emit(phase="menu", page_name="正在展开菜单树…")
    leaves = _collect_all_leaves(page, on_progress)
    _progress(on_progress, f"菜单收集完成，共 {len(leaves)} 个候选页面；开始逐页探测")

    if not probe:
        return [dict(l, url="", has_table=None) for l in leaves][:max_pages]

    # 逐个点击，记录 URL
    out = []
    seen_url: Set[str] = set()
    total = min(len(leaves), max_pages)
    for index, leaf in enumerate(leaves[:max_pages], 1):
        name = leaf["name"]
        progress.emit(phase="menu", page=index, pages=total, page_name=name)
        _progress(on_progress, f"探测页面 {index}/{total}：{name}")
        try:
            direct_url = _route_url(home_url, leaf.get("menu_route"))
            if direct_url:
                _progress(on_progress, f"  通过菜单路由访问：{direct_url}")
                page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1200)
                url = page.url
                if is_login_page(page):
                    _progress(on_progress, "  跳过：会话已回到登录页")
                    continue
                key = url.split("?")[0]
                if key in seen_url:
                    _progress(on_progress, "  跳过：与已发现页面 URL 重复")
                    continue
                seen_url.add(key)
                info = _probe_page(page)
                out.append({"name": name, "group": leaf["group"], "url": url, **info})
                _progress(on_progress, f"  完成：已保留 {len(out)} 个页面")
                continue

            page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(900)
            _open_menu_path(page, leaf["group"], leaf.get("parent_indexes"))

            target = page.locator(f"[index={json.dumps(leaf['menu_index'])}], "
                                  f"[data-index={json.dumps(leaf['menu_index'])}]") \
                if leaf.get("menu_index") else page.locator(f"{','.join(LEAF_SELECTORS)}").filter(
                    has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"))
            if target.count() == 0:
                target = page.get_by_text(name, exact=True)
            try:
                target_visible = target.count() > 0 and target.first.is_visible()
            except Exception:
                target_visible = False
            if not target_visible:
                _progress(on_progress, "  路径定位失败，正在遍历菜单分支…")
                target = _find_visible_menu_item(page, name)
                if target is None:
                    _progress(on_progress, "  跳过：找不到菜单项")
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
                    _progress(on_progress, "  跳过：菜单未跳转")
                    continue
            if is_login_page(page):
                _progress(on_progress, "  跳过：会话已回到登录页")
                continue
            # 去掉 hash 里的随机参数，避免同一页面重复
            key = url.split("?")[0]
            if key in seen_url:
                _progress(on_progress, "  跳过：与已发现页面 URL 重复")
                continue
            seen_url.add(key)

            info = _probe_page(page)
            out.append({
                "name": name,
                "group": leaf["group"],
                "url": url,
                **info,
            })
            _progress(on_progress, f"  完成：已保留 {len(out)} 个页面")
        except Exception as exc:
            _progress(on_progress, f"  跳过：{type(exc).__name__}")
            continue

    return out


def _probe_page(page: Page) -> Dict:
    """粗略判断这一页值不值得测"""
    table = None
    try:
        candidates = page.locator(".el-table, .ant-table, table.data-table, table")
        for i in range(candidates.count()):
            candidate = candidates.nth(i)
            headers = candidate.locator(
                ".el-table__header-wrapper th .cell, .ant-table-thead th, thead th").all_inner_texts()
            if candidate.is_visible() and any(h.strip() for h in headers):
                table = candidate
                break
    except Exception:
        table = None
    has_table = table is not None

    cols, rows = 0, 0
    if has_table:
        try:
            cols = len([t for t in table.locator(
                ".el-table__header-wrapper th .cell, .ant-table-thead th, thead th"
            ).all_inner_texts() if t.strip()])
            rows = table.locator(
                ".el-table__body-wrapper tbody tr.el-table__row, .ant-table-tbody tr, tbody tr"
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
        "has_search": has_btn(*_i18n_words("search")),
        "has_export": has_btn(*_i18n_words("export")),
        "has_create": has_btn(*_i18n_words("create")),
        "has_edit": has_btn(*_i18n_words("edit")),
        "has_delete": has_btn(*_i18n_words("delete")),
        "has_batch": has_btn(*_i18n_words("batch")),
        # 有表格 + 有搜索 = 典型的数据列表页，最值得测
        "recommended": bool(has_table and cols >= 2),
    }


def discover(home_url: str, login_cfg: Optional[dict] = None,
             storage_state: Optional[str] = None,
             max_pages: int = 60, probe: bool = True,
             on_progress=None) -> List[Dict]:
    """对外入口：登录 → 爬菜单 → 返回页面列表"""
    with sync_playwright() as pw:
        browser = B.launch(pw, headless=True)
        args = B.context_args()
        state = valid_storage_state(storage_state, on_progress)
        if state:
            args["storage_state"] = state
        bctx = browser.new_context(**args)
        bctx.set_default_timeout(20000)
        page = bctx.new_page()

        if login_cfg:
            did = ensure_logged_in(page, home_url, login_cfg)
            if did:
                save_storage_state(bctx, storage_state, on_progress)
            if on_progress:
                on_progress("已重新登录" if did else "复用已有登录态")

        if on_progress:
            on_progress("正在逐分支展开菜单…")
        pages = crawl_menu(page, home_url, max_pages=max_pages, probe=probe,
                           on_progress=on_progress)

        save_storage_state(bctx, storage_state, on_progress)
        browser.close()

    return pages
