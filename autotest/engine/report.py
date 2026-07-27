"""HTML 报告生成"""
import html
import json
import os
from typing import List

from . import tz
from .models import PageResult, Status

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
"""


def _badge(s: Status) -> str:
    return f'<span class="badge b-{s.value}">{s.value.upper()}</span>'


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
            parts.append("</div></details>")
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
