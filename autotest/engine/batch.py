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
from .runner import Context, filter_cases_by_tags, load_config, run_case
from .state import save_storage_state, valid_storage_state

# 单页扫描的硬超时。scanner.scan() 内部各步骤都有超时，但加总起来（多个下拉框
# 逐个探测选项、页面本身卡死等）仍可能远超预期；真出现过一个带地图/大量级联
# 下拉的页面把整批扫描拖死在原地、既不报错也不失败的情况。这里兜底一刀切断。
SCAN_TIMEOUT_SEC = 150

# 项目配了多语言时，scan() 会为每种语言额外切一次、重新扫一遍表单 label /
# 表头 / 新增弹窗字段（见 scanner.scan_language_variants()），纯 CRUD 页面
# 也会因为这部分多出好几十秒——SCAN_TIMEOUT_SEC 是按"不带多语言"的普通页面
# 定的，配了语言就该按语言数量给扫描按比例多留时间，而不是让所有页面共用
# 同一个不考虑这部分开销的固定上限。
LANG_SCAN_BUDGET_SEC = 30

# 并发跑几个页面。开的 Chromium 标签页越多，服务器内存占用越高——这个项目
# 之前特意把整个控制台限制成"同一时刻只跑一个任务"就是因为内存吃紧，2 是
# 一个相对稳的默认值，服务器内存宽裕再考虑调大。
DEFAULT_CONCURRENCY = 2

# 并发起步时错峰的间隔（秒）。几个 worker 提交后几乎同时登录、同时点第一次
# 搜索，会在最开始几秒钟形成一次并发高峰，把目标后端的响应瞬时拖慢——
# 让排在后面的 worker 晚一点点开始，把这个起步高峰削掉，跑到中途各 worker
# 进度自然错开，不需要一直错峰。
STAGGER_DELAY_SEC = 2


def _log(cb, msg):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _scan_timeout_for(languages: Optional[Dict], base: int = SCAN_TIMEOUT_SEC) -> int:
    """
    配了几种语言，就多给几份 LANG_SCAN_BUDGET_SEC——scan_language_variants()
    会为每种语言切一次、重新扫一遍表单/表头/弹窗字段，这部分开销跟语言数量
    成正比，超时预算也该跟着成正比，不能让所有页面（不管配没配多语言）
    共用同一个只按"普通页面"估的固定上限。
    """
    lang_count = len((languages or {}).get("options") or {})
    return base + lang_count * LANG_SCAN_BUDGET_SEC


def _scan_with_timeout(url: str, storage_state: Optional[str], timeout: int = SCAN_TIMEOUT_SEC,
                       languages: Optional[Dict] = None, login: Optional[Dict] = None) -> Dict:
    """
    在子线程里跑 scanner.scan()，超时就放弃等待、把这一页判失败，不拖死整批任务。

    子线程用 daemon=True：如果目标页面真的把浏览器渲染进程卡死，这个线程会
    一直卡在里面出不来，但作为 daemon 线程它不会阻止批量扫描继续跑下一页，
    也不会阻止整个 batch-scan 进程最终退出（进程退出时 daemon 线程被直接终止，
    残留的 Chromium 子进程通常会随驱动连接断开一并退出）。
    """
    effective_timeout = _scan_timeout_for(languages, timeout)
    box: Dict = {}

    def worker():
        try:
            box["report"] = scanner.scan(url, storage_state=storage_state, headless=True,
                                         languages=languages, login=login)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(effective_timeout)
    if t.is_alive():
        raise TimeoutError(f"扫描超过 {effective_timeout}s 未完成，页面可能卡死（地图/弹窗/死循环等），已跳过")
    if "error" in box:
        raise box["error"]
    return box["report"]


