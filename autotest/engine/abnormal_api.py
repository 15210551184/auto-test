"""从执行结果中提取异常接口，并生成便于排查的 CSV。"""

import csv
import io
import json
from http.client import responses
from typing import Any, Dict, Iterable, List, Optional, Tuple


_SUCCESS_CODES = {"0", "1", "200", "00000", "ok", "success", "true"}
_MESSAGE_KEYS = ("message", "msg", "errorMessage", "error_message",
                 "description", "detail", "error")


def _response_json(call: Dict[str, Any]) -> Optional[Any]:
    body = call.get("response_body")
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def _message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            nested = _message(value)
            if nested:
                return nested
        elif value not in (None, ""):
            return str(value)
    return ""


def abnormal_description(call: Dict[str, Any]) -> str:
    """返回异常原因；空字符串表示该接口没有发现异常。"""
    request_error = call.get("error")
    if request_error:
        return f"请求失败：{request_error}"

    status = call.get("status")
    payload = _response_json(call)
    message = _message(payload)
    reasons: List[str] = []

    if isinstance(status, int) and status >= 400:
        reason = responses.get(status, "")
        reasons.append(f"HTTP {status}" + (f" {reason}" if reason else ""))

    if isinstance(payload, dict):
        if payload.get("success") is False:
            reasons.append("业务返回 success=false")
        code = payload.get("code")
        if code is not None and str(code).strip().lower() not in _SUCCESS_CODES:
            reasons.append(f"业务返回 code={code}")

    if not reasons:
        return ""
    if message:
        reasons.append(message)
    return "；".join(dict.fromkeys(reasons))


def _case_description(case: Dict[str, Any]) -> str:
    messages: List[str] = []
    if case.get("error"):
        messages.append(str(case["error"]))
    for step in case.get("steps") or []:
        if step.get("status") in ("fail", "error") and step.get("message"):
            messages.append(str(step["message"]))
    return "；".join(dict.fromkeys(messages))


def collect_abnormal_apis(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in results:
        for case in page.get("cases") or []:
            case_description = _case_description(case)
            for call in case.get("api_calls") or []:
                description = abnormal_description(call)
                if not description:
                    continue
                rows.append({
                    "页面": page.get("name", ""),
                    "用例": case.get("name", ""),
                    "用例状态": case.get("status", ""),
                    "请求方法": call.get("method", ""),
                    "接口地址": call.get("url", ""),
                    "HTTP状态": call.get("status") if call.get("status") is not None else "",
                    "接口异常描述": description,
                    "用例异常描述": case_description,
                    "耗时(ms)": call.get("duration_ms") if call.get("duration_ms") is not None else "",
                    "请求参数": call.get("request_body") or "",
                    "响应内容": call.get("response_body") or "",
                })
    return rows


_CSV_FIELDS: Tuple[str, ...] = (
    "页面", "用例", "用例状态", "请求方法", "接口地址", "HTTP状态",
    "接口异常描述", "用例异常描述", "耗时(ms)", "请求参数", "响应内容",
)


def render_abnormal_api_csv(results: Iterable[Dict[str, Any]]) -> bytes:
    """生成带 UTF-8 BOM 的 CSV，下载后可直接用 Excel 打开。"""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    writer.writerows(collect_abnormal_apis(results))
    return output.getvalue().encode("utf-8-sig")
