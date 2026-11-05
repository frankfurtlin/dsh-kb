"""报告文件的路径解析与 HTML 内链改写。

裁剪自 betterlife 的 `app/services/report_html.py`，改写目标换成插件的 `/kb`
前缀：

- `/mine/reports/...` → `/kb/reports/...`
- `/mine/report/...`  → `/kb/reports/...`（旧站历史链接）
- `/mine/static/...`  → `/kb/static/...`（报告里的图标/样式引用）
- 其余根绝对路径（`href="/20260905/xxx.html"`、`src="/assets/shell.js"`）只有在
  reports/ 下确实存在对应文件时才改写成 `/kb/reports/...`，避免把外链改坏。

这样报告正文在 iframe（`/kb/` 基址）里点任何内链都留在面板内，不会跳到
DSH 的根路径上去。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from app.config import REPORTS_DIR, ROUTE_PREFIX

CANONICAL = f"{ROUTE_PREFIX}/reports"
STATIC_PREFIX = f"{ROUTE_PREFIX}/static"

_ATTR_RE = re.compile(r'\b(href|src)="/(?!/)([^"]*)"')
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?/(?!/)([^)'\"]*)['\"]?\s*\)")


def safe_path(rel: str) -> Path | None:
    """把 URL 里的相对路径解析成报告目录内的真实路径；越界返回 None。"""
    raw = unquote(str(rel or "")).strip().lstrip("/")
    if not raw:
        return None
    target = (REPORTS_DIR / raw).resolve()
    try:
        target.relative_to(REPORTS_DIR.resolve())
    except ValueError:
        return None
    return target


def target_exists(value: str) -> bool:
    """去掉 ?query/#fragment 后检查 reports/ 下是否存在该文件。"""
    raw = str(value or "").split("#", 1)[0].split("?", 1)[0]
    if not raw:
        return False
    target = safe_path(raw)
    return target is not None and target.is_file()


def rewrite_html(text: str) -> str:
    """把报告正文里的根绝对链接改写到 /kb 前缀下（相对路径保持原样）。"""
    if not text:
        return text

    # 1) 旧站前缀整体换掉（含被 JS 拼进字符串里的路径）。
    text = text.replace("/mine/reports/", f"{CANONICAL}/")
    text = text.replace("/mine/report/", f"{CANONICAL}/")
    text = text.replace("/mine/static/", f"{STATIC_PREFIX}/")
    text = text.replace("/mine/", f"{ROUTE_PREFIX}/")

    # 2) 剩余根绝对 href/src：目标确实在 reports/ 里才改写，其余原样保留。
    def replace_attr(match: re.Match[str]) -> str:
        attr, value = match.group(1), match.group(2)
        if target_exists(value):
            return f'{attr}="{CANONICAL}/{value}"'
        return match.group(0)

    text = _ATTR_RE.sub(replace_attr, text)

    # 3) CSS 里的 url(/...) 同样处理（报告内联样式里的背景图）。
    def replace_css_url(match: re.Match[str]) -> str:
        value = match.group(1)
        if target_exists(value):
            return f'url("{CANONICAL}/{value}")'
        return match.group(0)

    return _CSS_URL_RE.sub(replace_css_url, text)


