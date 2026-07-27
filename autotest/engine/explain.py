"""
把 YAML 用例翻译成人话。

不懂代码的人打开「编辑 YAML」看到的是 action/params 结构，看不出这条用例
到底在测什么。这层只做只读翻译——配置本身还是 YAML，改动照旧走 YAML 编辑器，
这里只是多一个「说明书」视图，把每个 step 转成一句中文描述。

新增/改了动作时记得在 _ACTIONS 里补一条模板；漏了也不会报错，会退化成
「动作名（参数原样打印）」，不好看但不会崩。
"""
import re
from typing import Any, Dict, List, Optional

from .models import Step

# 内部选择器别名 -> 人话按钮名。用户看到的应该是"点搜索"，不是"点 search_btn"。
_BTN_ALIASES = {
    "search_btn": "搜索", "reset_btn": "重置", "export_btn": "导出",
    "create_btn": "新增", "submit_btn": "确定/提交",
}

# ${xxx} 占位符 -> 人话解释，避免非技术读者看到一串奇怪符号
_PLACEHOLDER_HINTS = {
    "random": "系统随机生成的一串字符", "timestamp": "当前时间戳",
    "email": "系统随机生成的邮箱", "phone": "系统随机生成的手机号",
    "today": "今天日期", "now": "当前时间",
}


def _humanize(v: Any) -> Any:
    """把值里的 ${...} 占位符换成人话解释；非字符串原样返回。"""
    if not isinstance(v, str) or "${" not in v:
        return v

    def rep(m):
        k = m.group(1)
        if k in _PLACEHOLDER_HINTS:
            return f"（{_PLACEHOLDER_HINTS[k]}）"
        if k.startswith("selected_"):
            return f"（刚才在「{k[len('selected_'):]}」下拉里选中的那一项）"
        if k.startswith("form_"):
            return f"（刚才表单里「{k[len('form_'):]}」字段填的值）"
        m2 = re.fullmatch(r"days_ago_(\d+)", k)
        if m2:
            return f"（{m2.group(1)} 天前）"
        return m.group(0)  # 未知占位符原样保留，好过丢信息

    return re.sub(r"\$\{(\w+)\}", rep, v)


