"""
结构化执行进度。

执行时除了刷文字日志，再单独打一行 `__PROGRESS__ {json}` 到 stdout。
server.py 把子进程 stdout 逐行经 SSE 推给前端，前端识别这个前缀就更新进度条/计数，
不把它当普通日志显示。和已有的 `__DONE__` 哨兵是同一套机制。

字段（都可选，前端按有啥显示啥）：
  phase      scan | run           当前阶段
  page/pages 第几个 / 共几个页面
  page_name  当前页面名
  case/cases 当前页内第几条 / 共几条用例
  passed/failed 累计通过 / 失败
  tasks      页面任务列表：[{name, status}]，status 为 waiting/running/passed/failed
"""
import json
import sys

SENTINEL = "__PROGRESS__"


def emit(**fields) -> None:
    try:
        print(f"{SENTINEL} {json.dumps(fields, ensure_ascii=False)}", flush=True)
    except Exception:
        pass  # 进度上报永远不能影响主流程
