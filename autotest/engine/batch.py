"""
批量执行：一次跑一个项目里勾选的多个页面。

关键决策：**固定大小的浏览器池复用登录态和 Chromium 进程**。
之前每个配置单独 run_page 会各自启动浏览器、各自登录，10 个页面登 10 次；
后来改成全部页面共用一个浏览器串行跑，登录只用 1 次，但页面之间完全排队，
页面一多总耗时就很难看。

现在的做法：先登录一次拿到 cookie（存成内存里的 dict），之后启动固定的
concurrency 个 worker。每个 worker 只启动一次 Playwright/Chromium，并连续
处理分配给它的多个页面；页面之间只重建轻量 BrowserContext 做状态隔离。

**为什么每个 worker 要起独立浏览器进程，不能共用一个 browser 开多个 tab**：
Playwright 的同步 API 不支持跨线程共享同一个 Playwright/Browser 实例——
一个 sync_playwright() 上下文创建出的对象只能在创建它的那个线程里驱动，
多线程各自调用同一个 browser 会出错。所以池中每个线程仍有自己的 Chromium，
但数量固定为 concurrency，而不再随着页面数量反复启动和退出。
"""
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from playwright.sync_api import sync_playwright

from . import browser as B
from . import cancellation
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
# 2 核 2G 上两个完整 Chromium 会争抢 CPU/内存，实测常出现“并发反而更慢”
# 和超时。默认串行最稳；4 核 8G 以上仍可通过 --concurrency 2 手工调大。
DEFAULT_CONCURRENCY = 1

# 并发起步时错峰的间隔（秒）。几个 worker 提交后几乎同时登录、同时点第一次
# 搜索，会在最开始几秒钟形成一次并发高峰，把目标后端的响应瞬时拖慢——
# 让排在后面的 worker 晚一点点开始，把这个起步高峰削掉，跑到中途各 worker
# 进度自然错开，不需要一直错峰。
STAGGER_DELAY_SEC = 2

# 一条用例可能在等导出文件或慢接口，旧逻辑直到用例结束才打印任何内容，
# 看起来像任务彻底卡死。定期打印心跳，让日志明确显示仍在执行哪条用例。
RUN_HEARTBEAT_SEC = 30
# 单条用例的兜底预算。正常动作本身通常 5~45 秒内有明确超时；这个上限
# 防止异常页面长期占住 worker，同时不会像“整页 60 秒”那样误杀有十几条
# 用例的正常页面。
CASE_RUN_TIMEOUT_SEC = 150

# 修改扫描报告结构或 scanner 的识别语义时递增，使旧缓存自动失效。
SCAN_CACHE_VERSION = 7

def _log(cb, msg):
    print(msg, flush=True)
    if cb:
        cb(msg)


def _record_page_completion(counters: Dict[str, int], task_states: Dict[str, str],
                            name: str, result: PageResult) -> None:
    """按页面更新实时进度，不能把页面里的用例数量混进页面统计。"""
    counters["done"] += 1
    page_failed = bool(result.failed)
    counters["failed" if page_failed else "passed"] += 1
    task_states[name] = "failed" if page_failed else "passed"


def _partition_targets(targets, workers: int):
    """按 worker 数量轮询分组；每组由一个常驻 Chromium 顺序处理。"""
    groups = [[] for _ in range(max(1, workers))]
    for index, item in enumerate(targets):
        groups[index % len(groups)].append((index, *item))
    return [group for group in groups if group]


def _scan_timeout_for(languages: Optional[Dict], base: int = SCAN_TIMEOUT_SEC) -> int:
    """
    实际会扫几种语言（languages.scan_languages，不是 options 里配置了几种
    "可切换的"语言），就多给几份 LANG_SCAN_BUDGET_SEC——scan_language_
    variants() 只会为 scan_languages 里列出的语言切一次、重新扫一遍表单/
    表头/弹窗字段，没配 scan_languages 就完全不做这部分，超时预算也该
    照实际会不会做、做几次来给，按 options 的语言总数给会白白多留预算。
    """
    lang_count = len((languages or {}).get("scan_languages") or [])
    return base + lang_count * LANG_SCAN_BUDGET_SEC


