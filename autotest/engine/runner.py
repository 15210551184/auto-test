"""执行引擎：上下文、配置加载、用例编排"""
import os
import random
import re
import string
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from playwright.sync_api import sync_playwright, Page

from . import browser as B
from . import progress
from .actions import REGISTRY, AssertionFailed, AssertionWarning
from .login import LoginError, ensure_logged_in, is_login_page
from .adapters.element_ui import ElementUIAdapter
from .models import Case, CaseResult, PageConfig, PageResult, Status, Step, StepResult
from .state import save_storage_state, valid_storage_state

DEFAULT_SELECTORS = {
    "table": ".el-table",
    "search_btn": "button:has-text('搜索'), button:has-text('查询')",
    "reset_btn": "button:has-text('重置')",
    "export_btn": "button:has-text('导出')",
    "create_btn": "button:has-text('新增'), button:has-text('添加')",
    "submit_btn": ".el-dialog:visible .el-button--primary",
}


class Context:
    """一次页面执行的上下文，贯穿所有 step"""

    def __init__(self, page: Page, config: PageConfig, out_dir: str):
        self.page = page
        self.config = config
        self.ui = ElementUIAdapter()
        self.vars: Dict[str, Any] = dict(config.variables)
        self.data: Dict[str, List[dict]] = {}   # capture 存的表格快照
        self.last_api: Optional[dict] = None
        self.console_errors: List[str] = []
        self.failed_requests: List[str] = []
        self.out_dir = out_dir
        self.download_dir = os.path.join(out_dir, "downloads")
        self.shots_dir = os.path.join(out_dir, "screenshots")
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.shots_dir, exist_ok=True)
        self._shot_n = 0
        self._hook()

    def _hook(self):
        self.page.on("console", lambda m: (
            self.console_errors.append(f"{m.type}: {m.text[:200]}")
            if m.type == "error" else None))
        self.page.on("requestfailed", lambda r:
                     self.failed_requests.append(f"{r.method} {r.url[:120]}"))
        # 原来只记 5xx，4xx（比如图片链接挂了返回 404）完全漏记。
        # 控制台报错里的"Failed to load resource: 404"不带 URL，光看那条消息
        # 猜不出是哪个资源坏的；这里记下来，assert_no_failed_request 能报出
        # 具体链接，配合 assert_no_console_error 一起看就知道是什么坏了。
        self.page.on("response", lambda r:
                     self.failed_requests.append(f"{r.status} {r.url[:160]}")
                     if r.status >= 400 else None)

    def selector(self, key: str) -> str:
        """selectors 里的别名 -> CSS；不是别名就当原始选择器用"""
        if not key:
            raise ValueError("选择器不能为空")
        return self.config.selectors.get(key, DEFAULT_SELECTORS.get(key, key))

    def resolve(self, value: Any) -> Any:
        """展开 ${var} / ${random} / ${today} 等占位符"""
        if not isinstance(value, str) or "${" not in value:
            return value

        def rep(m):
            k = m.group(1)
            if k == "random":
                return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            if k == "timestamp":
                return str(int(time.time()))
            if k == "email":
                s = "".join(random.choices(string.ascii_lowercase, k=8))
                return f"auto_{s}@test.com"
            if k == "phone":
                return "138" + "".join(random.choices(string.digits, k=8))
            if k == "today":
                return datetime.now().strftime("%Y-%m-%d")
            if k == "now":
                return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            m2 = re.fullmatch(r"days_ago_(\d+)", k)
            if m2:
                return (datetime.now() - timedelta(days=int(m2.group(1)))).strftime("%Y-%m-%d")
            return str(self.vars.get(k, m.group(0)))

        return re.sub(r"\$\{(\w+)\}", rep, value)

    def shot(self, tag: str) -> str:
        self._shot_n += 1
        safe = re.sub(r"[^\w\-]", "_", tag)[:40]
        path = os.path.join(self.shots_dir, f"{self._shot_n:03d}_{safe}.png")
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            return ""
        return os.path.relpath(path, self.out_dir)

    def reset_signals(self):
        self.console_errors.clear()
        self.failed_requests.clear()


