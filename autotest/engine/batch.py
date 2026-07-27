"""
批量执行：一次跑一个项目里勾选的多个页面。

关键决策：**所有 worker 复用同一份登录态，但各自起独立的浏览器进程并发跑**。
之前每个配置单独 run_page 会各自启动浏览器、各自登录，10 个页面登 10 次；
后来改成全部页面共用一个浏览器串行跑，登录只用 1 次，但页面之间完全排队，
页面一多总耗时就很难看。

现在的做法：先登录一次拿到 cookie（存成内存里的 dict），之后最多
concurrency 个并发 worker 各自起一个完整的 sync_playwright() 会话（各自的
Chromium 进程），加载同一份 cookie 跑不同页面——不是并发登录（很多系统会
把老会话踢掉或触发风控），只是把已经建立好的会话复用到多个进程，跟同一
账号在好几台设备上分别打开已登录的浏览器是一回事。

**为什么每个 worker 要起独立浏览器进程，不能共用一个 browser 开多个 tab**：
Playwright 的同步 API 不支持跨线程共享同一个 Playwright/Browser 实例——
一个 sync_playwright() 上下文创建出的对象只能在创建它的那个线程里驱动，
多线程各自调用同一个 browser 会出错。所以并发的代价是内存换成了"多开
Chromium 进程"而不是"一个进程开多个 tab"，concurrency 调大之前务必确认
服务器内存够用。
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml
from playwright.sync_api import sync_playwright

from . import browser as B
from . import progress
from . import project as P
from . import scanner
from .login import LoginError, ensure_logged_in
from .models import CaseResult, PageResult, Status
from .runner import Context, load_config, run_case
from .state import save_storage_state, valid_storage_state

# 单页扫描的硬超时。scanner.scan() 内部各步骤都有超时，但加总起来（多个下拉框
# 逐个探测选项、页面本身卡死等）仍可能远超预期；真出现过一个带地图/大量级联
# 下拉的页面把整批扫描拖死在原地、既不报错也不失败的情况。这里兜底一刀切断。
SCAN_TIMEOUT_SEC = 150

# 并发跑几个页面。开的 Chromium 标签页越多，服务器内存占用越高——这个项目
# 之前特意把整个控制台限制成"同一时刻只跑一个任务"就是因为内存吃紧，2 是
# 一个相对稳的默认值，服务器内存宽裕再考虑调大。
DEFAULT_CONCURRENCY = 2


def _log(cb, msg):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _scan_with_timeout(url: str, storage_state: Optional[str], timeout: int = SCAN_TIMEOUT_SEC) -> Dict:
    """
    在子线程里跑 scanner.scan()，超时就放弃等待、把这一页判失败，不拖死整批任务。

    子线程用 daemon=True：如果目标页面真的把浏览器渲染进程卡死，这个线程会
    一直卡在里面出不来，但作为 daemon 线程它不会阻止批量扫描继续跑下一页，
    也不会阻止整个 batch-scan 进程最终退出（进程退出时 daemon 线程被直接终止，
    残留的 Chromium 子进程通常会随驱动连接断开一并退出）。
    """
    box: Dict = {}

    def worker():
        try:
            box["report"] = scanner.scan(url, storage_state=storage_state, headless=True)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"扫描超过 {timeout}s 未完成，页面可能卡死（地图/弹窗/死循环等），已跳过")
    if "error" in box:
        raise box["error"]
    return box["report"]


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
        progress.emit(phase="scan", page=i, pages=len(pages), page_name=name)
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
            rep = _scan_with_timeout(url, storage_state)
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
                 on_log: Callable = None,
                 concurrency: int = DEFAULT_CONCURRENCY) -> List[PageResult]:
    """
    执行勾选的页面，最多 concurrency 个页面并发跑（见模块头部说明）。

    每条日志都会带 [页面名] 前缀，前端按这个前缀把日志分到各页面自己的
    标签页里——并发跑的时候几个页面的日志会交替出现，没有前缀会看不清
    哪行是哪个页面的。
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

    concurrency = max(1, min(concurrency, len(targets)))
    _log(on_log, f"执行 {len(targets)} 个页面（并发 {concurrency}）")

    base_ctx_args = B.context_args(accept_downloads=True)
    state = valid_storage_state(storage_state, on_log)
    if state:
        base_ctx_args["storage_state"] = state

    # 独立起一个一次性的 Playwright 会话专门用来登录、拿 cookie；这个 browser
    # 用完即关，不会留给下面的并发 worker（同步 API 不能跨线程共享同一个
    # browser，见模块头部说明）。
    with sync_playwright() as pw:
        login_browser = B.launch(pw, headless=True)
        login_bctx = login_browser.new_context(**base_ctx_args)
        login_bctx.set_default_timeout(30000)
        login_page = login_bctx.new_page()

        login_cfg = proj.get("login")
        if login_cfg:
            try:
                did = ensure_logged_in(login_page, proj.get("home_url") or str(targets[0][1]), login_cfg) \
                    if proj.get("home_url") else False
                if did:
                    save_storage_state(login_bctx, storage_state, on_log)
                _log(on_log, "  · 已重新登录" if did else "  · 复用已有登录态")
            except LoginError as e:
                login_browser.close()
                _log(on_log, f"  ✗ 登录失败: {e}")
                pr = PageResult("登录", proj.get("home_url", ""))
                pr.cases.append(CaseResult("登录", Status.ERROR, error=str(e)))
                return [pr]
        # 登录后的 cookie 存成内存对象，每个并发 worker 各自的浏览器进程
        # 加载它复用会话，不再重新走一遍登录流程
        shared_state = login_bctx.storage_state()
        login_browser.close()

    results: List[Optional[PageResult]] = [None] * len(targets)
    lock = threading.Lock()
    counters = {"done": 0, "passed": 0, "failed": 0}

    def run_one(idx: int, name: str, cfg_path: Path) -> None:
        tag = f"[{name}]"
        t0 = time.time()
        _log(on_log, f"\n{tag} 开始")
        try:
            cfg = load_config(str(cfg_path))
        except Exception as e:
            _log(on_log, f"{tag} ✗ 配置读取失败: {e}")
            pr = PageResult(name, "")
            pr.cases.append(CaseResult("配置", Status.ERROR, error=str(e)))
            results[idx] = pr
            with lock:
                counters["done"] += 1
                counters["failed"] += 1
                progress.emit(phase="run", page=counters["done"], pages=len(targets),
                              passed=counters["passed"], failed=counters["failed"])
            return

        # 每个 worker 线程独立起一个完整的 Playwright 会话（自己的 Chromium
        # 进程），只共享上面登录拿到的 cookie（普通 dict，线程间只读安全）
        with sync_playwright() as pw:
            browser = B.launch(pw, headless=True)
            worker_ctx_args = dict(base_ctx_args)
            worker_ctx_args["storage_state"] = shared_state
            worker_bctx = browser.new_context(**worker_ctx_args)
            worker_bctx.set_default_timeout(30000)
            page = worker_bctx.new_page()

            page_out = os.path.join(out_dir, f"{idx + 1:02d}_{P._safe(name)}")
            os.makedirs(page_out, exist_ok=True)
            ctx = Context(page, cfg, page_out)
            pr = PageResult(cfg.name, cfg.url)

            cases = cfg.cases
            if only_tags:
                cases = [c for c in cases if set(c.tags) & set(only_tags)]

            for case in cases:
                cr = run_case(ctx, case)
                icon = {"pass": "✓", "warn": "⚠", "fail": "✗", "error": "!", "skip": "-"}[cr.status.value]
                _log(on_log, f"{tag} ▶ {case.name} ... {icon} ({cr.duration_ms}ms)")
                if cr.status not in (Status.PASS, Status.WARN):
                    tail = cr.steps[-1].message if cr.steps else cr.error
                    if tail:
                        _log(on_log, f"{tag}    └ {tail[:160]}")
                pr.cases.append(cr)

            pr.duration_ms = int((time.time() - t0) * 1000)
            _log(on_log, f"{tag} 小计：通过 {pr.passed} / 失败 {pr.failed}")
            results[idx] = pr
            browser.close()

        with lock:
            counters["done"] += 1
            counters["passed"] += pr.passed
            counters["failed"] += pr.failed
            progress.emit(phase="run", page=counters["done"], pages=len(targets),
                          passed=counters["passed"], failed=counters["failed"])

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(run_one, i, name, path) for i, (name, path) in enumerate(targets)]
        for f in as_completed(futures):
            f.result()   # worker 内部没兜住的异常在这里重新抛出，不悄悄吞掉

    final_results = [r for r in results if r is not None]
    total_p = sum(r.passed for r in final_results)
    total_f = sum(r.failed for r in final_results)
    _log(on_log, f"\n全部完成：{len(final_results)} 个页面，通过 {total_p} / 失败 {total_f}")
    return final_results
