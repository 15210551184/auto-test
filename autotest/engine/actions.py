"""
动作与断言注册表。

配置里每个 step 的键名对应这里的一个函数。加新能力 = 加一个 @action 函数，
不用改引擎。ctx 是执行上下文，带着 page / adapter / 变量池 / 抓取的数据。
"""
import re
from typing import Any, Callable, Dict, List

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import datafactory as DF
from . import normalize as N
from .i18n_terms import words as _i18n_words

REGISTRY: Dict[str, Callable] = {}


def action(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


class AssertionFailed(Exception):
    """
    断言失败（业务问题），区别于 Exception（脚本/环境问题）。

    detail 是给报告页面用的结构化附件（下载链接、对比截图之类），跟
    str(self) 这条给人看的失败消息是两回事——run_step() 会把它原样搬进
    StepResult.detail，report.py 按里面的 key（如 download/images）渲染
    对应的 UI，不认识的 key 直接忽略。
    """

    def __init__(self, msg: str, detail: Dict[str, Any] = None):
        super().__init__(msg)
        self.detail = detail


class AssertionWarning(Exception):
    """
    检测到问题但不算失败。用于"页面本身显示正常时，这个问题不该拖垮整条用例"
    的场景——比如控制台报错：页面数据、表头、行数都对，说明用户看到的东西没问题，
    一条无关的资源报错不该让用例变红；但内容确实不对（行数不对/渲染出乱码）时，
    这些报错就有诊断价值，会作为上下文附在真正失败的那条断言消息里。
    """


def _fail(msg: str, detail: Dict[str, Any] = None):
    raise AssertionFailed(msg, detail=detail)


def _warn(msg: str):
    raise AssertionWarning(msg)


def _diag_suffix(ctx) -> str:
    """内容类断言（行数/表头/渲染）失败时，附带当时的控制台/网络错误做诊断参考。"""
    bits = []
    errs = getattr(ctx, "console_errors", None) or []
    if errs:
        bits.append(f"控制台报错 {len(errs)} 条: {errs[:2]}")
    reqs = getattr(ctx, "failed_requests", None) or []
    if reqs:
        bits.append(f"失败请求 {len(reqs)} 条: {reqs[:2]}")
    return ("\n  ↳ 可能相关: " + "；".join(bits)) if bits else ""


# ============ 导航与基础交互 ============

@action("goto")
def do_goto(ctx, url: str = None, value: str = None, **kw):
    target = url or value or ctx.config.url
    ctx.page.goto(ctx.resolve(target), wait_until="domcontentloaded", timeout=60000)
    return f"打开 {target}"


@action("click")
def do_click(ctx, target: str = None, value: str = None, text: str = None, **kw):
    sel = target or value
    if text:
        ctx.page.locator(f"button:has-text('{text}'), a:has-text('{text}')").first.click()
        return f"点击 '{text}'"
    ctx.page.locator(ctx.selector(sel)).first.click()
    return f"点击 {sel}"


@action("fill")
def do_fill(ctx, label: str = None, value: Any = None, selector: str = None, **kw):
    v = ctx.resolve(value)
    if selector:
        ctx.page.locator(ctx.selector(selector)).first.fill(str(v))
    else:
        ctx.ui.fill(ctx.page, ctx.label_of(label), v)
    return f"{label or selector} = {v}"


@action("select")
def do_select(ctx, label: str = None, option: str = None, index: int = None, **kw):
    picked = ctx.ui.select(ctx.page, ctx.label_of(label), option=option, index=index)
    ctx.vars[f"selected_{label}"] = picked
    return f"{label} 选中 '{picked}'"


@action("date_range")
def do_date_range(ctx, label: str = None, start: str = None, end: str = None, **kw):
    ctx.ui.date_range(ctx.page, ctx.label_of(label), ctx.resolve(start), ctx.resolve(end))
    return f"{label} = {start} ~ {end}"


@action("wait")
def do_wait(ctx, ms: int = 500, value: int = None, **kw):
    ctx.page.wait_for_timeout(value or ms)
    return f"等待 {value or ms}ms"


@action("wait_api")
def do_wait_api(ctx, url: str = None, value: str = None, timeout: int = 15000, **kw):
    """
    等列表接口返回并把 JSON 存进上下文。
    Element 表格没有可靠的 loading 结束信号，等接口比等元素稳。
    """
    frag = url or value or ctx.config.list_api
    if not frag:
        ctx.page.wait_for_timeout(1500)
        return "等待网络空闲"
    with ctx.page.expect_response(
        lambda r: frag in r.url and r.status == 200, timeout=timeout
    ) as info:
        pass
    try:
        ctx.last_api = info.value.json()
    except Exception:
        ctx.last_api = None
    ctx.page.wait_for_timeout(300)  # 留出渲染时间
    return f"接口 {frag} 已返回"


@action("search")
def do_search(ctx, **kw):
    """
    点搜索 + 等接口，合成一步，配置里最常用。

    等接口超时有两种完全不同的原因：
    1. 批量执行并发跑多个页面时，接口响应比单独手动测慢一截，偶尔卡过
       超时线——接口本身没问题，纯粹是撞上了并发高峰。超时先重试一次
       （等一拍再点一次搜索），大概率第二次就赶上峰值过去了。
    2. list_api 这个 URL 片段配错了/过期了——页面搜索其实成功了，只是
       等的 URL 跟实际触发的接口对不上，两次重试都不会有用（等的目标
       根本不存在）。两次都超时后，失败消息里会把这期间实际收到的
       JSON 接口列出来，方便直接判断是"接口慢"还是"list_api 配错了"。
    超时时长可以在页面配置里用 search_timeout（毫秒）单独调，默认 30s。

    搜索前后各截一张图，作为报告里的"搜索前/搜索后"对比——这条用例
    十有八九是通过的，通过了也看不出页面到底有没有真的按条件筛出结果，
    截图比一句"执行搜索"直观。
    """
    before = ctx.shot("search_before")
    frag = ctx.config.list_api
    btn = ctx.selector("search_btn")
    timeout = ctx.config.search_timeout
    if frag:
        retried = False
        for attempt in range(2):
            try:
                with ctx.page.expect_response(
                    lambda r: frag in r.url and r.status == 200, timeout=timeout
                ) as info:
                    ctx.page.locator(btn).first.click()
                break
            except PlaywrightTimeoutError as e:
                if attempt == 1:
                    # 两次都等不到——很可能不是"响应慢"，是 list_api 这个
                    # URL 片段本身就配错了，页面搜索其实早就成功了，只是
                    # 触发的接口 URL 跟配置里等的那个对不上，永远等不到。
                    # api_log 里这段时间实际收到的 JSON 接口摆出来，比让人
                    # 去猜"到底是慢还是配错了"直接得多。
                    seen = [c["url"] for c in ctx.api_log[-8:]]
                    hint = (f"配置的 list_api '{frag}' 一直没等到匹配的响应。"
                           f"这期间实际收到的接口: {seen}——如果这里面有一个明显才是"
                           f"真正的列表接口，说明 list_api 配错了，不是接口慢，"
                           f"改一下配置里的 list_api 就行。"
                           if seen else
                           f"配置的 list_api '{frag}' 一直没等到匹配的响应，这期间"
                           f"也没收到任何 JSON 接口响应，可能页面根本没发起请求。")
                    raise PlaywrightTimeoutError(f"{e}\n{hint}") from e
                retried = True
                ctx.page.wait_for_timeout(800)   # 给并发高峰一点时间过去，再重试
        try:
            ctx.last_api = info.value.json()
        except Exception:
            ctx.last_api = None
    else:
        ctx.page.locator(btn).first.click()
        ctx.page.wait_for_timeout(1500)
        retried = False
    ctx.page.wait_for_timeout(400)
    msg = "执行搜索（重试后成功）" if retried else "执行搜索"
    after = ctx.shot("search_after")
    detail = None
    if before or after:
        detail = {"images": [{"label": "搜索前", "path": before},
                            {"label": "搜索后", "path": after}]}
    return msg, detail


# ============ 第一期：全自动健康巡检 ============

@action("check_buttons")
def do_check_buttons(ctx, skip: list = None, max_buttons: int = 20, **kw):
    """
    巡检工具栏按钮是否可用。逐个点非破坏性按钮，检查：点得动、不报错、
    弹窗能正常打开（随后关闭）、跳转后不是空白页。破坏性按钮（删除/停用等）
    只确认存在且可点，绝不点下去。搜索/重置/导出有各自的专用用例，这里跳过
    避免重复触发下载等副作用。
    """
    page, ui = ctx.page, ctx.ui
    always_skip = set(_i18n_words("export", "search", "reset", "refresh"))
    user_skip = set(skip or [])
    texts = ui.toolbar_button_texts(page)
    if not texts:
        return "页面没有可巡检的工具栏按钮"

    home = page.url
    checked, dangerous, problems = [], [], []
    for t in texts[:max_buttons]:
        if t in always_skip or any(s in t for s in user_skip):
            continue
        if any(k in t for k in ui.DESTRUCTIVE):
            btn = ui.button(page, t)
            try:
                if btn.count() == 0:
                    continue
                if btn.is_disabled():
                    problems.append(f"'{t}' 处于禁用态")
            except Exception:
                pass
            dangerous.append(t)
            continue

        btn = ui.button(page, t)
        try:
            if btn.count() == 0 or not btn.first.is_visible():
                continue
        except Exception:
            continue

        base_console = len(ctx.console_errors)
        base_failed = len(ctx.failed_requests)
        before = page.url
        try:
            btn.first.click(timeout=4000)
        except Exception as e:
            problems.append(f"'{t}' 点击失败: {type(e).__name__}")
            continue
        page.wait_for_timeout(700)

        new_errs = [e for e in ctx.console_errors[base_console:]
                    if "ResizeObserver" not in e and "favicon" not in e]
        new_5xx = ctx.failed_requests[base_failed:]
        if new_errs:
            problems.append(f"'{t}' 触发前端报错: {new_errs[0][:120]}")
        if new_5xx:
            problems.append(f"'{t}' 触发失败请求: {new_5xx[0][:120]}")

        if ui.dialog_visible(page):
            ui.close_dialog(page)
        elif page.url != before:
            # 跳转了：确认新页面不是空白/错误页，然后退回
            try:
                body_len = len(page.locator("body").inner_text().strip())
            except Exception:
                body_len = 0
            if body_len < 20:
                problems.append(f"'{t}' 跳转后页面疑似空白")
            try:
                page.go_back(wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(500)
            except Exception:
                pass
            # 退不回原页就停止后续巡检，避免在错页上连锁误报
            if page.url.split("?")[0] != home.split("?")[0]:
                checked.append(t)
                break
        checked.append(t)

    if problems:
        _fail(f"按钮巡检发现 {len(problems)} 个问题: {problems[:5]}")
    tail = f"（破坏性按钮仅确认存在: {dangerous}）" if dangerous else ""
    return f"巡检 {len(checked)} 个按钮均正常 ✓{tail}"


# 明显的渲染异常标记：整格等于这些值，说明前端没拿到/没处理好数据
_GARBAGE_EXACT = {"undefined", "null", "nan", "none", "[object object]",
                  "invalid date", "0000-00-00", "0000-00-00 00:00:00"}
_GARBAGE_SUB = ("[object object]", "invalid date", "undefined")


@action("assert_no_render_garbage")
def as_no_render_garbage(ctx, columns: list = None, extra: list = None, **kw):
    """
    扫全表找渲染异常，零配置就能抓"接口对但前端展示错"那一类：
    undefined/null/NaN/[object Object]/Invalid Date，以及时间列里的裸时间戳。
    """
    data = ctx.table_data()
    if not data:
        return "列表为空，跳过渲染检查"
    exact = set(_GARBAGE_EXACT) | {str(e).lower() for e in (extra or [])}
    cols = columns or list(data[0].keys())
    bad = []
    for i, row in enumerate(data):
        for c in cols:
            if c not in row:
                continue
            s = N.text(row[c])
            low = s.lower()
            if low in exact or any(g in low for g in _GARBAGE_SUB):
                bad.append(f"行{i+1} {c}='{s}'")
            elif ("时间" in c or "日期" in c) and re.fullmatch(r"\d{10,13}", s):
                bad.append(f"行{i+1} {c} 疑似未格式化时间戳='{s}'")
    if bad:
        _fail(f"列表有 {len(bad)} 处渲染异常: {bad[:5]}{_diag_suffix(ctx)}")
    return f"渲染检查通过（{len(data)} 行 × {len(cols)} 列）✓"


# ============ 多语言 ============

@action("switch_language")
def do_switch_language(ctx, to: str = None, **kw):
    """
    切换页面语言。

    语言切换控件（图标/下拉/独立按钮）不像表单、表格那样有统一的 DOM 约定，
    没法像其他动作一样零配置自动识别，需要在项目配置里指定一次：
        languages:
          switcher_trigger: ".lang-switch"   # F12 找触发元素的选择器
          options: {zh: 中文, en: English}    # 每种语言在切换菜单里显示的文字
    切完记下 ctx.vars['_current_lang']，供 assert_no_mixed_language 默认取用。
    """
    langs = ctx.config.languages
    if not langs or not langs.get("switcher_trigger") or not langs.get("options"):
        raise LookupError(
            "配置里没有 languages.switcher_trigger / languages.options，无法切换语言。"
            "请在项目配置里补上（F12 找语言切换按钮的选择器）"
        )
    options = langs["options"]
    if to not in options:
        raise LookupError(f"未知语言 '{to}'，配置里只有: {list(options)}")

    page = ctx.page
    target_text = options[to]
    page.locator(langs["switcher_trigger"]).first.click(timeout=5000)
    page.wait_for_timeout(300)
    page.get_by_text(target_text, exact=True).first.click(timeout=5000)
    page.wait_for_timeout(1000)
    ctx.vars["_current_lang"] = to
    return f"已切换到 {target_text}"


# 形如 common.search / menu.order.list —— i18n 库找不到翻译时常见的 fallback
# 行为就是直接把 key 原样显示出来，肉眼很容易当成正常文案扫过去
_I18N_KEY_RE = re.compile(r"^[a-zA-Z][\w]*(\.[a-zA-Z][\w]*){1,}$")
# 没被替换掉的模板占位符，比如 {{ common.search }} 或 ${title}
_I18N_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|\$\{[^}]*\}")
_CJK_RE = re.compile(r"[一-鿿]")


@action("assert_no_i18n_leak")
def as_no_i18n_leak(ctx, columns: list = None, **kw):
    """
    检查表格里有没有漏翻译的原始 key（如 'common.search'）或没渲染掉的模板
    占位符（如 '{{xxx}}'）——翻译文件漏配某个 key 时，前端常见的失败模式
    是直接显示 key 本身而不是报错，人工核对很容易漏看。
    """
    data = ctx.table_data()
    if not data:
        return "列表为空，跳过翻译泄漏检查"
    cols = columns or list(data[0].keys())
    bad = []
    for i, row in enumerate(data):
        for c in cols:
            if c not in row:
                continue
            s = N.text(row[c])
            if not s:
                continue
            if _I18N_KEY_RE.match(s) or _I18N_TEMPLATE_RE.search(s):
                bad.append(f"行{i+1} {c}='{s}'")
    if bad:
        _fail(f"发现 {len(bad)} 处疑似漏翻译: {bad[:5]}")
    return "翻译泄漏检查通过 ✓"


@action("assert_no_mixed_language")
def as_no_mixed_language(ctx, expect: str = None, columns: list = None, **kw):
    """
    切到某语言后，页面不该还残留中文——这是最常见的漏翻译表现：某个字段
    没配对应语言的翻译，UI 就直接落回默认的中文文案。

    只检查"非中文语言下不该有中文"这一个方向，不反过来查"中文页面不该有
    英文"——中文界面里出现英文缩写、品牌名、ID 之类完全正常，反向检查
    误报会很多。expect 不传就用 switch_language 记下的当前语言。
    """
    lang = expect or ctx.vars.get("_current_lang")
    if not lang:
        raise LookupError("不知道当前应该是什么语言：传 expect 参数，或者先执行 switch_language")
    if lang == "zh":
        return "当前语言是中文，跳过混用检查"
    data = ctx.table_data()
    if not data:
        return "列表为空，跳过多语言检查"
    cols = columns or list(data[0].keys())
    bad = []
    for i, row in enumerate(data):
        for c in cols:
            if c not in row:
                continue
            s = N.text(row[c])
            if s and _CJK_RE.search(s):
                bad.append(f"行{i+1} {c}='{s}'")
    if bad:
        _fail(f"应为 '{lang}' 但发现 {len(bad)} 处残留中文: {bad[:5]}")
    return f"多语言检查通过（{lang}）✓"


@action("check_select_options")
def do_check_select_options(ctx, label: str = None, max_options: int = 6, **kw):
    """
    遍历一个下拉筛选的前 N 个选项，逐个选中+搜索，确认每个选项都能正常筛选、
    不报错。比只测第一个选项更能抓到"某个状态码筛选后页面崩了"这类问题。
    循环结束后 ${selected_label} 停在最后一个选项，供后续列断言复用。
    """
    page, ui = ctx.page, ctx.ui
    candidates = ctx.label_of(label)
    try:
        raw_opts = ui.list_options(page, candidates)
    except Exception:
        # 级联选择器（如「城市」依赖「国家」）在父级没选时点不开，属于正常情况
        # （扫描阶段已经因为取不到选项而不会生成这条用例；这里是手写配置时的兜底）
        return f"下拉 '{label}' 打不开（可能依赖其他字段先选），跳过"
    opts = [o for o in raw_opts if o and o not in ("全部", "请选择", "不限")][:max_options]
    if not opts:
        return f"下拉 '{label}' 无可选项，跳过"
    problems = []
    for o in opts:
        base = len(ctx.console_errors)
        try:
            picked = ui.select(page, candidates, option=o)
        except Exception as e:
            problems.append(f"选项 '{o}' 选择失败: {type(e).__name__}")
            continue
        ctx.vars[f"selected_{label}"] = picked
        try:
            do_search(ctx)
        except Exception as e:
            problems.append(f"选项 '{o}' 搜索异常: {type(e).__name__}: {e}")
            continue
        errs = [e for e in ctx.console_errors[base:]
                if "ResizeObserver" not in e and "favicon" not in e]
        if errs:
            problems.append(f"选项 '{o}' 触发报错: {errs[0][:100]}")
    if problems:
        _fail(f"下拉 '{label}' 有 {len(problems)} 个选项异常: {problems[:5]}")
    return f"下拉 '{label}' 遍历 {len(opts)} 个选项均正常 ✓"


@action("capture")
def do_capture(ctx, name: str = "snapshot", value: str = None, **kw):
    """把当前表格快照存进变量池，供后面和导出文件比对"""
    key = value or name
    ctx.data[key] = ctx.table_data()
    return f"抓取 {len(ctx.data[key])} 行到 '{key}'"


@action("capture_all_pages")
def do_capture_all(ctx, name: str = "all_pages", max_pages: int = 20, **kw):
    """翻页抓全量，用于和全量导出比对"""
    rows, pages = [], 0
    while pages < max_pages:
        rows.extend(ctx.table_data())
        pages += 1
        if not ctx.ui.next_page(ctx.page):
            break
        ctx.page.wait_for_timeout(800)
    ctx.data[name] = rows
    return f"翻 {pages} 页共抓取 {len(rows)} 行"


# ============ 列表断言 ============

@action("assert_row_count")
def as_row_count(ctx, min: int = None, max: int = None, equals: int = None, **kw):
    n = ctx.ui.row_count(ctx.page)
    if equals is not None and n != equals:
        _fail(f"行数应为 {equals}，实际 {n}{_diag_suffix(ctx)}")
    if min is not None and n < min:
        _fail(f"行数应 >= {min}，实际 {n}{_diag_suffix(ctx)}")
    if max is not None and n > max:
        _fail(f"行数应 <= {max}，实际 {n}")
    return f"行数 {n} ✓"


@action("assert_headers")
def as_headers(ctx, contains: list = None, equals: list = None, value: list = None, **kw):
    hs = ctx.canonical_headers()
    want = contains or value
    if equals is not None and hs != equals:
        _fail(f"表头不匹配\n期望: {equals}\n实际: {hs}{_diag_suffix(ctx)}")
    if want:
        missing = [c for c in want if c not in hs]
        if missing:
            _fail(f"表头缺少 {missing}，实际表头: {hs}{_diag_suffix(ctx)}")
    return f"表头校验通过 ({len(hs)} 列)"


@action("assert_column_all")
def as_col_all(ctx, column: str = None, equals: Any = None, contains: str = None,
               matches: str = None, kind: str = "auto", **kw):
    """搜索条件的核心断言：某列所有值都满足条件"""
    vals = ctx.ui.column_values(ctx.page, ctx.column_of(column))
    if not vals:
        return "列表为空，跳过列值校验"
    bad = []
    for i, v in enumerate(vals):
        ok = True
        if equals is not None:
            ok = N.compare(v, ctx.resolve(equals), kind)
        elif contains is not None:
            ok = ctx.resolve(contains) in N.text(v)
        elif matches is not None:
            ok = re.search(matches, N.text(v)) is not None
        if not ok:
            bad.append(f"行{i+1}='{v}'")
    if bad:
        _fail(f"列 '{column}' 有 {len(bad)}/{len(vals)} 行不符: {bad[:5]}")
    return f"列 '{column}' 全部 {len(vals)} 行符合 ✓"


@action("assert_column_range")
def as_col_range(ctx, column: str = None, start: str = None, end: str = None,
                 kind: str = "date", **kw):
    """时间/数值范围筛选断言"""
    vals = ctx.ui.column_values(ctx.page, ctx.column_of(column))
    fn = N.TYPE_MAP.get(kind, N.auto)
    lo, hi = (fn(ctx.resolve(start)) if start else None), (fn(ctx.resolve(end)) if end else None)
    bad = []
    for i, v in enumerate(vals):
        pv = fn(v)
        if pv is None:
            continue
        if lo is not None and pv < lo:
            bad.append(f"行{i+1}='{v}' < {start}")
        if hi is not None and pv > hi:
            bad.append(f"行{i+1}='{v}' > {end}")
    if bad:
        _fail(f"列 '{column}' 超出范围: {bad[:5]}")
    return f"列 '{column}' {len(vals)} 行均在范围内 ✓"


@action("assert_column_not_empty")
def as_col_not_empty(ctx, column: str = None, value: str = None, allow_ratio: float = 0.0, **kw):
    col = column or value
    vals = ctx.ui.column_values(ctx.page, ctx.column_of(col))
    if not vals:
        return "列表为空"
    empties = [i for i, v in enumerate(vals) if N.is_empty(v)]
    ratio = len(empties) / len(vals)
    if ratio > allow_ratio:
        _fail(f"列 '{col}' 空值率 {ratio:.0%} 超过允许的 {allow_ratio:.0%}（空行: {[i+1 for i in empties[:5]]}）")
    return f"列 '{col}' 空值率 {ratio:.0%} ✓"


@action("assert_sorted")
def as_sorted(ctx, column: str = None, order: str = "desc", kind: str = "auto", **kw):
    vals = [N.TYPE_MAP.get(kind, N.auto)(v) for v in ctx.ui.column_values(ctx.page, ctx.column_of(column))]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return "数据不足，跳过排序校验"
    ok = all(vals[i] >= vals[i+1] for i in range(len(vals)-1)) if order == "desc" \
        else all(vals[i] <= vals[i+1] for i in range(len(vals)-1))
    if not ok:
        _fail(f"列 '{column}' 未按 {order} 排序")
    return f"列 '{column}' {order} 排序正确 ✓"


@action("assert_inputs_empty")
def as_inputs_empty(ctx, labels: list = None, value: list = None, **kw):
    """重置按钮的断言"""
    dirty = []
    for lb in (labels or value or []):
        try:
            v = ctx.ui.get_input_value(ctx.page, ctx.label_of(lb))
            if not N.is_empty(v):
                dirty.append(f"{lb}='{v}'")
        except LookupError:
            continue
    if dirty:
        _fail(f"重置后仍有值: {dirty}")
    return "所有输入框已清空 ✓"


# ============ 前后端一致性断言 ============

@action("assert_api_matches_table")
def as_api_table(ctx, list_path: str = "data.records", mapping: dict = None, **kw):
    """
    把接口 JSON 和表格渲染值逐字段对比。
    专抓"接口对但前端展示错"的 bug：金额单位、时区、状态码映射。
    这类问题在后台系统里出现频率远高于接口本身出错。
    """
    if not ctx.last_api:
        return "无接口数据，跳过"
    node = ctx.last_api
    for k in list_path.split("."):
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return f"接口结构里找不到 {list_path}，跳过"
    if not isinstance(node, list) or not node:
        return "接口返回空列表，跳过"

    table = ctx.table_data()
    if len(table) != len(node):
        _fail(f"接口返回 {len(node)} 条，表格渲染 {len(table)} 行")

    mapping = mapping or {}
    diffs = []
    for i, (api_row, ui_row) in enumerate(zip(node, table)):
        for col, field in mapping.items():
            if col not in ui_row:
                continue
            if not N.compare(ui_row[col], api_row.get(field)):
                diffs.append(f"行{i+1} {col}: 页面='{ui_row[col]}' 接口='{api_row.get(field)}'")
    if diffs:
        _fail(f"前后端展示不一致 {len(diffs)} 处: {diffs[:5]}")
    return f"接口与表格 {len(table)} 行一致 ✓"


# ============ 新增 / 修改 ============

@action("fill_form")
def do_fill_form(ctx, fields: dict = None, in_dialog: bool = True, **kw):
    """在弹窗里批量填表。值支持 ${random} / ${email} 等占位符。"""
    filled = {}
    for label, raw in (fields or {}).items():
        v = ctx.resolve(raw)
        try:
            ctx.ui.fill(ctx.page, ctx.label_of(label), v)
        except LookupError:
            ctx.ui.select(ctx.page, ctx.label_of(label), option=str(v))
        filled[label] = v
        ctx.vars[f"form_{label}"] = v
    return f"填写 {len(filled)} 个字段: {filled}"


@action("assert_message")
def as_message(ctx, contains: str = None, value: str = None, **kw):
    want = contains or value or "成功"
    try:
        txt = ctx.ui.message_text(ctx.page)
    except Exception:
        _fail(f"未出现提示消息（期望包含 '{want}'）")
    if want not in txt:
        _fail(f"提示消息为 '{txt}'，期望包含 '{want}'")
    return f"提示 '{txt}' ✓"


@action("assert_in_list")
def as_in_list(ctx, column: str = None, value: str = None, **kw):
    """
    新增/修改的闭环断言：回列表里搜到刚才那条。
    只断言 toast 成功是没有意义的 —— 提示成功但数据没落库的 bug 很常见。
    """
    expect = ctx.resolve(value)
    vals = ctx.ui.column_values(ctx.page, ctx.column_of(column))
    if not any(N.compare(v, expect) for v in vals):
        _fail(f"列 '{column}' 中找不到刚提交的值 '{expect}'，当前列值: {vals[:5]}")
    return f"列表中找到 '{expect}' ✓"


@action("confirm")
def do_confirm(ctx, ok: bool = True, **kw):
    ctx.ui.confirm_dialog(ctx.page, ok=ok)
    return "确认弹窗"


# ============ 导出 ============

@action("export_and_verify")
def do_export(ctx, compare_with: str = None, columns: list = None,
              row_count: str = "total", timeout: int = 90000,
              header_match: bool = True, sample: int = 20, **kw):
    """
    导出验证三件事：
      1. 文件能下载、能解析
      2. 表头与页面表头一致
      3. 行数与分页总数一致（导出通常是全量而非当前页）
      4. 抽样行的字段值与页面一致（归一化后比对）
    """
    from .export_verify import verify_export
    return verify_export(
        ctx, compare_with=compare_with, columns=columns,
        row_count_mode=row_count, timeout=timeout,
        header_match=header_match, sample=sample,
    )


# ============ 健康检查 ============

@action("assert_no_console_error")
def as_no_console(ctx, ignore: list = None, **kw):
    """
    只降级成警告，不判失败：页面数据、表头、行数这些内容类断言如果都通过了，
    说明用户看到的东西是对的，一条无关资源报错不该拖垮整条用例。
    真出现"页面显示不对"时，assert_row_count/assert_headers/
    assert_no_render_garbage 失败信息里会自动带上这些报错，不用靠这条断言来抓。
    """
    ignore = ignore or ["favicon", "ResizeObserver"]
    errs = [e for e in ctx.console_errors if not any(p in e for p in ignore)]
    if errs:
        _warn(f"控制台有 {len(errs)} 条报错（页面内容本身正常，不算失败）: {errs[:3]}")
    return "无控制台报错 ✓"


@action("assert_no_failed_request")
def as_no_failed_req(ctx, ignore: list = None, **kw):
    """同 assert_no_console_error：只警告不判失败，理由见上面那条的说明。"""
    ignore = ignore or []
    bad = [r for r in ctx.failed_requests if not any(p in r for p in ignore)]
    if bad:
        _warn(f"有 {len(bad)} 个请求失败（页面内容本身正常，不算失败）: {bad[:3]}")
    return "无失败请求 ✓"


@action("screenshot")
def do_screenshot(ctx, name: str = None, value: str = None, **kw):
    path = ctx.shot(name or value or "manual")
    return f"截图 {path}"


# ============ 第二期：新增/修改闭环验证 ============
#
# 铁律：只动自己造的数据。这里每个动作都要求调用方明确给出 identity 列和值，
# find_row_by 找不到、或者匹配上不止一行都直接报错——绝不在一堆真实业务数据
# 里瞎猜一行去改/删。

def _open_dialog(ctx, trigger_selector: str) -> None:
    ctx.page.locator(trigger_selector).first.click(timeout=5000)
    dlg = ctx.ui.dialog(ctx.page)
    dlg.wait_for(state="visible", timeout=5000)
    ctx.page.wait_for_timeout(500)   # 等弹窗里的下拉/字典数据加载完


def _fill_fields(ctx, fields: List[Dict[str, Any]], values: Dict[str, Any]) -> None:
    by_label = {f["label"]: f for f in fields}
    for label, v in values.items():
        f = by_label.get(label, {"label": label, "type": "text"})
        f = dict(f, label=ctx.label_of(f["label"]))
        ctx.ui.set_field_value(ctx.page, f, v)


def _submit(ctx) -> str:
    ctx.page.locator(ctx.selector("submit_btn")).first.click(timeout=5000)
    ctx.page.wait_for_timeout(300)
    try:
        return ctx.ui.message_text(ctx.page, timeout=5000)
    except Exception:
        return ""


@action("assert_form_errors")
def as_form_errors(ctx, expect: list = None, **kw):
    """
    必填校验：假定新增/编辑弹窗已经打开，什么都不填直接点提交，
    断言该报错的字段都报错了。expect 不给就只要求"至少报了点什么"。
    """
    _submit(ctx)
    got = ctx.form_error_labels()
    if not got:
        texts = ctx.ui.form_error_texts(ctx.page)
        if texts:
            got = texts   # 有些页面错误信息不挂在 label 对应的 form-item 上
    if expect:
        missing = [f for f in expect if not any(f in g for g in got)]
        if missing:
            _fail(f"必填校验缺失，这些字段应该报错但没报: {missing}（实际报错: {got}）")
    elif not got:
        _fail("提交空表单没有触发任何校验提示，必填校验可能没生效")
    return f"必填校验通过，{len(got)} 项报错符合预期"


@action("create_and_verify")
def do_create_and_verify(ctx, fields: list = None, identity: str = None,
                         identity_column: str = None, only_required: bool = False,
                         export: str = None, **kw):
    """
    新增闭环：打开新增弹窗 → 数据工厂生成合法值 → 逐字段填写 → 提交 →
    断言成功提示 → 回列表搜索 → 断言每个字段值和填的一致。

    identity 是拿来定位这条新记录的字段（通常是"名称"这类唯一性字段），
    identity_column 是它对应的表格列名（大多数情况和 label 同名，不同名时指定）。
    生成的值和这条记录的行号存进 ctx.vars，供后续 edit/detail/delete 步骤使用。
    """
    if not fields:
        _fail("没有可用的表单字段（扫描时没探测到新增弹窗结构）")
    identity = identity or fields[0]["label"]
    identity_column = identity_column or identity

    _open_dialog(ctx, ctx.selector("create_btn"))
    values = DF.fill_values(fields, only_required=only_required)
    if identity not in values:
        # identity 字段必须真的填了值，不然后面搜不到这条记录
        f = next((x for x in fields if x["label"] == identity), {"label": identity, "type": "text"})
        v = DF.value_for(f) or f"{DF.AUTO_PREFIX}{identity}"
        values[identity] = v
    _fill_fields(ctx, fields, values)

    msg = _submit(ctx)
    if "成功" not in msg and "失败" in msg:
        _fail(f"提交后提示: '{msg}'，疑似保存失败")

    ident_val = str(values[identity])
    try:
        ctx.ui.fill(ctx.page, ctx.label_of(identity), ident_val)
        do_search(ctx)
    except LookupError:
        # identity 字段不在搜索表单里（不是每个新增字段都能搜），退回整页扫
        do_search(ctx)

    row = ctx.find_row_by(identity_column, ident_val)
    row_data = ctx.table_data()[row]

    diffs = []
    checked = 0
    for label, v in values.items():
        col_val = row_data.get(label)
        if col_val is None:
            continue   # 表格里没有这一列（比如密码字段），跳过不算
        checked += 1
        if not N.compare(v, col_val):
            diffs.append(f"{label}: 填的='{v}' 列表显示='{col_val}'")
    if diffs:
        _fail(f"新增后列表数据不一致 {len(diffs)}/{checked} 处: {diffs[:5]}")

    ctx.vars["created_row"] = row
    ctx.vars["created_identity"] = ident_val
    ctx.vars["created_identity_column"] = identity_column
    ctx.vars["created_fields"] = values
    ctx.vars["created_field_schema"] = fields   # 供 edit_and_verify 按类型正确填表
    if export:
        ctx.vars[export] = ident_val
    return f"新增闭环通过：填 {len(values)} 项，列表比对 {checked} 项一致 ✓"


@action("assert_form_prefilled")
def as_form_prefilled(ctx, identity_column: str = None, action: str = "编辑", **kw):
    """
    编辑回显：打开编辑弹窗，断言表单里的值和列表当前显示的值一致。
    "提示成功但其实编辑弹窗回显不出原值"是后台系统的常见 bug，这条专门抓这个。
    """
    identity_column = identity_column or ctx.vars.get("created_identity_column")
    ident_val = ctx.vars.get("created_identity")
    if not identity_column or ident_val is None:
        _fail("没有可用的记录定位信息，请先执行 create_and_verify")

    row = ctx.find_row_by(identity_column, ident_val)
    row_data = ctx.table_data()[row]
    ctx.ui.row_action(ctx.page, row, action)

    form_vals = ctx.dialog_field_values()
    diffs = []
    checked = 0
    for label, list_val in row_data.items():
        if label not in form_vals or not list_val:
            continue
        checked += 1
        if not N.compare(list_val, form_vals[label]):
            diffs.append(f"{label}: 列表='{list_val}' 编辑框回显='{form_vals[label]}'")
    if diffs:
        _fail(f"编辑弹窗回显不一致 {len(diffs)}/{checked} 处: {diffs[:5]}")
    ctx.vars["edit_row"] = row
    return f"编辑回显校验通过，比对 {checked} 项 ✓"


@action("edit_and_verify")
def do_edit_and_verify(ctx, fields: dict = None, identity_column: str = None, **kw):
    """
    修改闭环：假定编辑弹窗已打开（前一步通常是 assert_form_prefilled），
    改指定字段 → 提交 → 断言列表对应行确实变了。
    """
    if not fields:
        _fail("edit_and_verify 需要指定要改哪些字段")
    identity_column = identity_column or ctx.vars.get("created_identity_column")
    ident_val = ctx.vars.get("created_identity")
    if identity_column is None or ident_val is None:
        _fail("没有可用的记录定位信息，请先执行 create_and_verify")

    resolved = {label: ctx.resolve(v) for label, v in fields.items()}
    schema = ctx.vars.get("created_field_schema") or []
    _fill_fields(ctx, schema, resolved)
    msg = _submit(ctx)
    if "成功" not in msg and "失败" in msg:
        _fail(f"提交后提示: '{msg}'，疑似保存失败")

    # identity 本身没改的话，还是用老值去定位这一行
    row = ctx.find_row_by(identity_column, ident_val)
    row_data = ctx.table_data()[row]
    diffs = []
    for label, v in resolved.items():
        col_val = row_data.get(label)
        if col_val is None:
            continue
        if not N.compare(v, col_val):
            diffs.append(f"{label}: 改成='{v}' 列表显示='{col_val}'")
    if diffs:
        _fail(f"修改后列表没有对应更新 {len(diffs)} 处: {diffs[:5]}")
    ctx.vars.setdefault("created_fields", {}).update(resolved)
    return f"修改闭环通过：改了 {len(resolved)} 项，列表已同步 ✓"


@action("assert_detail_matches")
def as_detail_matches(ctx, identity_column: str = None, action: str = "查看", **kw):
    """
    详情一致性：打开详情弹窗/页面，断言每个字段和列表当前显示的值一致。
    "列表对但详情是老值"是后台系统另一个高频 bug。
    """
    identity_column = identity_column or ctx.vars.get("created_identity_column")
    ident_val = ctx.vars.get("created_identity")
    if not identity_column or ident_val is None:
        _fail("没有可用的记录定位信息，请先执行 create_and_verify")

    row = ctx.find_row_by(identity_column, ident_val)
    row_data = ctx.table_data()[row]
    ctx.ui.row_action(ctx.page, row, action)

    detail = ctx.detail_values()
    if ctx.ui.dialog_visible(ctx.page):
        ctx.ui.close_dialog(ctx.page)
    diffs = []
    checked = 0
    for label, list_val in row_data.items():
        if label not in detail or not list_val:
            continue
        checked += 1
        if not N.compare(list_val, detail[label]):
            diffs.append(f"{label}: 列表='{list_val}' 详情='{detail[label]}'")
    if diffs:
        _fail(f"详情和列表不一致 {len(diffs)}/{checked} 处: {diffs[:5]}")
    if checked == 0:
        return "详情弹窗没有能对上的字段名，跳过比对（可能详情用了不同措辞）"
    return f"详情一致性校验通过，比对 {checked} 项 ✓"


@action("delete_and_verify")
def do_delete_and_verify(ctx, identity_column: str = None, action: str = "删除", **kw):
    """
    清理闭环：删掉本次执行自己创建的那条记录，断言列表里确实没了。
    只删 ctx.vars 里记录的、本次创建的那一条——不接受传入任意 identity，
    避免误删非自动化数据。
    """
    identity_column = identity_column or ctx.vars.get("created_identity_column")
    ident_val = ctx.vars.get("created_identity")
    if not identity_column or ident_val is None:
        return "没有需要清理的记录，跳过"
    if not DF.is_auto_data(ident_val):
        _fail(f"'{ident_val}' 不像是自动化创建的数据（没有 {DF.AUTO_PREFIX} 前缀），"
             f"为避免误删真实数据，拒绝执行删除")

    try:
        row = ctx.find_row_by(identity_column, ident_val)
    except LookupError:
        return "记录已不在列表中，可能已被清理"
    ctx.ui.row_action(ctx.page, row, action)
    if ctx.ui.dialog_visible(ctx.page):
        try:
            ctx.ui.confirm_dialog(ctx.page, ok=True)
        except Exception:
            pass
    ctx.page.wait_for_timeout(600)

    try:
        ctx.find_row_by(identity_column, ident_val)
        _fail(f"删除后记录仍在列表中: {ident_val}")
    except LookupError:
        pass
    return f"清理完成：'{ident_val}' 已从列表移除 ✓"


@action("toggle_status_and_verify")
def do_toggle_status(ctx, column: str = "状态", action: str = None,
                     identity_column: str = None, **kw):
    """
    状态流转验证：点行内的状态切换按钮（"设为失效"/"停用"这类，不同系统
    措辞不一样，不给 action 就现场探测），断言状态列真的变了。

    只在本次自动创建的记录上操作——铁律和 delete_and_verify 一样，
    不是自动化数据一律拒绝，行为不由传参决定。
    """
    identity_column = identity_column or ctx.vars.get("created_identity_column")
    ident_val = ctx.vars.get("created_identity")
    if not identity_column or ident_val is None:
        _fail("没有可用的记录定位信息，请先执行 create_and_verify")
    if not DF.is_auto_data(ident_val):
        _fail(f"'{ident_val}' 不像是自动化创建的数据，拒绝操作其状态")

    row = ctx.find_row_by(identity_column, ident_val)
    before = ctx.table_data()[row].get(column)

    act = action or ctx.ui.find_row_toggle_text(ctx.page, row)
    if not act:
        _fail(f"第 {row + 1} 行没有找到状态切换按钮（列 '{column}'）")
    ctx.ui.row_action(ctx.page, row, act)
    if ctx.ui.dialog_visible(ctx.page):
        try:
            ctx.ui.confirm_dialog(ctx.page, ok=True)
        except Exception:
            pass
    ctx.page.wait_for_timeout(600)

    row2 = ctx.find_row_by(identity_column, ident_val)
    after = ctx.table_data()[row2].get(column)
    if before == after:
        _fail(f"点击「{act}」后「{column}」列没有变化，仍是 '{after}'")
    return f"状态切换通过：点「{act}」后「{column}」从 '{before}' 变为 '{after}' ✓"
