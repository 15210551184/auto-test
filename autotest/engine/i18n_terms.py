"""
按钮/菜单关键词的中英文对照表。

工具原来到处按"搜索""删除""新增"这类中文文案找按钮——目标系统一切到
英文界面，这些匹配全部失效。最要命的是 check_buttons 巡检时用一份纯中文
的"危险按钮"名单来判断"这个按钮别真的点下去"：切到英文后 "Delete" 认不出来，
巡检会真的把它点下去，这是安全问题，不只是功能缺失。

统一到这里维护，各处按类别引用，不再各写各的、只顾中文一种语言。
以后系统里出现别的语言（比如日语、韩语），照这个格式加一组词就行，
不用改调用方代码。
"""
from typing import List

TERMS = {
    "create": ["新增", "添加", "创建", "Add", "Create", "New", "Ajouter", "Créer", "إضافة", "إنشاء"],
    "edit": ["编辑", "修改", "Edit", "Modify", "Update", "Modifier", "تعديل"],
    "delete": ["删除", "移除", "Delete", "Remove", "Supprimer", "حذف"],
    "search": ["搜索", "查询", "Search", "Query", "Rechercher", "بحث"],
    "reset": ["重置", "清空", "Reset", "Clear", "Réinitialiser", "Effacer", "إعادة تعيين", "مسح"],
    "export": ["导出", "下载", "Export", "Download", "Exporter", "Télécharger", "تصدير", "تنزيل"],
    "batch": ["批量", "Batch", "Lot", "دفعة"],
    "detail": ["查看", "详情", "View", "Detail", "Details", "Voir", "Détails", "عرض", "تفاصيل"],
    "disable": ["设为失效", "失效", "停用", "禁用", "冻结", "Disable", "Deactivate", "Désactiver", "تعطيل"],
    "enable": ["设为生效", "生效", "启用", "解冻", "Enable", "Activate", "Activer", "تمكين"],
    "refresh": ["刷新", "Refresh", "Actualiser", "تحديث"],
    "logout": ["退出", "登出", "注销", "Logout", "Sign out", "Déconnexion", "تسجيل الخروج"],
}


def words(*categories: str) -> List[str]:
    """按类别名取词表，多个类别的词合并去重。"""
    out, seen = [], set()
    for c in categories:
        for w in TERMS.get(c, []):
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out
