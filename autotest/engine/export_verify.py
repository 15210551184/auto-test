"""
导出验证。

两种导出模式：
  direct — 点按钮直接触发浏览器下载
  async  — 点按钮只创建任务，需要轮询任务接口拿下载链接（大数据量后台常见）
配置里 export_mode 指定；不确定就用 auto，先试 direct，超时再退到 async。
"""
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from . import normalize as N
from . import lang_variants as LV


def _export_buttons(ctx):
    """导出按钮定位由 Context 统一追加多语言兜底。"""
    return ctx.page.locator(ctx.selector("export_btn"))


def _configured_headers(ctx) -> List[str]:
    """从列表冒烟用例中读取扫描时保存的完整 canonical 表头。"""
    for case in getattr(ctx.config, "cases", []) or []:
        for step in getattr(case, "steps", []) or []:
            if getattr(step, "action", "") != "assert_headers":
                continue
            params = getattr(step, "params", {}) or {}
            headers = params.get("contains") or params.get("equals")
            if isinstance(headers, list) and headers:
                return headers
    return []


def _runtime_header_map(ctx, explicit: Dict[str, str]) -> Dict[str, str]:
    """缺少 header_variants 时，按完整且等长的页面列顺序安全补全映射。"""
    mapping = LV.runtime_reverse_map(
        {}, _configured_headers(ctx), list(ctx.ui.headers(ctx.page)))
    mapping.update(explicit)
    return mapping


def _phase(ctx, text: str) -> None:
    setter = getattr(ctx, "set_phase", None)
    if setter:
        setter(text)


