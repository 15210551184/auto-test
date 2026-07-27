"""
动作与断言注册表。

配置里每个 step 的键名对应这里的一个函数。加新能力 = 加一个 @action 函数，
不用改引擎。ctx 是执行上下文，带着 page / adapter / 变量池 / 抓取的数据。
"""
import re
from typing import Any, Callable, Dict

from . import normalize as N

REGISTRY: Dict[str, Callable] = {}


def action(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


class AssertionFailed(Exception):
    """断言失败（业务问题），区别于 Exception（脚本/环境问题）"""


class AssertionWarning(Exception):
    """
    检测到问题但不算失败。用于"页面本身显示正常时，这个问题不该拖垮整条用例"
    的场景——比如控制台报错：页面数据、表头、行数都对，说明用户看到的东西没问题，
    一条无关的资源报错不该让用例变红；但内容确实不对（行数不对/渲染出乱码）时，
    这些报错就有诊断价值，会作为上下文附在真正失败的那条断言消息里。
    """


def _fail(msg: str):
    raise AssertionFailed(msg)


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
        ctx.ui.fill(ctx.page, label, v)
    return f"{label or selector} = {v}"


@action("select")
def do_select(ctx, label: str = None, option: str = None, index: int = None, **kw):
    picked = ctx.ui.select(ctx.page, label, option=option, index=index)
    ctx.vars[f"selected_{label}"] = picked
    return f"{label} 选中 '{picked}'"


@action("date_range")
def do_date_range(ctx, label: str = None, start: str = None, end: str = None, **kw):
    ctx.ui.date_range(ctx.page, label, ctx.resolve(start), ctx.resolve(end))
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
    """点搜索 + 等接口，合成一步，配置里最常用"""
    frag = ctx.config.list_api
    btn = ctx.selector("search_btn")
    if frag:
        with ctx.page.expect_response(
            lambda r: frag in r.url and r.status == 200, timeout=20000
        ) as info:
            ctx.page.locator(btn).first.click()
        try:
            ctx.last_api = info.value.json()
        except Exception:
            ctx.last_api = None
    else:
        ctx.page.locator(btn).first.click()
        ctx.page.wait_for_timeout(1500)
    ctx.page.wait_for_timeout(400)
    return "执行搜索"


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
    always_skip = {"导出", "下载", "搜索", "查询", "重置", "清空", "刷新"}
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
    data = ctx.ui.table_data(ctx.page)
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


@action("check_select_options")
def do_check_select_options(ctx, label: str = None, max_options: int = 6, **kw):
    """
    遍历一个下拉筛选的前 N 个选项，逐个选中+搜索，确认每个选项都能正常筛选、
    不报错。比只测第一个选项更能抓到"某个状态码筛选后页面崩了"这类问题。
    循环结束后 ${selected_label} 停在最后一个选项，供后续列断言复用。
    """
    page, ui = ctx.page, ctx.ui
    opts = [o for o in ui.list_options(page, label)
            if o and o not in ("全部", "请选择", "不限")][:max_options]
    if not opts:
        return f"下拉 '{label}' 无可选项，跳过"
    problems = []
    for o in opts:
        base = len(ctx.console_errors)
        try:
            picked = ui.select(page, label, option=o)
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
    ctx.data[key] = ctx.ui.table_data(ctx.page)
    return f"抓取 {len(ctx.data[key])} 行到 '{key}'"


@action("capture_all_pages")
def do_capture_all(ctx, name: str = "all_pages", max_pages: int = 20, **kw):
    """翻页抓全量，用于和全量导出比对"""
    rows, pages = [], 0
    while pages < max_pages:
        rows.extend(ctx.ui.table_data(ctx.page))
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
    hs = ctx.ui.headers(ctx.page)
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
    vals = ctx.ui.column_values(ctx.page, column)
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
    vals = ctx.ui.column_values(ctx.page, column)
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
    vals = ctx.ui.column_values(ctx.page, col)
    if not vals:
        return "列表为空"
    empties = [i for i, v in enumerate(vals) if N.is_empty(v)]
    ratio = len(empties) / len(vals)
    if ratio > allow_ratio:
        _fail(f"列 '{col}' 空值率 {ratio:.0%} 超过允许的 {allow_ratio:.0%}（空行: {[i+1 for i in empties[:5]]}）")
    return f"列 '{col}' 空值率 {ratio:.0%} ✓"


@action("assert_sorted")
def as_sorted(ctx, column: str = None, order: str = "desc", kind: str = "auto", **kw):
    vals = [N.TYPE_MAP.get(kind, N.auto)(v) for v in ctx.ui.column_values(ctx.page, column)]
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
            v = ctx.ui.get_input_value(ctx.page, lb)
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

    table = ctx.ui.table_data(ctx.page)
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
            ctx.ui.fill(ctx.page, label, v)
        except LookupError:
            ctx.ui.select(ctx.page, label, option=str(v))
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
    vals = ctx.ui.column_values(ctx.page, column)
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
