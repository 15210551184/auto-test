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

from . import normalize as N


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


def _download_direct(ctx, timeout: int) -> Optional[str]:
    btn = ctx.selector("export_btn")
    try:
        with ctx.page.expect_download(timeout=timeout) as dl:
            ctx.page.locator(btn).first.click()
        d = dl.value
        dest = os.path.join(ctx.download_dir, d.suggested_filename)
        d.save_as(dest)
        return dest
    except Exception:
        return None


def _download_async(ctx, timeout: int) -> Optional[str]:
    """
    点导出 -> 后端建任务 -> 轮询任务列表 -> 拿到 url -> 用页面上下文请求下载。
    用 page.request 而不是 requests，是为了复用登录态 cookie。
    """
    api = ctx.config.export_task_api
    if not api:
        return None
    ctx.page.locator(ctx.selector("export_btn")).first.click()
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
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
                  timeout=90000, header_match=True, sample=20) -> str:
    from .actions import AssertionFailed

    mode = ctx.config.export_mode
    path = None
    if mode in ("direct", "auto"):
        path = _download_direct(ctx, timeout if mode == "direct" else 20000)
    if path is None and mode in ("async", "auto"):
        path = _download_async(ctx, timeout)

    if path is None:
        raise AssertionFailed(
            f"导出失败：{timeout}ms 内没拿到文件。"
            f"若是异步导出请在配置里设 export_mode: async 并填 export_task_api"
        )

    size = os.path.getsize(path)
    if size == 0:
        raise AssertionFailed(f"导出文件为空: {path}")

    rows = _read_table(path)
    notes = [f"文件 {Path(path).name} ({size//1024}KB, {len(rows)} 行)"]
    problems = []

    # --- 1. 表头一致性 ---
    if header_match and rows:
        # columns 是生成/人工确认过的“可导出、可比较列”。配置存在时以它为准，
        # 不再强制要求头像、操作按钮等纯页面展示列也出现在 Excel 中。
        ui_headers = (list(columns) if columns else [
            h for h in ctx.ui.headers(ctx.page) if h not in ("操作", "序号", "")
        ])
        xl_headers = list(rows[0].keys())
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
            xl_row = rows[i]
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
    if problems:
        # 把导出文件本身挂到报告上：导出对不对，光看一句"缺少列 X/Y"判断不了
        # 是"导出真漏了"还是"页面表头识别多了"，直接把文件下下来打开看最快。
        # 当时的页面截图不用在这里另外截——run_step() 对任何 AssertionFailed
        # 都会自动截一张（StepResult.screenshot），再截一张只是重复。
        raise AssertionFailed(
            " | ".join(problems) + f"  [{'; '.join(notes)}]",
            detail={"download": download})
    return "导出验证通过 ✓ " + "; ".join(notes), {"download": download}