def _read_table(path: str) -> List[Dict[str, Any]]:
    """读 xlsx / xls / csv，统一成 list[dict]"""
    import pandas as pd
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        df = pd.read_excel(path, dtype=object)
    elif ext == ".csv":
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                df = pd.read_csv(path, dtype=object, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV 编码无法识别")
    else:
        raise ValueError(f"不支持的导出格式: {ext}")

    df.columns = [str(c).strip() for c in df.columns]

    # 有些导出第一行是合并的大标题（"订单导出报表"），真表头在第二行。
    # 特征：多数列名是 pandas 补的 Unnamed:N。只判断第一列会漏，因为
    # 第一列会拿到标题文本本身，只有后面的列才是 Unnamed。
    unnamed = sum(1 for c in df.columns if c.startswith("Unnamed"))
    if len(df) > 0 and unnamed >= max(1, len(df.columns) // 2):
        df.columns = [str(c).strip() for c in df.iloc[0]]
        df = df.iloc[1:].reset_index(drop=True)
        df = df.loc[:, [c for c in df.columns if c and c.lower() != "nan"]]

    return df.where(df.notna(), None).to_dict("records")


def _export_response_name(resp) -> Optional[str]:
    """从文件响应头/URL 推导安全文件名；不是文件响应就返回 None。"""
    try:
        headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
        disposition = headers.get("content-disposition", "")
        content_type = headers.get("content-type", "").lower()
        url = resp.url
    except Exception:
        return None
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    if not match:
        match = re.search(r'filename\s*=\s*"?([^";]+)', disposition, re.I)
    name = unquote(match.group(1).strip()) if match else ""
    path_name = unquote(Path(urlparse(url).path).name)
    if not name and re.search(r"\.(xlsx?|csv)(?:$|\?)", url, re.I):
        name = path_name
    looks_file = (
        "attachment" in disposition.lower()
        or any(token in content_type for token in (
            "spreadsheet", "ms-excel", "text/csv", "octet-stream"))
        or bool(re.search(r"\.(xlsx?|csv)$", name or path_name, re.I))
    )
    if not looks_file:
        return None
    name = os.path.basename(name or path_name or f"export_{int(time.time())}.xlsx")
    if not Path(name).suffix:
        name += ".csv" if "csv" in content_type else ".xlsx"
    return name or f"export_{int(time.time())}.xlsx"


def _save_finished_response(ctx, resp) -> Optional[str]:
    name = _export_response_name(resp)
    if not name:
        return None
    try:
        body = resp.body()
        if not body:
            return None
        dest = os.path.join(ctx.download_dir, name)
        with open(dest, "wb") as f:
            f.write(body)
        return dest
    except Exception:
        return None


def _download_direct(ctx, timeout: int) -> Optional[str]:
    """
    同时兼容三种“直接导出”：
    1. 浏览器原生 download 事件；
    2. 点击后先弹确认框；
    3. Axios/fetch 拿 Blob 后由前端保存（可能不触发 download，但接口响应
       本身就是 Excel/CSV，可在 requestfinished 后直接保存）。
    """
    page = ctx.page
    _phase(ctx, f"导出：点击按钮并等待文件（最多 {max(1, timeout // 1000)}s）")
    downloads, responses = [], []

    def on_download(download):
        downloads.append(download)

    def on_finished(request):
        try:
            resp = request.response()
            if resp and _export_response_name(resp):
                responses.append(resp)
        except Exception:
            pass

    page.on("download", on_download)
    page.on("requestfinished", on_finished)
    try:
        buttons = _export_buttons(ctx)
        clicked = False
        for i in range(8):
            try:
                button = buttons.nth(i)
                button.wait_for(state="visible", timeout=250)
                button.click(timeout=2000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return None

        # 有些系统点“导出”只是先弹确认框；此时必须确认后才会真正发请求。
        page.wait_for_timeout(350)
        confirm = page.locator(
            ".el-message-box:visible .el-button--primary, "
            ".el-dialog:visible button:has-text('确定'), "
            ".el-dialog:visible button:has-text('Confirm'), "
            ".el-dialog:visible button:has-text('OK'), "
            ".el-dialog:visible button:has-text('导出'), "
            ".el-dialog:visible button:has-text('Export'), "
            ".el-dialog:visible button:has-text('Exporter'), "
            ".el-dialog:visible button:has-text('تصدير')"
        ).first
        try:
            confirm.wait_for(state="visible", timeout=350)
            confirm.click(timeout=1200)
        except Exception:
            pass

        deadline = time.monotonic() + max(500, timeout) / 1000
        while time.monotonic() < deadline:
            if downloads:
                d = downloads[0]
                dest = os.path.join(ctx.download_dir, d.suggested_filename)
                d.save_as(dest)
                return dest
            while responses:
                path = _save_finished_response(ctx, responses.pop(0))
                if path:
                    return path
            page.wait_for_timeout(
                min(250, max(1, int((deadline - time.monotonic()) * 1000))))
        return None
    finally:
        for event, handler in (
                ("download", on_download), ("requestfinished", on_finished)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass


def _download_async(ctx, timeout: int) -> Optional[str]:
    """
    点导出 -> 后端建任务 -> 轮询任务列表 -> 拿到 url -> 用页面上下文请求下载。
    用 page.request 而不是 requests，是为了复用登录态 cookie。
    """
    api = getattr(ctx.config, "export_task_api", None)
    if not api:
        return None
    _export_buttons(ctx).first.click()
    deadline = time.time() + timeout / 1000
    poll = 0
    while time.time() < deadline:
        poll += 1
        left = max(0, int(deadline - time.time()))
        _phase(ctx, f"导出：轮询异步任务（第 {poll} 次，剩余 {left}s）")
        ctx.page.wait_for_timeout(3000)
        try:
            resp = ctx.page.request.get(api)
            data = resp.json()
        except Exception:
            continue
        url = _find_download_url(data)
        if url:
            r = ctx.page.request.get(url)
            name = url.split("/")[-1].split("?")[0] or "export.xlsx"
            dest = os.path.join(ctx.download_dir, name)
            with open(dest, "wb") as f:
                f.write(r.body())
            return dest
    return None


def _find_download_url(node, depth=0):
    """在任意嵌套的 JSON 里找形如下载链接的字段"""
    if depth > 6:
        return None
    if isinstance(node, str):
        if re.match(r"https?://.*\.(xlsx|xls|csv)", node, re.I):
            return node
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if any(t in k.lower() for t in ("url", "path", "link", "file")):
                found = _find_download_url(v, depth + 1)
                if found:
                    return found
        for v in node.values():
            found = _find_download_url(v, depth + 1)
            if found:
                return found
    if isinstance(node, list):
        for v in node:
            found = _find_download_url(v, depth + 1)
            if found:
                return found
    return None


def verify_export(ctx, compare_with=None, columns=None, row_count_mode="total",
                  timeout=45000, header_match=True, sample=20) -> str:
    from .actions import AssertionFailed

    # 导出证据的第一张图：下载前的真实列表页。Context.shot 会在宽表场景
    # 临时扩大 viewport，确保横向滚动区里的列也进入同一张截图。
    _phase(ctx, "导出：截取列表页完整列")
    list_image = ctx.shot("export_list_page")
    evidence_images = ([{"label": "列表页（完整列）", "path": list_image}]
                       if list_image else [])

    mode = ctx.config.export_mode
    path = None
    export_started = time.monotonic()
    # timeout 是整次导出的总预算，不是每个下载策略各自的预算。以前 auto
    # 曾经会先等 direct 20s，再把完整 90s 给 async，单条用例实际可等 110s+，
    # 页面看起来像卡死。现在无论走几种策略，总等待都不会超过配置值。
    export_deadline = export_started + max(1, timeout) / 1000
    api_log_start = len(getattr(ctx, "api_log", None) or [])
    if mode in ("direct", "auto"):
        direct_timeout = timeout if mode == "direct" else min(timeout, 20000)
        path = _download_direct(ctx, direct_timeout)
    if path is None and mode in ("async", "auto"):
        task_api = getattr(ctx.config, "export_task_api", None)
        if task_api:
            remaining_ms = max(0, int((export_deadline - time.monotonic()) * 1000))
            if remaining_ms:
                path = _download_async(ctx, remaining_ms)

    if path is None:
        waited_ms = int((time.monotonic() - export_started) * 1000)
        seen = []
        api_calls = (getattr(ctx, "api_log", None) or [])[api_log_start:]
        for call in api_calls[-8:]:
            url = call.get("url") if isinstance(call, dict) else str(call)
            if url:
                seen.append(url)
        hint = (
            "已尝试浏览器下载事件、确认弹窗和文件响应。"
            "若页面显示“导出任务已创建”，请配置 export_mode: async "
            "和 export_task_api。"
        )
        if seen:
            hint += f" 导出期间接口: {seen}"
        raise AssertionFailed(
            f"导出失败：实际等待约 {waited_ms}ms 仍没拿到文件。{hint}",
            detail={"images": evidence_images} if evidence_images else None,
        )

    size = os.path.getsize(path)
    if size == 0:
        raise AssertionFailed(f"导出文件为空: {path}")

    _phase(ctx, f"导出：读取文件 {Path(path).name}")
    rows = _read_table(path)
    # 页面和导出文件在英文/法文/阿文状态下使用的是翻译后表头，而 YAML 的
    # columns 以及 capture 保存的页面数据使用 canonical（通常为中文）列名。
    # 两边先统一映射回 canonical，再进行缺列和字段值比较。
    header_variants = getattr(ctx.config, "header_variants", {}) or {}
    canonical_of = _runtime_header_map(ctx, LV.reverse_map(header_variants))
    comparison_rows = [
        LV.canonicalize_row(row, canonical_of)
        for row in rows
    ]
    _phase(ctx, f"导出：生成文件预览（{len(rows)} 行）")
    file_image = ctx.table_preview_shot(
        rows,
        "export_file_content",
        "导出文件内容",
        source_name=Path(path).name,
        max_rows=sample,
    )
    if file_image:
        evidence_images.append({"label": "导出文件内容", "path": file_image})
    notes = [f"文件 {Path(path).name} ({size//1024}KB, {len(rows)} 行)"]
    problems = []
    _phase(ctx, "导出：比对表头、行数和字段值")

    # --- 1. 表头一致性 ---
    if header_match and rows:
        # columns 是生成/人工确认过的“可导出、可比较列”。配置存在时以它为准，
        # 不再强制要求头像、操作按钮等纯页面展示列也出现在 Excel 中。
        ui_headers = (list(columns) if columns else [
            LV.canonical_name(h, canonical_of) for h in ctx.ui.headers(ctx.page)
            if LV.canonical_name(h, canonical_of) not in ("操作", "序号", "")
        ])
        xl_headers = list(comparison_rows[0].keys())
        missing = [h for h in ui_headers if h not in xl_headers]
        if missing:
            problems.append(f"导出缺少页面上的列: {missing}")
        notes.append(f"表头 {len(xl_headers)} 列")

    # --- 2. 行数一致性 ---
    if row_count_mode == "total":
        total = ctx.ui.total_count(ctx.page)
        if total is not None and len(rows) != total:
            problems.append(f"导出 {len(rows)} 行，分页总数 {total} 条")
        elif total is not None:
            notes.append(f"行数与总数 {total} 一致")
    elif row_count_mode == "page":
        n = ctx.ui.row_count(ctx.page)
        if len(rows) != n:
            problems.append(f"导出 {len(rows)} 行，当前页 {n} 行")

    # --- 3. 抽样字段比对 ---
    src = ctx.data.get(compare_with) if compare_with else None
    if src and columns:
        checked = 0
        diffs = []
        skipped = set()
        for i, ui_row in enumerate(src[:sample]):
            if i >= len(rows):
                break
            xl_row = comparison_rows[i]
            for col in columns:
                if col not in ui_row or col not in xl_row:
                    skipped.add(col)
                    continue
                checked += 1
                if not N.compare(ui_row[col], xl_row[col]):
                    diffs.append(f"行{i+1} {col}: 页面='{ui_row[col]}' 导出='{xl_row[col]}'")
        if skipped:
            problems.append(f"以下配置列未实际参与数据比对: {sorted(skipped)}")
        if diffs:
            problems.append(f"字段不一致 {len(diffs)}/{checked} 处: {diffs[:5]}")
        elif checked:
            notes.append(
                f"抽样比对 {checked} 个字段一致"
                f"（{min(len(src), sample, len(rows))} 行 × {len(columns)} 列）"
            )
        else:
            problems.append("没有任何字段实际参与数据比对")

    download = {"label": f"导出文件 {Path(path).name}",
                "path": os.path.relpath(path, ctx.report_root)}
    detail = {"download": download, "images": evidence_images}
    if problems:
        # 把导出文件本身挂到报告上：导出对不对，光看一句"缺少列 X/Y"判断不了
        # 是"导出真漏了"还是"页面表头识别多了"，直接把文件下下来打开看最快。
        # 当时的页面截图不用在这里另外截——run_step() 对任何 AssertionFailed
        # 都会自动截一张（StepResult.screenshot），再截一张只是重复。
        raise AssertionFailed(
            " | ".join(problems) + f"  [{'; '.join(notes)}]",
            detail=detail)
    return "导出验证通过 ✓ " + "; ".join(notes), detail
