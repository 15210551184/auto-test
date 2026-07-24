"""
Web 控制台。

设计要点：
  - 执行放后台线程，接口立即返回。浏览器不会挂在那儿等五分钟。
  - 日志实时流式推送（SSE），能看到"正在跑第几条用例"而不是黑盒等待。
  - 同一时刻只允许一个任务在跑。Chromium 很吃内存，并发跑容易 OOM，
    而且多个任务同时操作同一套测试数据会互相干扰。
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from engine import project as P

ROOT = Path(__file__).parent.resolve()
CONFIG_DIR = ROOT / "configs"
REPORT_DIR = ROOT / "reports"
PY = sys.executable

app = Flask(__name__, static_folder=None)


# ---------- 任务状态 ----------
class Job:
    """当前正在执行的任务。全局只有一个。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.name = ""
        self.cmd = ""
        self.started = None
        self.finished = None
        self.returncode = None
        self.lines = []                # 完整日志，供刷新页面后回看
        self.subscribers = []          # SSE 订阅者队列
        self.report_url = None
        self.proc = None

    def start(self, name: str, cmd: list) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.name = name
            self.cmd = " ".join(cmd)
            self.started = time.time()
            self.finished = None
            self.returncode = None
            self.lines = []
            self.report_url = None
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()
        return True

    def _emit(self, line: str):
        self.lines.append(line)
        # 日志太长会撑爆内存，只留最近 2000 行
        if len(self.lines) > 2000:
            self.lines = self.lines[-2000:]
        for q in list(self.subscribers):
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def _run(self, cmd: list):
        try:
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", env=env,
            )
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self._emit(line)
                # 从输出里抓报告路径，跑完直接给链接
                m = re.search(r"reports[/\\]([\w\-]+)[/\\]report\.html", line)
                if m:
                    self.report_url = f"/reports/{m.group(1)}/report.html"
            self.proc.wait()
            self.returncode = self.proc.returncode
        except Exception as e:
            self._emit(f"[执行异常] {e}")
            self.returncode = -1
        finally:
            self.finished = time.time()
            self.running = False
            self._emit("__DONE__")

    def stop(self):
        if self.proc and self.running:
            self.proc.terminate()
            self._emit("[已手动终止]")
            return True
        return False

    def snapshot(self):
        return {
            "running": self.running,
            "name": self.name,
            "cmd": self.cmd,
            "started": self.started,
            "elapsed": round((self.finished or time.time()) - self.started, 1)
                       if self.started else 0,
            "returncode": self.returncode,
            "report_url": self.report_url,
            "lines": self.lines[-500:],
        }


JOB = Job()


# ---------- 工具 ----------
def list_configs():
    if not CONFIG_DIR.exists():
        return []
    out = []
    for p in sorted(CONFIG_DIR.glob("*.yaml")):
        info = {"file": p.name, "name": p.stem, "url": ""}
        try:
            import yaml
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            info["name"] = d.get("name", p.stem)
            info["url"] = d.get("url", "")
            info["cases"] = len(d.get("cases", []))
        except Exception:
            info["cases"] = 0
        out.append(info)
    return out


def list_reports(limit=50):
    if not REPORT_DIR.exists():
        return []
    out = []
    for d in sorted(REPORT_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "report.html").exists():
            continue
        item = {"dir": d.name, "url": f"/reports/{d.name}/report.html",
                "time": datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "passed": None, "failed": None, "total": None}
        # 从 result.json 读汇总，读不到就只显示时间
        rj = d / "result.json"
        if rj.exists():
            try:
                data = json.loads(rj.read_text(encoding="utf-8"))
                cases = data[0].get("cases", []) if data else []
                item["total"] = len(cases)
                item["passed"] = sum(1 for c in cases if c.get("status") == "pass")
                item["failed"] = sum(1 for c in cases
                                     if c.get("status") in ("fail", "error"))
            except Exception:
                pass
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ---------- 接口 ----------
@app.get("/api/configs")
def api_configs():
    return jsonify(list_configs())


@app.get("/api/reports")
def api_reports():
    return jsonify(list_reports())


@app.get("/api/status")
def api_status():
    return jsonify(JOB.snapshot())


