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
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                    request, send_from_directory, session)

from engine import project as P
from engine.login import load_dotenv

ROOT = Path(__file__).parent.resolve()
CONFIG_DIR = ROOT / "configs"
REPORT_DIR = ROOT / "reports"
RUNTIME_DIR = ROOT / "runtime"
PY = sys.executable

load_dotenv(str(RUNTIME_DIR / ".env"))
WEB_USER = os.environ.get("WEB_USER", "").strip()
WEB_PASS = os.environ.get("WEB_PASS", "")
# 是否已配置管理员账号。没配 = 未就绪，一律拦截（fail-closed），绝不放行任何人。
AUTH_READY = bool(WEB_USER and WEB_PASS)
LOGIN_PAGE = (ROOT / "web" / "login.html").read_text(encoding="utf-8")

SETUP_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>控制台未配置</title><style>body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;
background:#eef1f5;color:#12161c;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{max-width:460px;background:#fff;border:1px solid #dde2e9;border-radius:8px;padding:28px}
h1{font-size:16px;margin:0 0 12px}code{background:#f5f6f8;padding:2px 6px;border-radius:4px;
font-family:ui-monospace,Menlo,monospace;font-size:13px}p{color:#5a6472;margin:8px 0}
.tip{color:#d64550}</style></head><body><div class="box">
<h1>控制台尚未配置管理员账号</h1>
<p class="tip">出于安全考虑，未设置账号密码时控制台不对任何人开放。</p>
<p>请在服务器的 <code>runtime/.env</code> 里配置后重启服务：</p>
<p><code>WEB_USER=你的账号</code><br><code>WEB_PASS=足够复杂的密码</code></p>
<p>建议同时配置 <code>SECRET_KEY</code>，避免重启后登录态失效。</p>
</div></body></html>"""

app = Flask(__name__, static_folder=None)


def _persistent_secret_key() -> str:
    """
    会话签名密钥。优先用环境变量；否则落一份到 runtime/.secret_key 复用。
    之前是「每次启动随机生成」，重启或多进程部署会导致会话签名不一致、
    登录后 cookie 立刻失效——表现就是「点登录没反应、一直弹回登录页」。
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    keyfile = RUNTIME_DIR / ".secret_key"
    try:
        if keyfile.is_file():
            saved = keyfile.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        keyfile.write_text(key, encoding="utf-8")
        try:
            os.chmod(keyfile, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        return secrets.token_hex(32)


app.secret_key = _persistent_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

# ---------- 登录失败限流（防暴力破解） ----------
_LOGIN_FAILS: dict = {}          # ip -> [失败时间戳]
_FAIL_WINDOW = 300               # 统计窗口：5 分钟
_FAIL_MAX = 5                    # 窗口内最多失败次数，超过即锁定
_login_lock = threading.Lock()


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"


def _recent_fails(ip: str) -> list:
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    _LOGIN_FAILS[ip] = fails
    return fails


def _lock_remaining(ip: str) -> int:
    """返回锁定剩余秒数；0 表示未锁定。"""
    fails = _recent_fails(ip)
    if len(fails) >= _FAIL_MAX:
        return max(1, int(_FAIL_WINDOW - (time.time() - fails[0])))
    return 0


def _safe_next(path):
    """只允许跳回本站内的相对路径，防止 next 参数被用来做开放重定向"""
    if path and path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


@app.before_request
def _require_login():
    """
    控制台自身的登录口令（跟被测系统的登录是两回事）。
    未配置管理员账号时一律拦截；已配置则要求登录后才能访问。
    """
    if not AUTH_READY:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "控制台未配置管理员账号，无法使用"}), 503
        return render_template_string(SETUP_PAGE), 503
    if request.path in ("/login", "/logout"):
        return None
    if session.get("authed"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": "未登录或登录已过期"}), 401
    return redirect(f"/login?next={quote(request.path, safe='')}")


@app.get("/login")
def login_page():
    nxt = _safe_next(request.args.get("next"))
    if session.get("authed"):
        return redirect(nxt)
    return render_template_string(LOGIN_PAGE, error=None, next=nxt)


@app.post("/login")
def login_submit():
    nxt = _safe_next(request.form.get("next"))
    ip = _client_ip()
    with _login_lock:
        rem = _lock_remaining(ip)
        if rem > 0:
            return render_template_string(
                LOGIN_PAGE, error=f"尝试过于频繁，请 {rem} 秒后再试", next=nxt), 429

        u, p = request.form.get("username", ""), request.form.get("password", "")
        ok = (secrets.compare_digest(u, WEB_USER)
              and secrets.compare_digest(p, WEB_PASS))
        if ok:
            _LOGIN_FAILS.pop(ip, None)
            session.permanent = True
            session["authed"] = True
            return redirect(nxt)

        _LOGIN_FAILS.setdefault(ip, []).append(time.time())
        left = _FAIL_MAX - len(_recent_fails(ip))
        hint = (f"账号或密码错误（还可尝试 {left} 次）" if left > 0
                else "账号或密码错误，已触发临时锁定")
    return render_template_string(LOGIN_PAGE, error=hint, next=nxt), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


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
        self.progress = None           # 最近一条结构化进度（只留最新）
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
            self.progress = None
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()
        return True

    def _emit(self, line: str):
        # 进度行只转发+留最新，不塞进日志缓冲（避免频繁进度把真实日志挤出 2000 行窗口）
        if not line.startswith("__PROGRESS__"):
            self.lines.append(line)
            if len(self.lines) > 2000:   # 日志太长会撑爆内存，只留最近 2000 行
                self.lines = self.lines[-2000:]
        else:
            self.progress = line[len("__PROGRESS__"):].strip()
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
            "progress": self.progress,
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


@app.delete("/api/projects/<d>/pages/<page>")
def api_delete_page(d, page):
    if not P.load_project(d):
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    if not P.remove_page(d, page):
        return jsonify({"ok": False, "msg": "页面不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/projects/<d>/pages/delete")
def api_delete_pages(d):
    names = (request.get_json(force=True) or {}).get("names", [])
    if not isinstance(names, list):
        return jsonify({"ok": False, "msg": "参数格式错误"}), 400
    deleted = P.remove_pages(d, names)
    return jsonify({"ok": bool(deleted), "deleted": deleted,
                    "msg": "没有可删除的页面" if not deleted else ""})


@app.get("/api/projects/<d>/pages/<page>/config")
def api_get_page_config(d, page):
    path = P.page_config_path(d, page)
    if not path.is_file():
        return jsonify({"ok": False, "msg": "该页面尚未生成用例"}), 404
    return jsonify({"ok": True, "content": path.read_text(encoding="utf-8")})


@app.post("/api/projects/<d>/pages/<page>/config")
def api_save_page_config(d, page):
    if not P.load_project(d):
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    content = (request.get_json(force=True) or {}).get("content", "")
    try:
        import yaml
        yaml.safe_load(content)
    except Exception as exc:
        return jsonify({"ok": False, "msg": f"YAML 语法错误: {exc}"}), 400
    path = P.page_config_path(d, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/reports/<path:sub>")
def serve_report(sub):
    return send_from_directory(REPORT_DIR, sub)


@app.get("/")
def index():
    return send_from_directory(ROOT / "web", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if not AUTH_READY:
        print("[已锁定] 未在 runtime/.env 配置 WEB_USER/WEB_PASS，控制台对所有人关闭，"
              "只显示配置提示页。配置账号密码后重启即可登录。")
    else:
        print(f"[已启用鉴权] 管理员账号: {WEB_USER}")
        if not os.environ.get("SECRET_KEY"):
            print("[提示] 未配置 SECRET_KEY，已用 runtime/.secret_key 持久化会话密钥。")
    print(f"控制台已启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