def _scan_with_timeout(url: str, storage_state: Optional[str], timeout: int = SCAN_TIMEOUT_SEC,
                       languages: Optional[Dict] = None, login: Optional[Dict] = None,
                       include_crud: bool = True, include_i18n: bool = True) -> Dict:
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
            box["phase"] = "准备扫描"
            box["report"] = scanner.scan(url, storage_state=storage_state, headless=True,
                                         languages=languages, login=login,
                                         include_crud=include_crud,
                                         include_i18n=include_i18n,
                                         on_phase=lambda name: box.update(phase=name))
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(effective_timeout)
    if t.is_alive():
        phase = box.get("phase", "未知阶段")
        raise TimeoutError(
            f"扫描超过 {effective_timeout}s 未完成，停在「{phase}」；"
            "页面可能卡死，已跳过")
    if "error" in box:
        raise box["error"]
    return box["report"]


def _language_fingerprint(languages: Optional[Dict]) -> str:
    raw = json.dumps(languages or {}, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_scan_cache(path: Path, url: str, languages: Optional[Dict],
                     include_crud: bool, include_i18n: bool) -> Optional[Dict]:
    """读取能力足够的缓存；较完整的缓存可以服务较轻量的生成请求。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if data.get("version") != SCAN_CACHE_VERSION or data.get("url") != url:
        return None
    capabilities = data.get("capabilities") or {}
    if include_crud and not capabilities.get("crud"):
        return None
    if include_i18n:
        if not capabilities.get("i18n"):
            return None
        if data.get("language_fingerprint") != _language_fingerprint(languages):
            return None
    report = data.get("report")
    return report if isinstance(report, dict) else None


def _save_scan_cache(path: Path, url: str, languages: Optional[Dict], report: Dict,
                     include_crud: bool, include_i18n: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCAN_CACHE_VERSION,
        "url": url,
        "language_fingerprint": _language_fingerprint(languages),
        "capabilities": {"crud": include_crud, "i18n": include_i18n},
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "report": report,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_selected(dir_name: str, storage_state: Optional[str] = None,
                  on_log: Callable = None, overwrite: bool = False,
                  concurrency: int = DEFAULT_CONCURRENCY,
                  force_scan: bool = False,
                  only_tags: Optional[List[str]] = None) -> Dict:
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
    requested_tags = set(only_tags or [])
    scan_all = not requested_tags
    include_crud = scan_all or "crud" in requested_tags
    include_i18n = scan_all or "i18n" in requested_tags
    mode = "强制扫描" if force_scan else "缓存优先"
    _log(on_log, f"开始生成 {len(pages)} 个页面（{mode}，浏览器并发 {concurrency}）")
    counters = {"made": 0, "cached": 0, "scanned": 0,
                "skipped": 0, "failed": 0, "done": 0}
    lock = threading.Lock()

    def scan_one(i: int, pg: Dict) -> None:
        name, url = pg["name"], pg.get("url")
        tag = f"[{name}]"
        dest = P.page_config_path(dir_name, name)
        cache_path = P.scan_cache_path(dir_name, name)

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
            rep = None
            if not force_scan:
                rep = _load_scan_cache(cache_path, url, proj.get("languages"),
                                       include_crud, include_i18n)
            if rep is not None:
                _log(on_log, f"{tag} 命中页面结构缓存，正在生成 YAML…")
                with lock:
                    counters["cached"] += 1
            else:
                reason = "已要求强制刷新" if force_scan else "没有可用缓存"
                _log(on_log, f"{tag} {reason}，启动浏览器扫描…")
                # 缓存命中不应该为了浏览器限流白等；只有真正启动浏览器的
                # 第一批任务才需要错峰，快速重新生成应当立即完成。
                if i <= concurrency:
                    time.sleep((i - 1) * STAGGER_DELAY_SEC)
                # 个别页面（大量级联下拉、地图选点、"新增"弹窗字段特别多）扫描
                # 本来就比一般页面慢，不该为了它们把所有页面的超时都调大——
                # 项目设置里给这一页单独加 scan_timeout（秒）就行，不给就用
                # 全局默认的 SCAN_TIMEOUT_SEC。
                page_timeout = pg.get("scan_timeout") or SCAN_TIMEOUT_SEC
                rep = _scan_with_timeout(url, storage_state, timeout=page_timeout,
                                         languages=proj.get("languages"),
                                         login=proj.get("login"),
                                         include_crud=include_crud,
                                         include_i18n=include_i18n)
                _save_scan_cache(cache_path, url, proj.get("languages"), rep,
                                 include_crud, include_i18n)
                with lock:
                    counters["scanned"] += 1
                timings = rep.get("scan_timings_ms") or {}
                if timings:
                    detail = "，".join(f"{k} {v}ms" for k, v in timings.items())
                    _log(on_log, f"{tag} 扫描耗时：{detail}")
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

    _log(on_log, f"生成完成：写入 {counters['made']}，缓存命中 {counters['cached']}，"
                 f"浏览器扫描 {counters['scanned']}，跳过 {counters['skipped']}，失败 {counters['failed']}")
    return {k: counters[k] for k in ("made", "cached", "scanned", "skipped", "failed")}


def redetect_list_api(dir_name: str, page_name: str,
                      storage_state: Optional[str] = None,
                      on_log: Callable = None) -> Dict[str, Any]:
    """
    只重新探测一个页面的 list_api，不做全量重新扫描。

    list_api 猜错了很常见（同源的全局小组件轮询接口凑巧压中最后一条、
    URL 命名巧合），但为了修这一个字段没必要走"重新生成用例"——那条
    路径会把这个页面的表单/表头/按钮/弹窗全部重新识别一遍，本来就没错的
    部分陪跑不说，还会覆盖掉用户手工加在这份配置里的业务断言。这里只
    调 scanner.redetect_list_api()（跳过表单/按钮/分页/弹窗识别，只探测
    列表接口），拿到新值后只改配置文件里的 list_api 一个字段，其余原样
    保留。
    """
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")
    pg = next((p for p in proj.get("pages", []) if p.get("name") == page_name), None)
    if not pg or not pg.get("url"):
        raise ValueError(f"页面「{page_name}」不存在或没有 URL")
    dest = P.page_config_path(dir_name, page_name)
    if not dest.exists():
        raise ValueError(f"页面「{page_name}」还没有生成过配置，请先「生成用例」")

    cfg = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
    old = cfg.get("list_api")
    _log(on_log, f"[{page_name}] 当前 list_api: {old}")
    new = scanner.redetect_list_api(pg["url"], storage_state, login=proj.get("login"))
    if not new:
        _log(on_log, f"[{page_name}] 没探测到任何候选接口，list_api 保持不变")
        return {"old": old, "new": None, "changed": False}
    if new == old:
        _patch_cached_list_api(dir_name, page_name, new)
        _log(on_log, f"[{page_name}] 重新探测结果跟原来一致，未改动")
        return {"old": old, "new": new, "changed": False}

    cfg["list_api"] = new
    dest.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    _patch_cached_list_api(dir_name, page_name, new)
    _log(on_log, f"[{page_name}] 重新探测到: {new}，已写回配置")
    return {"old": old, "new": new, "changed": True}


def _patch_cached_list_api(dir_name: str, page_name: str, value: str) -> None:
    """同步修正结构缓存，避免以后“重新生成 YAML”又把旧接口写回来。"""
    cache_path = P.scan_cache_path(dir_name, page_name)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        report = cached.get("report") if isinstance(cached, dict) else None
        if not isinstance(report, dict):
            return
        report["list_api"] = value
        cache_path.write_text(
            json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


def redetect_all_list_apis(dir_name: str,
                           storage_state: Optional[str] = None,
                           on_log: Callable = None) -> Dict[str, int]:
    """重新探测当前项目所有已生成配置页面的 ``list_api``。

    只改 YAML 和结构缓存里的 list_api，不覆盖表单、表头、按钮、用例步骤
    以及人工补充的断言。浏览器和登录态由 scanner.redetect_list_apis()
    在整批页面间复用，逐页错误互不影响。
    """
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")

    targets = []
    skipped = 0
    for pg in proj.get("pages", []):
        name, url = pg.get("name"), pg.get("url")
        dest = P.page_config_path(dir_name, name) if name else None
        if not name or not url or not dest or not dest.exists():
            skipped += 1
            reason = "没有 URL" if name and not url else "还没生成配置"
            _log(on_log, f"[{name or '未命名页面'}] 跳过（{reason}）")
            continue
        targets.append({"name": name, "url": url})

    if not targets:
        raise ValueError("当前系统没有已生成配置的页面，请先执行「生成用例」")

    counters = {"total": len(targets), "changed": 0, "unchanged": 0,
                "no_candidate": 0, "failed": 0, "skipped": skipped}
    task_states = {item["name"]: "waiting" for item in targets}

    def emit_progress(index: int, name: Optional[str] = None) -> None:
        progress.emit(
            phase="redetect",
            page=max(1, index),
            pages=len(targets),
            page_name=name,
            tasks=[{"name": item["name"], "status": task_states[item["name"]]}
                   for item in targets],
        )

    _log(on_log, f"开始全局重新探测 {len(targets)} 个页面的列表接口（复用一个浏览器）")
    # 任务开始前先把完整页面清单发给前端，状态栏一开始就显示 38/38，
    # 而不是等哪个页面打印过日志才临时增加一个任务。
    emit_progress(1)

    def on_page(index, total, name, stage, result):
        if stage == "running":
            task_states[name] = "running"
            emit_progress(index, name)
            _log(on_log, f"[{name}] 探测列表接口 {index}/{total} …")
            return

        page_failed = False
        try:
            if result.get("error"):
                page_failed = True
                counters["failed"] += 1
                _log(on_log, f"[{name}] 失败: {result['error']}")
            else:
                dest = P.page_config_path(dir_name, name)
                cfg = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
                old, new = cfg.get("list_api"), result.get("api")
                if not new:
                    counters["no_candidate"] += 1
                    _log(on_log, f"[{name}] 没探测到候选接口，保持 {old}")
                elif new == old:
                    counters["unchanged"] += 1
                    _patch_cached_list_api(dir_name, name, new)
                    _log(on_log, f"[{name}] 接口正确，无需修改: {new}")
                else:
                    cfg["list_api"] = new
                    dest.write_text(
                        yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=110),
                        encoding="utf-8")
                    _patch_cached_list_api(dir_name, name, new)
                    counters["changed"] += 1
                    _log(on_log, f"[{name}] 已修正: {old} -> {new}")
        except Exception as exc:
            page_failed = True
            counters["failed"] += 1
            _log(on_log, f"[{name}] 写回失败: {type(exc).__name__}: {exc}")
        finally:
            task_states[name] = "failed" if page_failed else "passed"
            emit_progress(index, name)

    scanner.redetect_list_apis(
        targets,
        storage_state=storage_state,
        login=proj.get("login"),
        on_page=on_page,
    )
    _log(on_log, "全局重探完成："
         f"修正 {counters['changed']}，无需修改 {counters['unchanged']}，"
         f"无候选 {counters['no_candidate']}，失败 {counters['failed']}，"
         f"跳过 {counters['skipped']}")
    return counters


def run_selected(dir_name: str, out_dir: str,
                 storage_state: Optional[str] = None,
                 only_tags: Optional[List[str]] = None,
                 exclude_tags: Optional[List[str]] = None,
                 on_log: Callable = None,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 target_language: Optional[str] = None,
                 artifact_namespace: Optional[str] = None,
                 result_name_suffix: Optional[str] = None) -> List[PageResult]:
    """
    执行勾选的页面，最多 concurrency 个页面并发跑（见模块头部说明）。

    每条日志都会带 [页面名] 前缀，前端按这个前缀把日志分到各页面自己的
    标签页里——并发跑的时候几个页面的日志会交替出现，没有前缀会看不清
    哪行是哪个页面的。
    """
    cancellation.reset_partial_results()
    proj = P.load_project(dir_name)
    if not proj:
        raise ValueError("项目不存在")

    pages = P.selected_pages(dir_name)
    targets = []
    for pg in pages:
        cfg_path = P.page_config_path(dir_name, pg["name"])
        if cfg_path.exists():
            task_name = (f"{pg['name']} · {result_name_suffix}"
                         if result_name_suffix else pg["name"])
            targets.append((task_name, cfg_path))
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
    task_states = {name: "waiting" for name, _ in targets}

    def emit_task_progress(current_name: Optional[str] = None,
                           case: Optional[int] = None,
                           cases: Optional[int] = None) -> None:
        """把每个页面的状态一起发给前端，刷新页面后也能恢复完整任务面板。"""
        progress.emit(
            phase="run",
            page=max(1, counters["done"]),
            pages=len(targets),
            page_name=current_name,
            case=case,
            cases=cases,
            passed=counters["passed"],
            failed=counters["failed"],
            tasks=[{"name": task_name, "status": task_states[task_name]}
                   for task_name, _ in targets],
        )

    # worker 尚未启动时先发一次 waiting 全量快照，状态栏无需等第一行页面日志。
    emit_task_progress()

    def run_one(idx: int, name: str, cfg_path: Path, browser) -> None:
        tag = f"[{name}]"
        with lock:
            if cancellation.requested():
                task_states[name] = "stopped"
                emit_task_progress(name)
                return
            task_states[name] = "running"
            emit_task_progress(name)
        if cancellation.requested():
            with lock:
                task_states[name] = "stopped"
                emit_task_progress(name)
            return
        t0 = time.time()
        _log(on_log, f"\n{tag} 开始")
        try:
            cfg = load_config(str(cfg_path))
        except Exception as e:
            _log(on_log, f"{tag} ✗ 配置读取失败: {e}")
            pr = PageResult(name, "")
            pr.cases.append(CaseResult("配置", Status.ERROR, error=str(e)))
            results[idx] = pr
            cancellation.publish_partial_result(pr)
            with lock:
                counters["done"] += 1
                counters["failed"] += 1
                task_states[name] = "failed"
                emit_task_progress(name)
            return

        # browser 由浏览器池 worker 创建并复用；每个页面只新建隔离 Context，
        # 保证 localStorage/监听器/下载互不污染，同时省掉反复启动 Chromium。
        worker_ctx_args = dict(base_ctx_args)
        worker_ctx_args["storage_state"] = shared_state
        worker_bctx = browser.new_context(**worker_ctx_args)
        try:
            worker_bctx.set_default_timeout(30000)
            page = worker_bctx.new_page()

            page_out = os.path.join(
                out_dir, artifact_namespace or "", f"{idx + 1:02d}_{P._safe(name)}")
            os.makedirs(page_out, exist_ok=True)
            # report_root=out_dir：截图存在 page_out（每个页面自己的子目录）里，
            # 但汇总的 report.html 写在 out_dir 顶层——截图相对路径必须相对
            # report.html 的位置算，不然报告里的图全部裂掉（用户看不到失败截图）。
            ctx = Context(page, cfg, page_out, target_language=target_language, report_root=out_dir)
            result_name = f"{cfg.name} · {result_name_suffix}" if result_name_suffix else cfg.name
            pr = PageResult(result_name, cfg.url)

            cases = filter_cases_by_tags(cfg.cases, only_tags, exclude_tags)

            stopped_early = False
            for case_index, case in enumerate(cases, 1):
                if cancellation.requested():
                    stopped_early = True
                    _log(on_log, f"{tag} · 已停止，不再执行后续用例")
                    break
                with lock:
                    emit_task_progress(name, case_index, len(cases))
                ctx.case_deadline = time.monotonic() + CASE_RUN_TIMEOUT_SEC
                case_started = time.monotonic()
                case_done = threading.Event()
                _log(on_log, f"{tag} ▶ {case.name} ...")

                def case_heartbeat(case_name=case.name, started=case_started,
                                   done=case_done):
                    while not done.wait(RUN_HEARTBEAT_SEC):
                        elapsed = int(time.monotonic() - started)
                        phase, phase_elapsed = ctx.phase_snapshot()
                        _log(on_log,
                             f"{tag}    … {case_name} 仍在执行（{elapsed}s）"
                             f" · 当前阶段：{phase}（{phase_elapsed}s）")

                heartbeat = threading.Thread(target=case_heartbeat, daemon=True)
                heartbeat.start()
                try:
                    cr = run_case(ctx, case)
                finally:
                    case_done.set()
                icon = {"pass": "✓", "warn": "⚠", "fail": "✗", "error": "!", "skip": "-"}[cr.status.value]
                _log(on_log, f"{tag}    {icon} {case.name}（{cr.duration_ms}ms）")
                if cr.status not in (Status.PASS, Status.WARN):
                    tail = cr.steps[-1].message if cr.steps else cr.error
                    if tail:
                        _log(on_log, f"{tag}    └ {tail[:160]}")
                pr.cases.append(cr)
                if cancellation.requested() and case_index < len(cases):
                    stopped_early = True
                    _log(on_log, f"{tag} · 当前用例已结束，正在生成部分报告")
                    break

            pr.duration_ms = int((time.time() - t0) * 1000)
            _log(on_log, f"{tag} 小计：通过 {pr.passed} / 失败 {pr.failed}")
            results[idx] = pr
        finally:
            worker_bctx.close()

        # 页面 Context 已关闭、PageResult 不会再变化，此时才允许停止处理器拿报告。
        cancellation.publish_partial_result(pr)

        with lock:
            # 进度条右侧和任务状态面板都按“页面”统计。之前这里累加的是
            # 用例数，导致同一时刻上面显示通过 5、下面却显示通过 11。
            # 报告和最终日志仍保留用例级汇总，这里只统一实时页面进度口径。
            if stopped_early:
                counters["done"] += 1
                task_states[name] = "stopped"
            else:
                _record_page_completion(counters, task_states, name, pr)
            emit_task_progress(name)

    # 固定浏览器池：每个 worker 只启动一次 Playwright/Chromium，连续处理分配
    # 给它的页面。默认 concurrency=1 时，整批几十个页面只启动一个执行浏览器。
    groups = _partition_targets(targets, concurrency)

    def run_worker(worker_index: int, items) -> None:
        if worker_index:
            time.sleep(worker_index * STAGGER_DELAY_SEC)
        with sync_playwright() as pw:
            browser = B.launch(pw, headless=True)
            try:
                for idx, name, path in items:
                    run_one(idx, name, path, browser)
            finally:
                browser.close()

    _log(on_log, f"  · 浏览器池已启用：{len(groups)} 个 Chromium worker")
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(run_worker, i, group)
                   for i, group in enumerate(groups) if group]
        for f in as_completed(futures):
            f.result()   # worker 内部没兜住的异常在这里重新抛出，不悄悄吞掉

    final_results = [r for r in results if r is not None]
    total_p = sum(r.passed for r in final_results)
    total_f = sum(r.failed for r in final_results)
    if cancellation.requested():
        _log(on_log, f"\n执行已停止：已收集 {len(final_results)} 个页面，正在生成部分报告")
    else:
        _log(on_log, f"\n全部完成：{len(final_results)} 个页面，通过 {total_p} / 失败 {total_f}")
    return final_results