@app.post("/api/run")
def api_run():
    body = request.get_json(force=True) or {}
    cfg = body.get("config", "")
    action = body.get("action", "run")

    # 只允许 configs 目录下的 yaml，防止路径穿越
    if action in ("run", "test-login"):
        if not cfg or "/" in cfg or "\\" in cfg or not cfg.endswith(".yaml"):
            return jsonify({"ok": False, "msg": "配置文件名不合法"}), 400
        if not (CONFIG_DIR / cfg).exists():
            return jsonify({"ok": False, "msg": "配置文件不存在"}), 404

    if action == "run":
        cmd = [PY, "cli.py", "run", f"configs/{cfg}"]
        tags = body.get("tags")
        if tags:
            cmd += ["--tags"] + tags
        name = f"执行 {cfg}"
    elif action == "test-login":
        cmd = [PY, "cli.py", "test-login", f"configs/{cfg}"]
        name = f"登录测试 {cfg}"
    elif action in ("discover", "batch-scan", "batch-run"):
        d = body.get("project", "")
        if not d or "/" in d or "\\" in d:
            return jsonify({"ok": False, "msg": "项目参数不合法"}), 400
        if not P.load_project(d):
            return jsonify({"ok": False, "msg": "项目不存在"}), 404
        verb = {"discover": "menu", "batch-scan": "batch-scan",
                "batch-run": "batch-run"}[action]
        cmd = [PY, "cli.py", verb, d]
        if action == "batch-scan" and body.get("overwrite"):
            cmd.append("--overwrite")
        if action == "batch-run" and body.get("tags"):
            cmd += ["--tags"] + body["tags"]
        name = {"discover": f"扫描菜单 {d}", "batch-scan": f"生成用例 {d}",
                "batch-run": f"批量执行 {d}"}[action]

    elif action == "scan":
        url = (body.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            return jsonify({"ok": False, "msg": "URL 必须以 http:// 或 https:// 开头"}), 400
        out = body.get("out") or "scanned.yaml"
        out = re.sub(r"[^\w\-.]", "", out)
        if not out.endswith(".yaml"):
            out += ".yaml"
        cmd = [PY, "cli.py", "scan", url, "-o", f"configs/{out}"]
        name = f"扫描 {url}"
    else:
        return jsonify({"ok": False, "msg": "未知操作"}), 400

    if not JOB.start(name, cmd):
        return jsonify({"ok": False, "msg": "已有任务在执行中，请等待完成"}), 409
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    return jsonify({"ok": JOB.stop()})


@app.get("/api/stream")
def api_stream():
    """SSE 实时日志"""
    def gen():
        q = queue.Queue(maxsize=1000)
        # 先补发已有日志，避免刷新页面后看不到前面的内容
        for ln in JOB.lines[-300:]:
            yield f"data: {json.dumps(ln)}\n\n"
        JOB.subscribers.append(q)
        try:
            while True:
                try:
                    line = q.get(timeout=20)
                    yield f"data: {json.dumps(line)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"    # 防止代理断开连接
        except GeneratorExit:
            pass
        finally:
            if q in JOB.subscribers:
                JOB.subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.get("/api/config/<name>")
def api_get_config(name):
    if "/" in name or "\\" in name or not name.endswith(".yaml"):
        return jsonify({"ok": False}), 400
    p = CONFIG_DIR / name
    if not p.exists():
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "content": p.read_text(encoding="utf-8")})


@app.post("/api/config/<name>")
def api_save_config(name):
    if "/" in name or "\\" in name or not name.endswith(".yaml"):
        return jsonify({"ok": False, "msg": "文件名不合法"}), 400
    content = (request.get_json(force=True) or {}).get("content", "")
    try:
        import yaml
        yaml.safe_load(content)      # 存之前先校验语法，避免存进去跑不了
    except Exception as e:
        return jsonify({"ok": False, "msg": f"YAML 语法错误: {e}"}), 400
    (CONFIG_DIR / name).write_text(content, encoding="utf-8")
    return jsonify({"ok": True})


# ---------- 项目 ----------
@app.get("/api/projects")
def api_projects():
    return jsonify(P.list_projects())


@app.post("/api/projects")
def api_create_project():
    b = request.get_json(force=True) or {}
    name = (b.get("name") or "").strip()
    home = (b.get("home_url") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "请填写系统名称"}), 400
    if not re.match(r"^https?://", home):
        return jsonify({"ok": False, "msg": "首页地址必须以 http:// 或 https:// 开头"}), 400
    login = {
        "url": (b.get("login_url") or home).strip(),
        "username": b.get("username") or "${env:AUTOTEST_USER}",
        "password": b.get("password") or "${env:AUTOTEST_PASS}",
    }
    if b.get("expect_selector"):
        login["expect_selector"] = b["expect_selector"]
    try:
        d = P.create_project(name, home, login)
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 409
    return jsonify({"ok": True, "dir": d})


@app.get("/api/projects/<d>")
def api_project(d):
    data = P.load_project(d)
    if not data:
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    return jsonify(data)


@app.delete("/api/projects/<d>")
def api_delete_project(d):
    return jsonify({"ok": P.delete_project(d)})


@app.post("/api/projects/<d>/selection")
def api_selection(d):
    names = (request.get_json(force=True) or {}).get("names", [])
    if not isinstance(names, list):
        return jsonify({"ok": False, "msg": "参数格式错误"}), 400
    n = P.set_selection(d, names)
    return jsonify({"ok": True, "selected": n})


@app.get("/reports/<path:sub>")
def serve_report(sub):
    return send_from_directory(REPORT_DIR, sub)


@app.get("/")
def index():
    return send_from_directory(ROOT / "web", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"控制台已启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