# ---------- 配置加载 ----------

def load_config(path: str) -> PageConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = []
    for c in raw.get("cases", []):
        cases.append(Case(
            name=c["name"],
            steps=[Step.from_raw(s) for s in c.get("steps", [])],
            tags=c.get("tags", []),
            skip=c.get("skip", False),
        ))
    return PageConfig(
        name=raw.get("name", "未命名页面"),
        url=raw["url"],
        selectors={**DEFAULT_SELECTORS, **raw.get("selectors", {})},
        cases=cases,
        variables=raw.get("variables", {}),
        list_api=raw.get("list_api"),
        export_mode=raw.get("export_mode", "auto"),
        export_task_api=raw.get("export_task_api"),
        login=raw.get("login", {}),
        languages=raw.get("languages", {}),
    )


# ---------- 执行 ----------

def run_step(ctx: Context, step: Step) -> StepResult:
    t0 = time.time()
    fn = REGISTRY.get(step.action)
    if fn is None:
        return StepResult(step.action, step.params, Status.ERROR,
                          f"未知动作 '{step.action}'，可用: {sorted(REGISTRY)}")
    try:
        msg = fn(ctx, **step.params)
        return StepResult(step.action, step.params, Status.PASS,
                          msg or "", int((time.time() - t0) * 1000))
    except AssertionWarning as e:
        # 警告不算失败，不截图（不是真出问题，没必要留证据），也不会打断后续步骤
        return StepResult(step.action, step.params, Status.WARN, str(e),
                          int((time.time() - t0) * 1000))
    except AssertionFailed as e:
        return StepResult(step.action, step.params, Status.FAIL, str(e),
                          int((time.time() - t0) * 1000),
                          screenshot=ctx.shot(f"fail_{step.action}"))
    except Exception as e:
        return StepResult(step.action, step.params, Status.ERROR,
                          f"{type(e).__name__}: {e}",
                          int((time.time() - t0) * 1000),
                          screenshot=ctx.shot(f"error_{step.action}"),
                          detail={"traceback": traceback.format_exc()[-1500:]})


def run_case(ctx: Context, case: Case) -> CaseResult:
    t0 = time.time()
    if case.skip:
        return CaseResult(case.name, Status.SKIP)

    # 每条用例回到干净的页面状态，避免用例间互相污染
    ctx.reset_signals()
    frag = ctx.config.list_api
    if frag:
        # 等这次导航触发的列表接口真正返回，比固定等待更准；expect_response
        # 必须包住触发动作（goto）本身才能等到它，不能在 goto 之后才注册——
        # 之前 scanner.scan() 就是在 goto 之后才等，经常等到无关的早发请求，
        # 这里改用正确的用法：包住 goto，等的就是这次导航真正触发的那次列表请求。
        try:
            with ctx.page.expect_response(
                lambda r: frag in r.url and r.status == 200, timeout=15000
            ):
                ctx.page.goto(ctx.config.url, wait_until="domcontentloaded", timeout=60000)
            ctx.page.wait_for_timeout(500)
        except Exception:
            ctx.page.wait_for_timeout(3000)   # 没等到，退回一个更保守的固定等待
    else:
        ctx.page.goto(ctx.config.url, wait_until="domcontentloaded", timeout=60000)
        ctx.page.wait_for_timeout(1500)

    # 长时间运行中 session 可能过期，掉回登录页就就地重登，
    # 否则后面每条用例都会在登录页上失败
    if ctx.config.login and is_login_page(ctx.page):
        try:
            ensure_logged_in(ctx.page, ctx.config.url, ctx.config.login)
        except LoginError as e:
            return CaseResult(case.name, Status.ERROR, error=f"会话过期且重登失败: {e}")

    results, status = [], Status.PASS
    stopped_at = None
    for i, step in enumerate(case.steps):
        r = run_step(ctx, step)
        results.append(r)
        if r.status in (Status.FAIL, Status.ERROR):
            status = r.status
            stopped_at = i
            break   # 快速失败：后续步骤依赖前面的状态，继续跑没意义
        if r.status == Status.WARN and status == Status.PASS:
            status = Status.WARN   # 警告不打断执行，只把整条用例的状态标成"有警告"

    if stopped_at is not None:
        # 快速失败会跳过后面所有步骤——但如果用例末尾是 delete_and_verify
        # 这类清理步骤，跳过它就等于把本次自动创建的测试数据永久留在系统里，
        # 这条铁律（只动自己建的数据、跑完自动清理）不能因为前面验证失败就破例。
        # delete_and_verify 自己有幂等和安全检查（没有待清理记录会直接跳过，
        # 不是 auto_ 前缀的数据会拒绝删除），这里放心兜底执行。
        for step in case.steps[stopped_at + 1:]:
            if step.action == "delete_and_verify":
                results.append(run_step(ctx, step))

    return CaseResult(case.name, status, results, int((time.time() - t0) * 1000))


