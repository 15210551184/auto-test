"""执行引擎：上下文、配置加载、用例编排"""
import html
import inspect
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
from . import cancellation
from . import lang_variants as LV
from . import progress
from .actions import (REGISTRY, AssertionFailed, AssertionWarning,
                      _infer_list_api_from_calls)
from .login import LoginError, ensure_logged_in, is_login_page
from .i18n_terms import button_selector as _button_selector
from .adapters.element_ui import ElementUIAdapter
from .models import Case, CaseResult, PageConfig, PageResult, Status, Step, StepResult
from .state import save_storage_state, valid_storage_state

DEFAULT_SELECTORS = {
    "table": ".el-table",
    "search_btn": _button_selector("search"),
    "reset_btn": _button_selector("reset"),
    "export_btn": _button_selector("export"),
    "create_btn": _button_selector("create"),
    "submit_btn": ".el-dialog:visible .el-button--primary",
}

# 接口调用记录里要替换掉的请求头——这些是登录凭证本身，报告是会被保存、
# 分享、甚至提交进仓库的文件，原样记进去等于把会话令牌写进一份不受权限
# 控制的静态文件里，比截图暴露业务数据严重得多。
_REDACT_HEADERS = {"cookie", "authorization", "set-cookie", "x-token", "token", "x-auth-token"}
# 单条用例最多记这么多条接口调用，避免一条用例里循环点了几十次搜索
# （比如 check_select_options）把报告文件撑得很大。
_API_LOG_LIMIT = 50


