"""HTML 报告生成"""
import html
import json
import os
from typing import List, Optional

from . import tz
from .models import PageResult, Status

# 跟 web/index.html 里"只执行勾选的类别"复选框的中文标签保持一致——
# 报告名称里要用人话显示勾了哪些类别，不能只甩 tag 的英文代号。
TAG_LABELS = {
    "smoke": "冒烟", "health": "健康检查", "search": "搜索筛选",
    "list": "列表分页", "export": "导出", "crud": "新增/修改/删除", "i18n": "多语言",
}

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
background:#f5f6f8;margin:0;padding:24px;color:#1f2329}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8a9099;font-size:13px;margin-bottom:20px}
.cards{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.card{background:#fff;border-radius:8px;padding:14px 20px;min-width:100px;
border:1px solid #e6e8eb}
.card .n{font-size:26px;font-weight:600;line-height:1.2}
.card .l{font-size:12px;color:#8a9099;margin-top:2px}
.pass .n{color:#00a870}.fail .n{color:#e34d59}.skip .n{color:#bbc0c6}.warn .n{color:#e6a23c}
.page{background:#fff;border:1px solid #e6e8eb;border-radius:8px;margin-bottom:16px;
overflow:hidden}
.page>h2{font-size:15px;margin:0;padding:14px 18px;border-bottom:1px solid #eef0f2}
.page>h2 a{color:#8a9099;font-weight:400;font-size:12px;text-decoration:none;margin-left:8px}
.case{border-bottom:1px solid #f0f2f4}
.case:last-child{border-bottom:none}
.case>summary{padding:11px 18px;cursor:pointer;display:flex;align-items:center;
gap:10px;font-size:14px;list-style:none}
.case>summary::-webkit-details-marker{display:none}
.case>summary:hover{background:#fafbfc}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:500}
.b-pass{background:#e3f9f0;color:#00a870}
.b-warn{background:#fdf3e2;color:#e6a23c}
.b-fail{background:#fdeaec;color:#e34d59}
.b-error{background:#fff1e0;color:#e37318}
.b-skip{background:#f3f4f6;color:#8a9099}
.ms{margin-left:auto;color:#bbc0c6;font-size:12px}
.steps{padding:4px 18px 14px;background:#fafbfc}
.step{display:flex;gap:10px;padding:7px 0;font-size:13px;
border-bottom:1px dashed #eef0f2;align-items:flex-start}
.step:last-child{border-bottom:none}
.ico{width:16px;flex:0 0 16px;text-align:center}
.act{font-family:ui-monospace,Menlo,monospace;color:#0052d9;flex:0 0 auto;font-size:12px}
.msg{color:#4b5158;word-break:break-all;flex:1}
.msg.err{color:#e34d59}
.msg.warn{color:#b8860b}
.shot{display:block;margin-top:8px;max-width:520px;border:1px solid #e6e8eb;border-radius:4px}
.api-log{margin:8px 18px 12px;font-size:12px}
.api-log>summary{cursor:pointer;color:#8a9099;list-style:none;padding:4px 0}
.api-log>summary::-webkit-details-marker{display:none}
.api-log>summary:hover{color:#0052d9}
.api-call{border:1px solid #eef0f2;border-radius:4px;margin-top:6px;overflow:hidden}
.api-call>summary{cursor:pointer;list-style:none;padding:6px 10px;
display:flex;gap:8px;align-items:center;background:#fafbfc}
.api-call>summary::-webkit-details-marker{display:none}
.api-call .method{font-family:ui-monospace,Menlo,monospace;font-weight:600;color:#0052d9;flex:0 0 auto}
.api-call .url{color:#4b5158;flex:1;word-break:break-all}
.api-call .status{flex:0 0 auto;font-weight:600;color:#00a870}
.api-call .status.err{color:#e34d59}
.api-call .dur{flex:0 0 auto;color:#bbc0c6}
.api-call-body{padding:8px 10px;background:#fff;border-top:1px solid #eef0f2}
.api-call-body pre{margin:4px 0 10px;padding:8px;background:#f5f6f8;border-radius:4px;
overflow-x:auto;white-space:pre-wrap;word-break:break-all;font-size:11.5px}
.api-call-body .k{color:#8a9099;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
"""


def _badge(s: Status) -> str:
    return f'<span class="badge b-{s.value}">{s.value.upper()}</span>'


def _api_log_html(calls: list) -> str:
    """
    这条用例触发的接口调用列表——默认折叠，只有真要排查"到底打了什么
    接口、传了什么参数、返回了什么"时才展开，不占报告默认的阅读空间。
    请求头在记录时（Context._on_api_response）已经把 Cookie/Authorization
    这类凭证替换成"[已隐藏]"，这里不用再额外处理。
    """
    if not calls:
        return ""
    items = []
    for c in calls:
        status = c.get("status")
        cls = " err" if status is not None and status >= 400 else ""
        dur = c.get("duration_ms")
        dur_txt = f"{dur}ms" if dur is not None else "—"
        headers_txt = json.dumps(c.get("request_headers") or {}, ensure_ascii=False, indent=2)
        body_parts = [f'<div class="k">请求头</div><pre>{html.escape(headers_txt)}</pre>']
        if c.get("request_body"):
            body_parts.append(f'<div class="k">请求参数</div><pre>{html.escape(c["request_body"])}</pre>')
        body_parts.append(f'<div class="k">响应</div><pre>{html.escape(c.get("response_body") or "")}</pre>')
        items.append(
            f'<details class="api-call"><summary>'
            f'<span class="method">{html.escape(c.get("method", ""))}</span>'
            f'<span class="url">{html.escape(c.get("url", ""))}</span>'
            f'<span class="status{cls}">{status if status is not None else "—"}</span>'
            f'<span class="dur">{dur_txt}</span></summary>'
            f'<div class="api-call-body">{"".join(body_parts)}</div></details>'
        )
    return (f'<details class="api-log"><summary>接口调用（{len(calls)} 次）</summary>'
           + "".join(items) + "</details>")


def render(results: List[PageResult], out_path: str) -> str:
    total = sum(len(r.cases) for r in results)
    passed = sum(r.passed for r in results)         # 含 WARN：页面内容本身没问题就算通过
    failed = sum(r.failed for r in results)
    warned = sum(1 for r in results for c in r.cases if c.status == Status.WARN)
    skipped = total - passed - failed
    ms = sum(r.duration_ms for r in results)

    parts = [f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>自动化测试报告</title><style>{CSS}</style></head><body><div class="wrap">
<h1>自动化数据验证报告</h1>
<div class="sub">{tz.now():%Y-%m-%d %H:%M:%S} · 耗时 {ms/1000:.1f}s</div>
<div class="cards">
<div class="card"><div class="n">{total}</div><div class="l">用例总数</div></div>
<div class="card pass"><div class="n">{passed}</div><div class="l">通过</div></div>
<div class="card fail"><div class="n">{failed}</div><div class="l">失败</div></div>
<div class="card skip"><div class="n">{skipped}</div><div class="l">跳过</div></div>
<div class="card warn"><div class="n">{warned}</div><div class="l">其中有警告</div></div>
<div class="card"><div class="n">{(passed/total*100 if total else 0):.0f}%</div>
<div class="l">通过率</div></div>
</div>"""]

    for pr in results:
        parts.append(f'<div class="page"><h2>{html.escape(pr.name)}'
                     f'<a href="{html.escape(pr.url)}" target="_blank">{html.escape(pr.url)}</a></h2>')
        for c in pr.cases:
            openattr = " open" if c.status in (Status.FAIL, Status.ERROR) else ""
            parts.append(f'<details class="case"{openattr}><summary>{_badge(c.status)}'
                         f'<span>{html.escape(c.name)}</span>'
                         f'<span class="ms">{c.duration_ms}ms</span></summary><div class="steps">')
            for s in c.steps:
                ico = {"pass": "✓", "warn": "⚠", "fail": "✗", "error": "!", "skip": "-"}[s.status.value]
                cls = " err" if s.status in (Status.FAIL, Status.ERROR) else \
                      (" warn" if s.status == Status.WARN else "")
                parts.append(f'<div class="step"><span class="ico">{ico}</span>'
                             f'<span class="act">{html.escape(s.action)}</span>'
                             f'<span class="msg{cls}">{html.escape(s.message)}')
                if s.screenshot:
                    parts.append(f'<img class="shot" src="{html.escape(s.screenshot)}">')
                parts.append("</span></div>")
            parts.append("</div>")
            parts.append(_api_log_html(c.api_calls))
            parts.append("</details>")
        parts.append("</div>")

    parts.append("</div></body></html>")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out_path


def render_json(results: List[PageResult], out_path: str) -> str:
    from dataclasses import asdict
    data = [asdict(r) for r in results]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return out_path


def build_run_label(page_count: int, only_tags: Optional[List[str]] = None,
                    language_display: Optional[str] = None) -> str:
    """
    给历史报告列表用的一句话摘要：跑了几个页面、勾了哪些类别、选了哪种
    语言——列表里不用点开报告、只看这一行就知道这份报告测的是什么，
    不用只靠一个时间戳去猜"这次是不是跑了全部类别"。
    """
    parts = [f"{page_count} 个页面"]
    parts.append("+".join(TAG_LABELS.get(t, t) for t in only_tags) if only_tags else "全部类别")
    parts.append(language_display or "默认语言")
    return " · ".join(parts)


def render_meta(out_path: str, *, project: Optional[str] = None, page_count: int = 0,
                only_tags: Optional[List[str]] = None, exclude_tags: Optional[List[str]] = None,
                language: Optional[str] = None, language_display: Optional[str] = None) -> str:
    """
    执行时的上下文（勾了哪些类别、选了哪种语言）单独存一份，不塞进
    result.json——那份文件的结构是"页面执行结果列表"，server.py 的
    list_reports() 已经在按这个结构读，加运行参数进去要么破坏现有结构，
    要么另包一层拖累所有读它的地方。历史报告列表要显示"这份报告测了
    什么"，只需要额外读一下这份小文件；读不到就是没有这份文件的旧报告，
    前端退回只显示时间戳的老样子，不会因为文件缺失而报错。
    """
    meta = {
        "project": project,
        "tags": only_tags or [],
        "exclude_tags": exclude_tags or [],
        "language": language,
        "language_display": language_display,
        "label": build_run_label(page_count, only_tags, language_display),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return out_path