def scan_selected(dir_name: str, storage_state: Optional[str] = None,
                  on_log: Callable = None, overwrite: bool = False,
                  concurrency: int = DEFAULT_CONCURRENCY) -> Dict:
    """
    给勾选的页面批量生成配置，最多 concurrency 个页面并发扫描。
    已有配置默认跳过，避免覆盖用户手工补的业务断言 —— 这点很重要，
    用户花时间补的断言不能因为重新扫描就没了。

    并发跟批量执行（run_selected）不一样的地方：扫描不需要"先登录一次共享
    cookie"那套——scanner.scan() 本身只读 storage_state 文件（不主动登录、
    不回写），多个 worker 同时读同一份 cookie 文件没有写冲突，直接各自起
    独立的 sync_playwright() 会话并发跑就行，不用额外协调登录。
    """
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")
    pages = P.selected_pages(dir_name)
    if not pages:
        raise ValueError("没有勾选任何页面")

    concurrency = max(1, min(concurrency, len(pages)))
    _log(on_log, f"开始扫描 {len(pages)} 个页面（并发 {concurrency}）")
    counters = {"made": 0, "skipped": 0, "failed": 0, "done": 0}
    lock = threading.Lock()

    def scan_one(i: int, pg: Dict) -> None:
        name, url = pg["name"], pg.get("url")
        tag = f"[{name}]"
        if i <= concurrency:
            time.sleep((i - 1) * STAGGER_DELAY_SEC)   # 错开并发起步的高峰，理由同 run_selected
        dest = P.page_config_path(dir_name, name)

        def finish(kind: str) -> None:
            with lock:
                counters[kind] += 1
                counters["done"] += 1
                progress.emit(phase="scan", page=counters["done"], pages=len(pages), page_name=name)

        if not url:
            _log(on_log, f"{tag} 跳过（没有 URL）")
            finish("skipped")
            return
        if dest.exists() and not overwrite:
            _log(on_log, f"{tag} 已有配置，跳过")
            finish("skipped")
            return

        try:
            _log(on_log, f"{tag} 扫描中…")
            # 个别页面（大量级联下拉、地图选点、"新增"弹窗字段特别多）扫描
            # 本来就比一般页面慢，不该为了它们把所有页面的超时都调大——
            # 项目设置里给这一页单独加 scan_timeout（秒）就行，不给就用
            # 全局默认的 SCAN_TIMEOUT_SEC。
            page_timeout = pg.get("scan_timeout") or SCAN_TIMEOUT_SEC
            rep = _scan_with_timeout(url, storage_state, timeout=page_timeout,
                                     languages=proj.get("languages"),
                                     login=proj.get("login"))
            cfg = scanner.to_config(rep, name=name, languages=proj.get("languages"))
            cfg = P.inject_project_settings(cfg, proj)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=110),
                encoding="utf-8")
            n = len(cfg.get("cases", []))
            _log(on_log, f"{tag} 生成 {n} 条用例 → {dest.name}")
            finish("made")
        except Exception as e:
            _log(on_log, f"{tag} 失败: {type(e).__name__}: {e}")
            finish("failed")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(scan_one, i, pg) for i, pg in enumerate(pages, 1)]
        for f in as_completed(futures):
            f.result()   # worker 内部没兜住的异常在这里重新抛出，不悄悄吞掉

    _log(on_log, f"扫描完成：新建 {counters['made']}，跳过 {counters['skipped']}，失败 {counters['failed']}")
    return {"made": counters["made"], "skipped": counters["skipped"], "failed": counters["failed"]}


def run_selected(dir_name: str, out_dir: str,
                 storage_state: Optional[str] = None,
                 only_tags: Optional[List[str]] = None,
                 exclude_tags: Optional[List[str]] = None,
                 on_log: Callable = None,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 target_language: Optional[str] = None) -> List[PageResult]:
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
        # 只错峰第一批并发起步的 worker（idx < concurrency）——排在后面的
        # worker 本来就要等前面某个 worker 跑完才轮到，天然已经错开了，
        # 不需要再额外等。
        if idx < concurrency:
            time.sleep(idx * STAGGER_DELAY_SEC)
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
            # report_root=out_dir：截图存在 page_out（每个页面自己的子目录）里，
            # 但汇总的 report.html 写在 out_dir 顶层——截图相对路径必须相对
            # report.html 的位置算，不然报告里的图全部裂掉（用户看不到失败截图）。
            ctx = Context(page, cfg, page_out, target_language=target_language, report_root=out_dir)
            pr = PageResult(cfg.name, cfg.url)

            cases = filter_cases_by_tags(cfg.cases, only_tags, exclude_tags)

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