class Context:
    """一次页面执行的上下文，贯穿所有 step"""

    def __init__(self, page: Page, config: PageConfig, out_dir: str,
                target_language: Optional[str] = None, report_root: Optional[str] = None):
        self.page = page
        self.config = config
        self.ui = ElementUIAdapter()
        # 用户在执行前选定的语言（对应 config.languages.options 里的某个 code）。
        # 不为 None 时，run_case 会在每条用例重新导航之后、跑用例自己的步骤
        # 之前，先切到这门语言——不是在这里切一次就完事，因为大多数系统语言
        # 状态挂在前端 localStorage/内存里，用例之间的每次 goto 都会把它冲掉。
        self.target_language = target_language
        self.vars: Dict[str, Any] = dict(config.variables)
        self.data: Dict[str, List[dict]] = {}   # capture 存的表格快照
        self.last_api: Optional[dict] = None
        # 导出用例由 export_and_verify 自己提供“列表完整列 + 文件内容”证据，
        # 不再重复放一组没有意义的“搜索前/后”截图。
        self.suppress_search_evidence = False
        self.console_errors: List[str] = []
        self.failed_requests: List[str] = []
        # 这条用例触发的接口调用（方法/URL/状态码/耗时/入参/响应），随
        # reset_signals() 按用例清空，run_case 跑完整条用例后存进 CaseResult，
        # 供报告里查"这条用例到底打了哪些接口、传了什么、返回了什么"。
        self.api_log: List[Dict[str, Any]] = []
        self._req_start: Dict[Any, float] = {}
        # 批量执行的心跳从这里读取当前细分阶段。以前只能看到“某用例仍在
        # 执行”，无法判断是在打开页面、等接口、等下载还是读 Excel。
        self.current_phase = "初始化"
        self.phase_started = time.monotonic()
        self.case_deadline: Optional[float] = None
        self.out_dir = out_dir
        # report_root：截图相对路径要相对谁计算——单页执行时 report.html 就写
        # 在 out_dir 里，两者相同；批量执行时每个页面各有自己的子目录
        # （out_dir 是子目录），但汇总的 report.html 写在批量任务的顶层目录，
        # 不给 report_root 就会用 out_dir 算出"相对子目录自己"的路径，
        # report.html 按它自己的位置去解析就会指向不存在的文件——
        # 图裂了，用户看不到失败截图，等于白截。
        self.report_root = report_root or out_dir
        self.download_dir = os.path.join(out_dir, "downloads")
        self.shots_dir = os.path.join(out_dir, "screenshots")
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.shots_dir, exist_ok=True)
        self._shot_n = 0
        self._hook()

    def set_phase(self, phase: str) -> None:
        self.current_phase = phase
        self.phase_started = time.monotonic()

    def phase_snapshot(self):
        return self.current_phase, max(0, int(time.monotonic() - self.phase_started))

    def remaining_case_ms(self, fallback: int = 150000) -> int:
        if self.case_deadline is None:
            return fallback
        return max(0, int((self.case_deadline - time.monotonic()) * 1000))

    def _hook(self):
        self.page.on("console", lambda m: (
            self.console_errors.append(f"{m.type}: {m.text[:200]}")
            if m.type == "error" else None))
        self.page.on("requestfailed", self._on_request_failed)
        # 原来只记 5xx，4xx（比如图片链接挂了返回 404）完全漏记。
        # 控制台报错里的"Failed to load resource: 404"不带 URL，光看那条消息
        # 猜不出是哪个资源坏的；这里记下来，assert_no_failed_request 能报出
        # 具体链接，配合 assert_no_console_error 一起看就知道是什么坏了。
        self.page.on("response", lambda r:
                     self.failed_requests.append(f"{r.status} {r.url[:160]}")
                     if r.status >= 400 else None)
        self.page.on("request", lambda r: self._req_start.__setitem__(r, time.time()))
        self.page.on("response", self._on_api_response)

    def _on_request_failed(self, request):
        self.failed_requests.append(f"{request.method} {request.url[:120]}")
        start = self._req_start.pop(request, None)  # 请求没等到响应，不留在字典里占地方
        try:
            is_api = request.resource_type in ("xhr", "fetch")
        except Exception:
            is_api = False
        if not is_api or len(self.api_log) >= _API_LOG_LIMIT:
            return
        try:
            headers = {k: ("[已隐藏]" if k.lower() in _REDACT_HEADERS else v)
                       for k, v in request.headers.items()}
        except Exception:
            headers = {}
        try:
            post_data = request.post_data[:2000] if request.post_data else None
        except Exception:
            post_data = None
        try:
            failure = request.failure or "网络请求失败"
        except Exception:
            failure = "网络请求失败"
        self.api_log.append({
            "method": request.method,
            "url": request.url[:500],
            "status": None,
            "duration_ms": int((time.time() - start) * 1000) if start else None,
            "request_headers": headers,
            "request_body": post_data,
            "response_body": None,
            "error": failure,
        })

    def _on_api_response(self, resp):
        """接口调用记录：只挑 JSON 响应（业务接口的典型特征），静态资源
        （图片/字体/js/css）都不是这个 content-type，天然被过滤掉。"""
        req = resp.request
        start = self._req_start.pop(req, None)
        try:
            ct = (resp.headers or {}).get("content-type", "")
        except Exception:
            ct = ""
        if "json" not in ct or len(self.api_log) >= _API_LOG_LIMIT:
            return
        duration_ms = int((time.time() - start) * 1000) if start else None
        try:
            headers = {k: ("[已隐藏]" if k.lower() in _REDACT_HEADERS else v)
                      for k, v in req.headers.items()}
        except Exception:
            headers = {}
        try:
            body = resp.text()
            if len(body) > 3000:
                body = body[:3000] + "…（已截断）"
        except Exception:
            body = None
        post_data = None
        try:
            if req.post_data:
                post_data = req.post_data[:2000]
        except Exception:
            pass
        self.api_log.append({
            "method": req.method,
            "url": resp.url[:500],
            "status": resp.status,
            "duration_ms": duration_ms,
            "request_headers": headers,
            "request_body": post_data,
            "response_body": body,
        })

    def selector(self, key: str) -> str:
        """selectors 里的别名 -> CSS；不是别名就当原始选择器用"""
        if not key:
            raise ValueError("选择器不能为空")
        configured = self.config.selectors.get(key)
        fallback = DEFAULT_SELECTORS.get(key)
        # 页面配置常在中文状态下扫描生成。切换语言执行时，保留配置选择器的
        # 同时追加内置多语言兜底，避免 Search/Add/Export 等按钮定位失效。
        if configured and fallback and configured != fallback and key.endswith("_btn"):
            return f"{configured}, {fallback}"
        return configured or fallback or key

    def label_of(self, canonical: str) -> List[str]:
        """
        canonical label -> 所有已知语言下的候选文案（含它自己，排最前面）。
        没配 label_variants 时就是只有它自己的单元素列表——适配层对单候选
        和多候选走的是同一条路径，行为和只传字符串完全一样，不是两套逻辑。
        """
        return LV.candidates(self.config.label_variants, canonical)

    def column_of(self, canonical: str) -> List[str]:
        """同 label_of，用于表头列名。"""
        return LV.candidates(self.config.header_variants, canonical)

    def table_data(self):
        """ctx.ui.table_data 的快捷方式，自动带上 header_variants。"""
        return self.ui.table_data(self.page, header_variants=self.config.header_variants)

    def find_row_by(self, column: str, value: str, table: str = ".el-table") -> int:
        """ctx.ui.find_row_by 的快捷方式，自动带上 header_variants。"""
        return self.ui.find_row_by(self.page, column, value, table,
                                   header_variants=self.config.header_variants)

    def canonical_headers(self) -> List[str]:
        """当前表头翻译回 canonical 列名，供 assert_headers 按 canonical 名字比对。"""
        raw = self.ui.headers(self.page)
        rmap = LV.reverse_map(self.config.header_variants)
        return [LV.canonical_name(h, rmap) for h in raw]

    def dialog_field_values(self) -> Dict[str, str]:
        """ctx.ui.dialog_field_values 的快捷方式，自动带上 header_variants。"""
        return self.ui.dialog_field_values(self.page, header_variants=self.config.header_variants)

    def detail_values(self) -> Dict[str, str]:
        """ctx.ui.detail_values 的快捷方式，自动带上 header_variants。"""
        return self.ui.detail_values(self.page, header_variants=self.config.header_variants)

    def form_error_labels(self) -> List[str]:
        """ctx.ui.form_error_labels 的快捷方式，自动带上 header_variants。"""
        return self.ui.form_error_labels(self.page, header_variants=self.config.header_variants)

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

    def _shot_path(self, tag: str) -> str:
        self._shot_n += 1
        safe = re.sub(r"[^\w\-]", "_", tag)[:40]
        return os.path.join(self.shots_dir, f"{self._shot_n:03d}_{safe}.png")

    def shot(self, tag: str) -> str:
        """
        截当前页面。页面里有横向滚动的数据表时，临时把 viewport 加宽到能
        容纳表格全部列，避免截图只留下左半边；截图后立即恢复原尺寸。
        """
        path = self._shot_path(tag)
        old_viewport = None
        resized = False
        try:
            # 宽表识别只是截图增强，页面没有表格、适配器不支持对应 API 时
            # 仍应正常保存普通截图，不能因为增强逻辑失败把证据图整个丢掉。
            try:
                table = self.page.locator(self.selector("table")).first
                if table.is_visible():
                    metrics = table.evaluate("""el => {
                      const nodes = [
                        el,
                        el.querySelector('.el-table__header-wrapper'),
                        el.querySelector('.el-table__body-wrapper'),
                        el.querySelector('.ant-table-content'),
                        el.querySelector('.ant-table-body')
                      ].filter(Boolean);
                      return {
                        scrollWidth: Math.max(...nodes.map(n => n.scrollWidth || 0)),
                        clientWidth: Math.max(...nodes.map(n => n.clientWidth || 0))
                      };
                    }""")
                    old_viewport = self.page.viewport_size
                    extra = max(0, metrics["scrollWidth"] - metrics["clientWidth"])
                    if old_viewport and extra > 8:
                        self.page.set_viewport_size({
                            "width": min(30000, old_viewport["width"] + extra + 32),
                            "height": old_viewport["height"],
                        })
                        self.page.wait_for_timeout(120)
                        resized = True
            except Exception:
                pass
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            return ""
        finally:
            if resized and old_viewport:
                try:
                    self.page.set_viewport_size(old_viewport)
                except Exception:
                    pass
        return os.path.relpath(path, self.report_root)

    def table_preview_shot(self, rows: List[dict], tag: str, title: str,
                           source_name: str = "", max_rows: int = 20) -> str:
        """把 Excel/CSV 解析结果渲染成可读表格并截成一张完整列的图片。"""
        if not rows:
            return ""
        path = self._shot_path(tag)
        preview = None
        try:
            headers = list(rows[0].keys())
            shown = rows[:max_rows]

            def text(v):
                return "" if v is None else str(v)

            # 按内容估算每列宽度；中日韩字符按两个英文字符计算。
            widths = []
            for col in headers:
                values = [str(col), *(text(r.get(col)) for r in shown)]
                units = max(sum(2 if ord(ch) > 127 else 1 for ch in value)
                            for value in values)
                widths.append(min(360, max(88, units * 8 + 28)))
            viewport_width = min(30000, max(960, sum(widths) + 42))

            colgroup = "".join(f'<col style="width:{w}px">' for w in widths)
            head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
            body = "".join(
                "<tr>" + "".join(
                    f"<td>{html.escape(text(row.get(h)))}</td>" for h in headers
                ) + "</tr>"
                for row in shown
            )
            note = (f"{html.escape(source_name)} · 共 {len(rows)} 行"
                    f" · 截图展示前 {len(shown)} 行")
            markup = f"""<!doctype html><html><head><meta charset="utf-8"><style>
              *{{box-sizing:border-box}} body{{margin:0;padding:20px;background:#f4f6f8;
              color:#20242b;font:14px/1.45 -apple-system,BlinkMacSystemFont,
              "PingFang SC","Microsoft YaHei",sans-serif}}
              h1{{font-size:18px;margin:0 0 5px}} .note{{color:#7a8492;margin-bottom:14px}}
              .sheet{{display:inline-block;background:#fff;border:1px solid #dfe3e8;
              box-shadow:0 2px 10px rgba(0,0,0,.06)}} table{{border-collapse:collapse;
              table-layout:fixed}} th,td{{padding:9px 11px;border-right:1px solid #e5e8ec;
              border-bottom:1px solid #e5e8ec;text-align:left;white-space:nowrap;
              overflow:hidden;text-overflow:ellipsis}} th{{background:#f2f4f7;
              font-weight:600;position:sticky;top:0}} tr:nth-child(even) td{{background:#fafbfc}}
            </style></head><body><h1>{html.escape(title)}</h1>
            <div class="note">{note}</div><div class="sheet"><table>
            <colgroup>{colgroup}</colgroup><thead><tr>{head}</tr></thead>
            <tbody>{body}</tbody></table></div></body></html>"""

            preview = self.page.context.new_page()
            preview.set_viewport_size({"width": viewport_width, "height": 900})
            preview.set_content(markup, wait_until="load")
            preview.screenshot(path=path, full_page=True)
        except Exception:
            return ""
        finally:
            if preview is not None:
                try:
                    preview.close()
                except Exception:
                    pass
        return os.path.relpath(path, self.report_root)

    def reset_signals(self):
        self.console_errors.clear()
        self.failed_requests.clear()
        self.api_log.clear()