def run_page(config: PageConfig, out_dir: str, headless: bool = True,
             storage_state: Optional[str] = None, slow_mo: int = 0,
             only_tags: Optional[List[str]] = None) -> PageResult:
    t0 = time.time()
    result = PageResult(config.name, config.url)

    with sync_playwright() as pw:
        browser = B.launch(pw, headless=headless, slow_mo=slow_mo)
        ctx_args = B.context_args(accept_downloads=True)
        state = valid_storage_state(storage_state)
        if state:
            ctx_args["storage_state"] = state
        bctx = browser.new_context(**ctx_args)
        bctx.set_default_timeout(30000)
        page = bctx.new_page()

        ctx = Context(page, config, out_dir)

        # --- 登录：先试旧 cookie，失效才用账号密码登录 ---
        # 登录失败直接中止，不然每条用例都会在登录页上失败，报告全红没意义
        if config.login:
            try:
                did = ensure_logged_in(page, config.url, config.login)
                if did:
                    save_storage_state(bctx, storage_state or "auth/state.json")
                    print("  · 已重新登录，登录态已保存")
                else:
                    print("  · 复用已有登录态")
            except LoginError as e:
                browser.close()
                result.cases.append(CaseResult("登录", Status.ERROR, error=str(e)))
                result.duration_ms = int((time.time() - t0) * 1000)
                print(f"  ✗ 登录失败: {e}")
                return result

        cases = config.cases
        if only_tags:
            cases = [c for c in cases if set(c.tags) & set(only_tags)]

        for ci, case in enumerate(cases, 1):
            print(f"  ▶ {case.name} ... ", end="", flush=True)
            cr = run_case(ctx, case)
            icon = {"pass": "✓", "warn": "⚠", "fail": "✗", "error": "!", "skip": "-"}[cr.status.value]
            print(f"{icon} ({cr.duration_ms}ms)")
            if cr.status != Status.PASS and cr.steps:
                print(f"     └ {cr.steps[-1].message[:160]}")
            result.cases.append(cr)
            progress.emit(phase="run", page=1, pages=1, page_name=config.name,
                          case=ci, cases=len(cases),
                          passed=result.passed, failed=result.failed)

        browser.close()

    result.duration_ms = int((time.time() - t0) * 1000)
    return result


def save_login_state(url: str, state_path: str, wait_seconds: int = 180) -> None:
    """
    有头浏览器打开登录页，人工登录后自动保存 cookie/localStorage。
    之后所有用例复用这个状态，不用每次登录。
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        bctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = bctx.new_page()
        page.goto(url)
        print(f"请在浏览器中完成登录，登录后回到这里按 Enter（最多等待 {wait_seconds}s）...")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(wait_seconds * 1000)
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        save_storage_state(bctx, state_path)
        print(f"登录态已保存到 {state_path}")
        browser.close()