def _p(params: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = params.get(key, default) if isinstance(params, dict) else default
    return _humanize(v)


def _quote(v: Any) -> str:
    """给列名/标签这类固定名词加书名号；已经是「（...）」人话解释的不再嵌套加。"""
    if v in (None, ""):
        return ""
    s = str(v)
    return s if s.startswith("（") and s.endswith("）") else f"「{s}」"


def _list(v: Optional[list], n: int = 6) -> str:
    if not v:
        return ""
    v = [_humanize(x) for x in v]
    tail = f" 等 {len(v)} 项" if len(v) > n else ""
    return "、".join(str(x) for x in v[:n]) + tail


_ACTIONS: Dict[str, Any] = {}


def _tpl(name: str):
    def deco(fn):
        _ACTIONS[name] = fn
        return fn
    return deco


# ============ 动作 ============

@_tpl("goto")
def _(p):
    return f"打开页面 {_p(p, 'url') or _p(p, 'value') or '（用例配置的地址）'}"


@_tpl("fill")
def _(p):
    label = _p(p, "label") or _p(p, "selector") or "?"
    return f"在{_quote(label)}输入框里填入{_quote(_p(p, 'value', ''))}"


@_tpl("select")
def _(p):
    label = _p(p, "label", "?")
    if _p(p, "option") is not None:
        return f"在{_quote(label)}下拉框里选择{_quote(_p(p, 'option'))}"
    return f"在{_quote(label)}下拉框里选择第 {int(_p(p, 'index', 0)) + 1} 项"


@_tpl("date_range")
def _(p):
    return f"在{_quote(_p(p, 'label', '?'))}选择日期范围：{_p(p, 'start', '?')} 至 {_p(p, 'end', '?')}"


@_tpl("click")
def _(p):
    target = _p(p, "text") or _p(p, "target") or _p(p, "value") or "?"
    return f"点击「{_BTN_ALIASES.get(target, target)}」"


@_tpl("search")
def _(p):
    return "点击「搜索」按钮，等列表刷新完成"


@_tpl("wait")
def _(p):
    return f"停顿 {_p(p, 'value') or _p(p, 'ms', 500)} 毫秒"


@_tpl("wait_api")
def _(p):
    return "等待列表接口返回数据"


@_tpl("check_buttons")
def _(p):
    return ("自动巡检页面上的按钮：逐个点一下非破坏性按钮（编辑/详情/刷新等），"
            "确认点得动、不报错、弹窗能正常打开和关闭；"
            "「删除/停用」这类有风险的按钮只确认存在、不会真的点下去")


@_tpl("check_select_options")
def _(p):
    return f"把{_quote(_p(p, 'label', '?'))}下拉框的每个选项都选一遍并搜索，确认每个选项筛选后都不报错"


@_tpl("capture")
def _(p):
    name = _p(p, "value") or _p(p, "name", "snapshot")
    return f"把当前列表的数据记下来（取名「{name}」），留着后面核对用"


@_tpl("capture_all_pages")
def _(p):
    return "翻遍所有分页，把全部数据都记下来"


@_tpl("fill_form")
def _(p):
    fields = p.get("fields") if isinstance(p, dict) else None
    fields = fields or {}
    items = "、".join(f"{k}={_humanize(v)}" for k, v in list(fields.items())[:6])
    return f"在弹出的表单里填写：{items or '（无字段）'}"


@_tpl("confirm")
def _(p):
    ok = _p(p, "ok", True)
    return f"在确认弹窗里点「{'确定' if ok else '取消'}」"


@_tpl("export_and_verify")
def _(p):
    return ("点击「导出」按钮，下载文件后自动核对四件事：文件能正常打开、"
            "表头和页面一致、行数和列表总数一致、抽样几行的字段值和页面显示一致")


@_tpl("screenshot")
def _(p):
    return "手动截一张图，留作记录"


# ============ 断言 ============

@_tpl("assert_row_count")
def _(p):
    bits = []
    if _p(p, "equals") is not None:
        bits.append(f"正好 {_p(p, 'equals')} 行")
    if _p(p, "min") is not None:
        bits.append(f"至少 {_p(p, 'min')} 行")
    if _p(p, "max") is not None:
        bits.append(f"最多 {_p(p, 'max')} 行")
    return "检查列表行数：" + ("、".join(bits) if bits else "有数据")


@_tpl("assert_headers")
def _(p):
    if _p(p, "equals") is not None:
        return f"检查表头和这份列表完全一致：{_list(_p(p, 'equals'))}"
    want = _p(p, "contains") or _p(p, "value")
    return f"检查表头里包含这些列：{_list(want)}"


@_tpl("assert_column_all")
def _(p):
    col = _quote(_p(p, "column", "?"))
    if _p(p, "equals") is not None:
        return f"检查{col}列的每一行都等于{_quote(_p(p, 'equals'))}"
    if _p(p, "contains") is not None:
        return f"检查{col}列的每一行都包含{_quote(_p(p, 'contains'))}"
    if _p(p, "matches") is not None:
        return f"检查{col}列的每一行都符合规则{_quote(_p(p, 'matches'))}"
    return f"检查{col}列每一行都满足条件"


@_tpl("assert_column_range")
def _(p):
    col = _quote(_p(p, "column", "?"))
    return f"检查{col}列的值都落在 {_p(p, 'start', '不限')} ~ {_p(p, 'end', '不限')} 之间"


@_tpl("assert_column_not_empty")
def _(p):
    col = _quote(_p(p, "column") or _p(p, "value", "?"))
    ratio = _p(p, "allow_ratio", 0.0)
    return f"检查{col}列基本没有空值" + (f"（允许最多 {ratio:.0%} 为空）" if ratio else "")


@_tpl("assert_no_render_garbage")
def _(p):
    return "检查列表里没有 undefined / [object Object] / 没格式化的时间戳 这类前端显示错误"


@_tpl("assert_sorted")
def _(p):
    col = _quote(_p(p, "column", "?"))
    order = "从大到小" if _p(p, "order", "desc") == "desc" else "从小到大"
    return f"检查{col}列是按{order}排好序的"


@_tpl("assert_inputs_empty")
def _(p):
    labels = _p(p, "labels") or _p(p, "value") or []
    return f"检查这些输入框已经清空：{_list(labels) or '（配置的输入框）'}"


@_tpl("assert_api_matches_table")
def _(p):
    return "检查后台接口返回的数据和页面上实际显示的是否一致（常见的金额单位、时区、状态码显示错误就是靠这条抓）"


@_tpl("assert_message")
def _(p):
    want = _p(p, "contains") or _p(p, "value", "成功")
    return f"检查弹出的提示消息里包含{_quote(want)}"


@_tpl("assert_in_list")
def _(p):
    col = _quote(_p(p, "column", "?"))
    return f"回到列表里搜索，确认{col}列能搜到刚才提交的值"


@_tpl("assert_no_console_error")
def _(p):
    return "检查浏览器控制台没有报 JS 错误"


@_tpl("assert_no_failed_request")
def _(p):
    return "检查页面没有请求失败（接口报 500 等）"


# ============ 第二期：新增/修改闭环 ============

@_tpl("assert_form_errors")
def _(p):
    want = _p(p, "expect")
    if want:
        return f"什么都不填直接提交，检查这些必填项都报错：{_list(want)}"
    return "什么都不填直接提交，检查表单有报出校验错误"


@_tpl("create_and_verify")
def _(p):
    fields = p.get("fields") if isinstance(p, dict) else None
    n = len(fields) if fields else 0
    identity = _p(p, "identity", "")
    return (f"点「新增」，自动生成 {n} 个字段的测试数据并填写、提交，"
            f"检查提示成功后回列表能搜到这条记录（按「{identity}」定位），"
            f"且每个填过的字段在列表里显示的值和填的一致")


@_tpl("assert_form_prefilled")
def _(p):
    return "打开这条记录的编辑弹窗，检查弹窗里回显的值和列表当前显示的值一致"


@_tpl("edit_and_verify")
def _(p):
    fields = p.get("fields") if isinstance(p, dict) else {}
    items = "、".join(f"{k}→{_humanize(v)}" for k, v in (fields or {}).items())
    return f"在编辑弹窗里改：{items or '（无字段）'}，提交，检查列表对应行确实变了"


@_tpl("assert_detail_matches")
def _(p):
    return "打开这条记录的详情弹窗，检查详情里每个字段和列表当前显示的值一致"


@_tpl("delete_and_verify")
def _(p):
    return ("删除本次自动创建的这条记录（只删自动化标记过的数据，"
            "不是自动创建的会拒绝执行），检查列表里确实没了")


@_tpl("toggle_status_and_verify")
def _(p):
    col = _p(p, "column", "状态")
    return f"点这条记录的状态切换按钮（如「设为失效」），检查「{col}」列确实变了"


def explain_step(step: Step) -> str:
    fn = _ACTIONS.get(step.action)
    if fn:
        try:
            return fn(step.params)
        except Exception:
            pass
    # 没模板或模板本身出错：兜底直接把参数摊出来，不好看但不会中断整份说明
    return f"{step.action}" + (f"（参数：{step.params}）" if step.params else "")


def explain_case(raw: Dict[str, Any]) -> Dict[str, Any]:
    steps_raw = raw.get("steps", []) or []
    lines = []
    for s in steps_raw:
        try:
            lines.append(explain_step(Step.from_raw(s)))
        except Exception as e:
            lines.append(f"（这一步解析失败：{e}）")
    return {
        "name": raw.get("name", "未命名用例"),
        "tags": raw.get("tags", []) or [],
        "skip": bool(raw.get("skip", False)),
        "steps": lines,
    }


def explain_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """raw 是 yaml.safe_load() 出来的原始 dict（未经 load_config 的强类型转换）。"""
    return {
        "name": raw.get("name", ""),
        "url": raw.get("url", ""),
        "cases": [explain_case(c) for c in (raw.get("cases") or [])],
    }