# ---------- 配置加载 ----------

def filter_cases_by_tags(cases: List[Case], only_tags: Optional[List[str]] = None,
                         exclude_tags: Optional[List[str]] = None) -> List[Case]:
    """
    only_tags：只跑带这些标签的用例；exclude_tags：排除带这些标签的用例。
    两者可以一起用（先只跑，再从里面排除）；都不传就是全部跑。

    exclude 存在的意义是"以后加新标签不用记得去改include名单"——比如不想跑
    多语言检查，只排除 i18n 一个标签就行，不用把 smoke/health/search/list/
    export/crud 全部勾一遍还要记得以后新加的标签也要勾上。
    """
    if only_tags:
        cases = [c for c in cases if set(c.tags) & set(only_tags)]
    if exclude_tags:
        cases = [c for c in cases if not (set(c.tags) & set(exclude_tags))]
    return cases


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
        search_timeout=raw.get("search_timeout", 30000),
        export_mode=raw.get("export_mode", "auto"),
        export_task_api=raw.get("export_task_api"),
        login=raw.get("login", {}),
        languages=raw.get("languages", {}),
        label_variants=raw.get("label_variants", {}),
        header_variants=raw.get("header_variants", {}),
    )


# ---------- 执行 ----------

def run_step(ctx: Context, step: Step) -> StepResult:
    t0 = time.time()
    fn = REGISTRY.get(step.action)
    if fn is None:
        return StepResult(step.action, step.params, Status.ERROR,
                          f"未知动作 '{step.action}'，可用: {sorted(REGISTRY)}")
    try:
        phase_names = {
            "export_and_verify": "导出：准备下载与数据比对",
            "search": "搜索：等待列表刷新",
            "check_select_options": "筛选：逐项验证下拉选项",
            "capture": "抓取页面表格数据",
        }
        if hasattr(ctx, "set_phase"):
            ctx.set_phase(phase_names.get(step.action, f"执行步骤：{step.action}"))
        params = dict(step.params)
        remaining = ctx.remaining_case_ms() if hasattr(ctx, "remaining_case_ms") else 150000
        if remaining <= 0:
            raise TimeoutError("当前用例超过 150 秒，已终止")
        timeout_param = inspect.signature(fn).parameters.get("timeout")
        configured_timeout = params.get("timeout")
        if configured_timeout is None and timeout_param is not None \
                and timeout_param.default is not inspect.Parameter.empty:
            configured_timeout = timeout_param.default
        if configured_timeout is not None:
            params["timeout"] = min(int(configured_timeout), remaining)
        if step.action == "wait":
            key = "value" if params.get("value") is not None else "ms"
            params[key] = min(int(params.get(key, 500)), remaining)
        try:
            # 仍保留原来的单次 Playwright 30 秒上限；用例总预算只负责进一步
            # 收紧，不能反过来把一次 locator 等待放宽到 150 秒。
            ctx.page.set_default_timeout(max(1, min(30000, remaining)))
        except AttributeError:
            pass
        out = fn(ctx, **params)
        # 动作可以只返回一句消息，也可以返回 (消息, detail)——detail 是给
        # 报告页面的结构化附件（对比截图、下载链接），不是所有动作都有。
        msg, detail = out if isinstance(out, tuple) else (out, None)
        return StepResult(step.action, step.params, Status.PASS,
                          msg or "", int((time.time() - t0) * 1000), detail=detail)
    except AssertionWarning as e:
        # 警告不算失败，不截图（不是真出问题，没必要留证据），也不会打断后续步骤
        return StepResult(step.action, step.params, Status.WARN, str(e),
                          int((time.time() - t0) * 1000))
    except AssertionFailed as e:
        detail = getattr(e, "detail", None)
        # 动作已经提供专用证据图时，不再补一张重复的通用失败截图。
        screenshot = None if detail and detail.get("images") else ctx.shot(
            f"fail_{step.action}")
        return StepResult(step.action, step.params, Status.FAIL, str(e),
                          int((time.time() - t0) * 1000),
                          screenshot=screenshot, detail=detail)
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
    if hasattr(ctx, "set_phase"):
        ctx.set_phase("打开并等待页面")
    ctx.reset_signals()
    ctx.suppress_search_evidence = any(
        step.action == "export_and_verify" for step in case.steps)
    frag = ctx.config.list_api
    remaining = ctx.remaining_case_ms() if hasattr(ctx, "remaining_case_ms") else 150000
    if remaining <= 0:
        return CaseResult(case.name, Status.ERROR, error="当前用例超过 150 秒，已终止")
    if frag:
        # 等这次导航触发的列表接口真正返回，比固定等待更准；expect_response
        # 必须包住触发动作（goto）本身才能等到它，不能在 goto 之后才注册——
        # 之前 scanner.scan() 就是在 goto 之后才等，经常等到无关的早发请求，
        # 这里改用正确的用法：包住 goto，等的就是这次导航真正触发的那次列表请求。
        try:
            with ctx.page.expect_response(
                lambda r: frag in r.url and r.status == 200, timeout=5000
            ):
                ctx.page.goto(ctx.config.url, wait_until="domcontentloaded",
                              timeout=min(60000, remaining))
            ctx.page.wait_for_timeout(500)
        except Exception:
            # 旧 YAML 可能把扫描表单时请求的国家/城市/加盟商下拉接口写成
            # list_api。页面导航后的真实列表响应已经进入 api_log 时，直接
            # 自动纠正内存配置，同页后续所有用例立即复用，不再每条先等 15 秒。
            inferred = _infer_list_api_from_calls(ctx, ctx.api_log)
            if inferred:
                actual_api, payload = inferred
                ctx.config.list_api = actual_api
                ctx.last_api = payload
                ctx.page.wait_for_timeout(500)
            else:
                # 没有替代候选时仍给真正的慢接口保留原来的总计 15 秒预算。
                try:
                    with ctx.page.expect_response(
                        lambda r: frag in r.url and r.status == 200,
                        timeout=10000,
                    ):
                        pass
                    ctx.page.wait_for_timeout(500)
                except Exception:
                    ctx.page.wait_for_timeout(3000)
    else:
        ctx.page.goto(ctx.config.url, wait_until="domcontentloaded",
                      timeout=min(60000, remaining))
        ctx.page.wait_for_timeout(1500)

    # 长时间运行中 session 可能过期，掉回登录页就就地重登，
    # 否则后面每条用例都会在登录页上失败
    if ctx.config.login and is_login_page(ctx.page):
        try:
            ensure_logged_in(ctx.page, ctx.config.url, ctx.config.login)
        except LoginError as e:
            return CaseResult(case.name, Status.ERROR, error=f"会话过期且重登失败: {e}")

    if ctx.target_language:
        try:
            if hasattr(ctx, "set_phase"):
                ctx.set_phase("切换页面语言")
            REGISTRY["switch_language"](ctx, to=ctx.target_language)
        except Exception as e:
            # 部分页面列表接口很慢：domcontentloaded/list_api 等待结束时，顶栏
            # 的 .lang-select 仍未挂载，第一次点击会耗尽 5 秒。仅对 Timeout
            # 做一次刷新重试；配置错误（未知语言/选择器）仍立即失败，不能用
            # 重试掩盖。刷新也会顺带清理之前导航留下的 response waiter。
            if "Timeout" not in type(e).__name__ and "Timeout" not in str(e):
                return CaseResult(case.name, Status.ERROR,
                                  error=f"切换语言失败: {e}")
            try:
                if hasattr(ctx, "set_phase"):
                    ctx.set_phase("语言入口未就绪，刷新后重试")
                ctx.page.reload(wait_until="domcontentloaded", timeout=30000)
                ctx.page.wait_for_timeout(1200)
                if ctx.config.login and is_login_page(ctx.page):
                    ensure_logged_in(ctx.page, ctx.config.url, ctx.config.login)
                REGISTRY["switch_language"](ctx, to=ctx.target_language)
            except Exception as retry_error:
                return CaseResult(
                    case.name, Status.ERROR,
                    error=f"切换语言失败（刷新重试仍失败）: {retry_error}")

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

    if hasattr(ctx, "set_phase"):
        ctx.set_phase("用例收尾")
    return CaseResult(case.name, status, results, int((time.time() - t0) * 1000),
                      api_calls=list(ctx.api_log))


def run_page(config: PageConfig, out_dir: str, headless: bool = True,
             storage_state: Optional[str] = None, slow_mo: int = 0,
             only_tags: Optional[List[str]] = None,
             exclude_tags: Optional[List[str]] = None,
             target_language: Optional[str] = None) -> PageResult:
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

        ctx = Context(page, config, out_dir, target_language=target_language)

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

        cases = filter_cases_by_tags(config.cases, only_tags, exclude_tags)

        for ci, case in enumerate(cases, 1):
            if cancellation.requested():
                print("  · 已停止，不再执行后续用例", flush=True)
                break
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
            if cancellation.requested():
                print("  · 当前用例已结束，正在生成部分报告", flush=True)
                break

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
