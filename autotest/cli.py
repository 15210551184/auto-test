#!/usr/bin/env python3
"""
自动化数据验证工具

用法:
  python cli.py login  <url> [--state auth.json]        人工登录一次，保存登录态
  python cli.py scan   <url> [-o configs/x.yaml]        扫描页面生成配置草稿
  python cli.py run    <config.yaml> [--headed]         执行验证
  python cli.py auto   <url>                            扫描 + 立即执行（一步到位）
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import report as R
from engine import scanner
from engine import batch, project as P
from engine.crawler import discover
from engine.login import load_dotenv
from engine.runner import load_config, run_page, save_login_state

# .env 里的账号密码。cron 环境读不到 shell 的 export，所以必须走文件。
load_dotenv(".env")


def _outdir(tag: str) -> str:
    d = os.path.join("reports", f"{datetime.now():%Y%m%d_%H%M%S}_{tag}")
    os.makedirs(d, exist_ok=True)
    return d


def cmd_login(a):
    save_login_state(a.url, a.state)


def cmd_scan(a):
    print(f"扫描 {a.url} ...")
    rep = scanner.scan(a.url, storage_state=a.state, headless=not a.headed)

    t = rep.get("table") or {}
    print(f"\n标题: {rep['title']}")
    print(f"搜索项 {len(rep['form_fields'])} 个:")
    for f in rep["form_fields"]:
        extra = f"  选项: {f['options'][:6]}" if f.get("options") else ""
        print(f"  - {f['label']} ({f['type']}){extra}")
    print(f"表格: {len(t.get('headers', []))} 列 / 当前页 {t.get('row_count', 0)} 行")
    print(f"  列: {t.get('headers', [])}")
    print(f"按钮: {[k for k, v in rep['buttons'].items() if v]}")
    print(f"列表接口: {rep.get('list_api')}")
    print(f"分页总数: {rep.get('pagination', {}).get('total')}")

    cfg = scanner.to_config(rep, name=a.name)
    scanner.write_config(cfg, a.out)
    print(f"\n生成 {len(cfg['cases'])} 条用例 -> {a.out}")
    print("请检查配置，重点补充：接口字段映射、新增/修改的表单字段、业务规则断言")


def cmd_test_login(a):
    """单独验证账号密码能不能登进去，不跑用例"""
    from playwright.sync_api import sync_playwright
    from engine.login import ensure_logged_in, LoginError

    cfg = load_config(a.config)
    if not cfg.login:
        print("配置里没有 login 段，无需测试")
        return
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not a.headed,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        args = {"viewport": {"width": 1600, "height": 900}, "locale": "zh-CN"}
        if os.path.exists(a.state):
            args["storage_state"] = a.state
        bctx = browser.new_context(**args)
        page = bctx.new_page()
        try:
            did = ensure_logged_in(page, cfg.url, cfg.login)
            print("✓ 重新登录成功" if did else "✓ 已有登录态仍然有效")
            bctx.storage_state(path=a.state)
            print(f"  当前地址: {page.url}")
            print(f"  登录态已保存: {a.state}")
        except LoginError as e:
            print(f"✗ 登录失败:\n{e}")
            page.screenshot(path="login_fail.png")
            print("  失败截图: login_fail.png")
            sys.exit(1)
        finally:
            browser.close()


def cmd_menu(a):
    """爬取系统菜单，写回项目"""
    proj = P.load_project(a.project)
    if not proj:
        print(f"项目不存在: {a.project}"); sys.exit(1)
    print(f"扫描菜单: {proj.get('home_url')}")
    pages = discover(proj["home_url"], login_cfg=proj.get("login"),
                     storage_state=a.state, max_pages=a.max,
                     on_progress=lambda m: print(f"  {m}", flush=True))
    n = P.update_pages(a.project, pages)
    rec = sum(1 for p in pages if p.get("recommended"))
    print(f"\n发现 {n} 个页面，其中 {rec} 个是数据列表页（已默认勾选）")
    for pg in pages:
        mark = "✓" if pg.get("recommended") else " "
        extra = []
        if pg.get("columns"): extra.append(f"{pg['columns']}列")
        if pg.get("has_export"): extra.append("可导出")
        if pg.get("has_create"): extra.append("可新增")
        tail = f"  [{', '.join(extra)}]" if extra else ""
        grp = f"{pg['group']} / " if pg.get("group") else ""
        print(f"  {mark} {grp}{pg['name']}{tail}")


def cmd_batch_scan(a):
    """给勾选的页面批量生成配置"""
    batch.scan_selected(a.project, storage_state=a.state, overwrite=a.overwrite)


def cmd_batch_run(a):
    """批量执行勾选的页面"""
    out = _outdir(P._safe(a.project))
    results = batch.run_selected(a.project, out, storage_state=a.state,
                                 only_tags=a.tags)
    path = R.render(results, os.path.join(out, "report.html"))
    R.render_json(results, os.path.join(out, "result.json"))
    print(f"报告: {os.path.abspath(path)}")
    sys.exit(1 if sum(r.failed for r in results) else 0)


def cmd_run(a):
    cfg = load_config(a.config)
    out = _outdir(os.path.basename(a.config).replace(".yaml", ""))
    print(f"执行 {cfg.name} ({len(cfg.cases)} 条用例)")
    res = run_page(cfg, out, headless=not a.headed, storage_state=a.state,
                   slow_mo=a.slow, only_tags=a.tags)
    path = R.render([res], os.path.join(out, "report.html"))
    R.render_json([res], os.path.join(out, "result.json"))
    print(f"\n通过 {res.passed} / 失败 {res.failed} / 共 {len(res.cases)}")
    print(f"报告: {os.path.abspath(path)}")
    sys.exit(1 if res.failed else 0)


def cmd_auto(a):
    print(f"扫描 {a.url} ...")
    rep = scanner.scan(a.url, storage_state=a.state, headless=True)
    cfg_dict = scanner.to_config(rep, name=a.name)
    out = _outdir("auto")
    cfg_path = os.path.join(out, "generated.yaml")
    scanner.write_config(cfg_dict, cfg_path)
    print(f"生成 {len(cfg_dict['cases'])} 条用例 -> {cfg_path}\n")

    cfg = load_config(cfg_path)
    res = run_page(cfg, out, headless=not a.headed, storage_state=a.state)
    path = R.render([res], os.path.join(out, "report.html"))
    R.render_json([res], os.path.join(out, "result.json"))
    print(f"\n通过 {res.passed} / 失败 {res.failed} / 共 {len(res.cases)}")
    print(f"报告: {os.path.abspath(path)}")


def main():
    p = argparse.ArgumentParser(description="页面自动化数据验证工具")
    p.add_argument("--state", default="auth.json", help="登录态文件")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("login", help="人工登录并保存登录态")
    s1.add_argument("url")
    s1.set_defaults(func=cmd_login)

    s2 = sub.add_parser("scan", help="扫描页面生成配置")
    s2.add_argument("url")
    s2.add_argument("-o", "--out", default="configs/generated.yaml")
    s2.add_argument("-n", "--name")
    s2.add_argument("--headed", action="store_true")
    s2.set_defaults(func=cmd_scan)

    s3 = sub.add_parser("run", help="执行配置")
    s3.add_argument("config")
    s3.add_argument("--headed", action="store_true")
    s3.add_argument("--slow", type=int, default=0, help="每步减速 ms，调试用")
    s3.add_argument("--tags", nargs="*", help="只跑指定 tag")
    s3.set_defaults(func=cmd_run)

    s6 = sub.add_parser("menu", help="爬取系统菜单")
    s6.add_argument("project")
    s6.add_argument("--max", type=int, default=60)
    s6.set_defaults(func=cmd_menu)

    s7 = sub.add_parser("batch-scan", help="给勾选页面批量生成配置")
    s7.add_argument("project")
    s7.add_argument("--overwrite", action="store_true", help="覆盖已有配置")
    s7.set_defaults(func=cmd_batch_scan)

    s8 = sub.add_parser("batch-run", help="批量执行勾选页面")
    s8.add_argument("project")
    s8.add_argument("--tags", nargs="*")
    s8.set_defaults(func=cmd_batch_run)

    s5 = sub.add_parser("test-login", help="只验证账号密码能否登录")
    s5.add_argument("config")
    s5.add_argument("--headed", action="store_true")
    s5.set_defaults(func=cmd_test_login)

    s4 = sub.add_parser("auto", help="扫描并立即执行")
    s4.add_argument("url")
    s4.add_argument("-n", "--name")
    s4.add_argument("--headed", action="store_true")
    s4.set_defaults(func=cmd_auto)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
