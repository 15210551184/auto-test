"""
项目（系统）管理。

一个项目 = 一套登录信息 + 一批页面。
之前每个 yaml 各写一份 login，页面多了改密码要改十几个文件。
现在登录信息放项目级，页面配置只管自己的用例。

目录结构：
  projects/
    出行管理系统/
      project.yaml          登录信息 + 菜单地图
      pages/
        订单列表.yaml        每页一个配置
        司机列表.yaml
"""
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import yaml

PROJECTS_DIR = Path(__file__).parent.parent / "projects"


def _safe(name: str) -> str:
    """项目名转成安全的目录名，防路径穿越"""
    s = re.sub(r"[^\w\u4e00-\u9fa5\-]", "_", str(name)).strip("_")
    return s[:60] or "未命名"


def project_dir(name: str) -> Path:
    return PROJECTS_DIR / _safe(name)


def list_projects() -> List[Dict]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        f = d / "project.yaml"
        if not d.is_dir() or not f.exists():
            continue
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        pages = data.get("pages", [])
        out.append({
            "name": data.get("name", d.name),
            "dir": d.name,
            "home_url": data.get("home_url", ""),
            "page_count": len(pages),
            "selected": sum(1 for p in pages if p.get("selected")),
            "configured": sum(1 for p in pages if (d / "pages" / f"{_safe(p['name'])}.yaml").exists()),
        })
    return out


def load_project(dir_name: str) -> Optional[Dict]:
    f = project_yaml_path(dir_name)
    if not f.exists():
        return None
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    d = PROJECTS_DIR / _safe(dir_name)
    # 标记哪些页面已经生成过配置
    for p in data.get("pages", []):
        p["has_config"] = (d / "pages" / f"{_safe(p['name'])}.yaml").exists()
    data["dir"] = _safe(dir_name)
    return data


def save_project(data: Dict) -> str:
    d = project_dir(data["name"])
    (d / "pages").mkdir(parents=True, exist_ok=True)
    out = {k: v for k, v in data.items() if k != "dir"}
    (d / "project.yaml").write_text(
        yaml.dump(out, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8")
    return d.name


def create_project(name: str, home_url: str, login: Dict) -> str:
    if project_dir(name).exists():
        raise ValueError(f"项目「{name}」已存在")
    return save_project({
        "name": name,
        "home_url": home_url,
        "login": login,
        "pages": [],
    })


def delete_project(dir_name: str) -> bool:
    d = PROJECTS_DIR / _safe(dir_name)
    if d.exists() and d.is_dir():
        shutil.rmtree(d)
        return True
    return False


def set_selection(dir_name: str, selected_names: List[str]) -> int:
    """勾选要测哪些页面"""
    data = load_project(dir_name)
    if not data:
        return 0
    s = set(selected_names)
    for p in data.get("pages", []):
        p["selected"] = p["name"] in s
        p.pop("has_config", None)
    save_project(data)
    return sum(1 for p in data["pages"] if p["selected"])


def update_pages(dir_name: str, pages: List[Dict]) -> int:
    """
    爬取结果写回项目。保留已有页面的勾选状态，
    避免重新扫描后用户之前的选择被清空。
    """
    data = load_project(dir_name)
    if not data:
        return 0
    old = {p["name"]: p.get("selected", False) for p in data.get("pages", [])}
    for p in pages:
        p["selected"] = old.get(p["name"], p.get("recommended", False))
        p.pop("has_config", None)
    data["pages"] = pages
    save_project(data)
    return len(pages)


def page_config_path(dir_name: str, page_name: str) -> Path:
    return PROJECTS_DIR / _safe(dir_name) / "pages" / f"{_safe(page_name)}.yaml"


def scan_cache_path(dir_name: str, page_name: str) -> Path:
    """页面结构扫描缓存。与 YAML 分开，重新生成时不会再启动浏览器。"""
    return PROJECTS_DIR / _safe(dir_name) / "scan-cache" / f"{_safe(page_name)}.json"


def project_yaml_path(dir_name: str) -> Path:
    return PROJECTS_DIR / _safe(dir_name) / "project.yaml"


def remove_page(dir_name: str, page_name: str) -> bool:
    """从菜单地图移除一个页面，并删除它已生成的用例配置。"""
    data = load_project(dir_name)
    if not data:
        return False
    kept = [p for p in data.get("pages", []) if p.get("name") != page_name]
    if len(kept) == len(data.get("pages", [])):
        return False
    for item in kept:
        item.pop("has_config", None)
    data["pages"] = kept
    save_project(data)
    config = page_config_path(dir_name, page_name)
    if config.is_file():
        config.unlink()
    cache = scan_cache_path(dir_name, page_name)
    if cache.is_file():
        cache.unlink()
    return True


def remove_pages(dir_name: str, page_names: List[str]) -> int:
    """批量移除菜单地图页面及其生成的用例，返回实际删除数量。"""
    data = load_project(dir_name)
    if not data:
        return 0
    requested = {str(name) for name in page_names}
    removed = [p for p in data.get("pages", []) if p.get("name") in requested]
    if not removed:
        return 0
    data["pages"] = [p for p in data["pages"] if p.get("name") not in requested]
    for item in data["pages"]:
        item.pop("has_config", None)
    save_project(data)
    for item in removed:
        config = page_config_path(dir_name, item["name"])
        if config.is_file():
            config.unlink()
        cache = scan_cache_path(dir_name, item["name"])
        if cache.is_file():
            cache.unlink()
    return len(removed)


def selected_pages(dir_name: str) -> List[Dict]:
    data = load_project(dir_name)
    if not data:
        return []
    return [p for p in data.get("pages", []) if p.get("selected")]


def set_scan_languages(dir_name: str, codes: List[str]) -> bool:
    """
    扫描时要为哪些语言重新合并 label_variants/header_variants
    （languages.scan_languages，是 languages.options 的子集）——控制台
    里勾选框直接调，不用手改 project.yaml。传空列表等于清空
    scan_languages（只扫默认语言）。传进来的语言码里不在 options
    里的会被丢弃，防止手滑传错码。
    """
    data = load_project(dir_name)
    if not data or not data.get("languages", {}).get("options"):
        return False
    for p in data.get("pages", []):
        p.pop("has_config", None)
    langs = dict(data["languages"])
    valid = set(langs["options"])
    picked = [c for c in codes if c in valid]
    if picked:
        langs["scan_languages"] = picked
    else:
        langs.pop("scan_languages", None)
    data["languages"] = langs
    save_project(data)
    return True


def inject_project_settings(cfg: Dict, project: Dict) -> Dict:
    """
    把项目级设置（登录信息、多语言切换配置）注入到页面配置里。
    页面配置自己写了同名字段就不覆盖，允许个别页面用不同账号/语言配置。
    """
    if "login" not in cfg and project.get("login"):
        cfg["login"] = dict(project["login"])
    if "languages" not in cfg and project.get("languages"):
        cfg["languages"] = dict(project["languages"])
    return cfg
