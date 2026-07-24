"""
批量执行：一次跑一个项目里勾选的多个页面。

关键决策：**所有页面复用同一个浏览器会话**。
之前每个配置单独 run_page 会各自启动浏览器、各自登录，
10 个页面就要登 10 次。合并后只登一次，10 个页面串行跑完。
"""
import os
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml
from playwright.sync_api import sync_playwright

from . import project as P
from . import scanner
from .login import LoginError, ensure_logged_in, is_login_page
from .models import CaseResult, PageResult, Status
from .runner import DEFAULT_SELECTORS, Context, load_config, run_case


def _log(cb, msg):
    print(msg, flush=True)
    if cb:
        cb(msg)


def scan_selected(dir_name: str, storage_state: Optional[str] = None,
                  on_log: Callable = None, overwrite: bool = False) -> Dict:
    """
    给勾选的页面批量生成配置。
    已有配置默认跳过，避免覆盖用户手工补的业务断言 —— 这点很重要，
    用户花时间补的断言不能因为重新扫描就没了。
    """
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")
    pages = P.selected_pages(dir_name)
    if not pages:
        raise ValueError("没有勾选任何页面")

    _log(on_log, f"开始扫描 {len(pages)} 个页面")
    made, skipped, failed = 0, 0, 0

    for i, pg in enumerate(pages, 1):
        name, url = pg["name"], pg.get("url")
        dest = P.page_config_path(dir_name, name)
        if not url:
            _log(on_log, f"  [{i}/{len(pages)}] {name} — 跳过（没有 URL）")
            skipped += 1
            continue
        if dest.exists() and not overwrite:
            _log(on_log, f"  [{i}/{len(pages)}] {name} — 已有配置，跳过")
            skipped += 1
            continue

        try:
            _log(on_log, f"  [{i}/{len(pages)}] {name} — 扫描中…")
            rep = scanner.scan(url, storage_state=storage_state, headless=True)
            cfg = scanner.to_config(rep, name=name)
            cfg = P.inject_login(cfg, proj)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=110),
                encoding="utf-8")
            n = len(cfg.get("cases", []))
            _log(on_log, f"      生成 {n} 条用例 → {dest.name}")
            made += 1
        except Exception as e:
            _log(on_log, f"      失败: {type(e).__name__}: {e}")
            failed += 1

    _log(on_log, f"扫描完成：新建 {made}，跳过 {skipped}，失败 {failed}")
    return {"made": made, "skipped": skipped, "failed": failed}


def run_selected(dir_name: str, out_dir: str,
                 storage_state: Optional[str] = None,
                 only_tags: Optional[List[str]] = None,
                 on_log: Callable = None) -> List[PageResult]:
    """
    执行勾选的页面。所有页面共用一个浏览器上下文，只登录一次。
    """
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")

    pages = P.selected_pages(dir_name)
    targets = []
    for pg in pages:
        cfg_path = P.page_config_path(dir_name, pg["name"])
        if cfg_path.exists():
            targets.append((pg["name"], cfg_path))
        else:
            _log(on_log, f"  跳过 {pg['name']}（还没生成配置）")

    if not targets:
        raise ValueError("勾选的页面都还没有配置，请先执行「生成用例」")

    _log(on_log, f"执行 {len(targets)} 个页面")
    results: List[PageResult] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"])
        ctx_args = {"viewport": {"width": 1600, "height": 900},
                    "accept_downloads": True, "locale": "zh-CN",
                    "ignore_https_errors": True}
        if storage_state and os.path.exists(storage_state):
            ctx_args["storage_state"] = storage_state
        bctx = browser.new_context(**ctx_args)
        bctx.set_default_timeout(30000)
        page = bctx.new_page()

        # 只登录一次，后面所有页面复用
        login_cfg = proj.get("login")
        if login_cfg:
            try:
                did = ensure_logged_in(page, proj.get("home_url") or targets[0][1], login_cfg) \
                    if proj.get("home_url") else False
                if did and storage_state:
                    bctx.storage_state(path=storage_state)
                _log(on_log, "  · 已重新登录" if did else "  · 复用已有登录态")
            except LoginError as e:
                browser.close()
                _log(on_log, f"  ✗ 登录失败: {e}")
                pr = PageResult("登录", proj.get("home_url", ""))
                pr.cases.append(CaseResult("登录", Status.ERROR, error=str(e)))
                return [pr]

        for idx, (name, cfg_path) in enumerate(targets, 1):
            t0 = time.time()
            _log(on_log, f"\n[{idx}/{len(targets)}] {name}")
            try:
                cfg = load_config(str(cfg_path))
            except Exception as e:
                _log(on_log, f"  ✗ 配置读取失败: {e}")
                pr = PageResult(name, "")
                pr.cases.append(CaseResult("配置", Status.ERROR, error=str(e)))
                results.append(pr)
                continue

            page_out = os.path.join(out_dir, f"{idx:02d}_{P._safe(name)}")
            os.makedirs(page_out, exist_ok=True)
            ctx = Context(page, cfg, page_out)
            pr = PageResult(cfg.name, cfg.url)

            cases = cfg.cases
            if only_tags:
                cases = [c for c in cases if set(c.tags) & set(only_tags)]

            for case in cases:
                cr = run_case(ctx, case)
                icon = {"pass": "✓", "fail": "✗", "error": "!", "skip": "-"}[cr.status.value]
                _log(on_log, f"  ▶ {case.name} ... {icon} ({cr.duration_ms}ms)")
                if cr.status != Status.PASS:
                    tail = cr.steps[-1].message if cr.steps else cr.error
                    if tail:
                        _log(on_log, f"     └ {tail[:160]}")
                pr.cases.append(cr)

            pr.duration_ms = int((time.time() - t0) * 1000)
            _log(on_log, f"  小计：通过 {pr.passed} / 失败 {pr.failed}")
            results.append(pr)

        if storage_state:
            bctx.storage_state(path=storage_state)
        browser.close()

    total_p = sum(r.passed for r in results)
    total_f = sum(r.failed for r in results)
    _log(on_log, f"\n全部完成：{len(results)} 个页面，通过 {total_p} / 失败 {total_f}")
    return results
