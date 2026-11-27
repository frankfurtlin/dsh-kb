"""公开报告共享（对外只读访问）。

用户可以把某篇报告标记成「公开」，拿到一个固定链接发给没有 DSH 账号的人：
`<KB_PUBLIC_BASE_URL>/reports/<相对路径>`。公开页和它的附件在 `/kb-public/...`
前缀下匿名提供，只允许 GET/HEAD，其它报告仍然 404。

放行范围（fail-closed：扫描出的新报告默认私有）：
- 被标记公开的 `.html/.htm` 报告本身；
- `reports/assets/` 下的共享样式/脚本/字体（报告站共用，无用户数据）；
- 公开报告 HTML/CSS 里显式引用的本地附件（顺着 `.css` 再追一层）；
- 公开报告同目录下的非 HTML 直接兄弟文件（`data.js`、配图等）；
- 报告旁边的图片目录：`<报告目录>/images/<报告名>/` 与 `<报告目录>/<报告名>/`
  —— 报告站常用这种布局，且路径常由页面内 JS 拼出来，静态扫描看不到。

公开状态存在索引库的 `reports.public` 列里（与已读/收藏同一处），路径一律先
`normalize_rel()` 规范化再比对，`../` 之类的越界路径直接拒绝。
"""

from __future__ import annotations

import posixpath
import re
import threading
from urllib.parse import quote, unquote

from app.config import REPORTS_DIR
from app.services import kb_blob, kb_store, report_html

PUBLIC_PREFIX = "/kb-public"
PUBLIC_REPORTS_PREFIX = f"{PUBLIC_PREFIX}/reports"
SHARED_ASSET_PREFIX = "assets/"

_REF_ATTR_RE = re.compile(r'\b(?:src|href|poster)="([^"]+)"')
_REF_SRCSET_RE = re.compile(r'\bsrcset="([^"]+)"')
_REF_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)")
_REF_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?['\"]?([^'\")\s]+)")
_REF_SKIP_PREFIXES = ("#", "data:", "blob:", "http://", "https://", "//",
                      "mailto:", "javascript:", "tel:")
_REF_MAX_FILES = 200

_ASSET_CACHE: dict[tuple, frozenset[str]] = {}
_CACHE_LOCK = threading.Lock()


def normalize_rel(value: str) -> str | None:
    """把 URL/接口给的相对路径规范成索引键；越界或空返回 None。"""
    rel = unquote(str(value or "")).replace("\\", "/").strip().lstrip("/")
    if not rel:
        return None
    rel = posixpath.normpath(rel)
    if rel in (".", "..", "") or rel.startswith("../") or rel.startswith("/"):
        return None
    return rel


def is_report_rel(rel: str) -> bool:
    """只有报告页（HTML）可以单独设置公开。"""
    return rel.lower().endswith((".html", ".htm"))


def flags() -> set[str]:
    """当前公开集合（规范化相对路径）。"""
    return set(kb_store.public_paths())


def is_public(rel: str) -> bool:
    norm = normalize_rel(rel)
    return bool(norm and norm in flags())


def report_exists(rel: str) -> bool:
    """必须是 reports/ 下真实存在的文件（safe_path 同时挡穿越）。"""
    path = report_html.safe_path(rel)
    return path is not None and path.is_file()


def set_public(rel: str, public: bool) -> dict:
    """切换一篇报告的公开状态；调用方需先做路径/存在性校验。"""
    norm = normalize_rel(rel)
    if norm is None:
        raise ValueError("报告路径不合法")
    result = kb_store.set_public(norm, bool(public))
    if result is None:
        raise ValueError("报告尚未建立索引")
    return result


def public_url(rel: str) -> str:
    """报告在公开站点上的绝对链接（供前端展示/复制）。"""
    from app import config

    norm = normalize_rel(rel) or ""
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/reports/{quote(norm)}"


def resolve_ref(ref: str, base_dir: str) -> str | None:
    """把 HTML/CSS 里的一条引用解析成 reports/ 下的相对路径；外链/锚点返回 None。"""
    raw = unquote(str(ref or "")).replace("\\", "/").strip()
    if not raw or raw.startswith(_REF_SKIP_PREFIXES):
        return None
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip()
    if not raw:
        return None
    if raw.startswith(PUBLIC_REPORTS_PREFIX):
        raw = raw[len(PUBLIC_REPORTS_PREFIX):]
    elif raw.startswith("/kb/reports/"):
        raw = raw[len("/kb/reports/"):]
    elif raw.startswith("/mine/reports/"):
        raw = raw[len("/mine/reports/"):]
    elif not raw.startswith("/"):
        raw = posixpath.join(base_dir, raw)
    return normalize_rel(raw)


def _file_exists(rel: str) -> bool:
    path = report_html.safe_path(rel)
    if path is not None and path.is_file():
        return True
    # v1.0：附件已迁进内容寻址存储，按路径索引查表。
    return kb_store.asset_exists(rel)


def _scan_file(rel: str, depth: int, out: set[str]) -> None:
    """把 rel（HTML/CSS）引用到的本地附件收进 out；CSS 再往下追一层。"""
    path = report_html.safe_path(rel)
    if path is None or not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    _scan_text(rel, text, depth, out)


def _scan_text(rel: str, text: str, depth: int, out: set[str]) -> None:
    """从正文文本里收集附件（文件型与库内文档共用）。"""
    base_dir = posixpath.dirname(rel)
    refs: list[str] = []
    refs.extend(_REF_ATTR_RE.findall(text))
    for srcset in _REF_SRCSET_RE.findall(text):
        for part in srcset.split(","):
            refs.append(part.strip().split(" ")[0])
    refs.extend(_REF_CSS_URL_RE.findall(text))
    if rel.lower().endswith(".css"):
        refs.extend(_REF_CSS_IMPORT_RE.findall(text))
    for ref in refs:
        target = resolve_ref(ref, base_dir)
        if not target or target == rel or target in out:
            continue
        if not _file_exists(target):
            continue
        out.add(target)
        if depth < 1 and target.lower().endswith(".css"):
            _scan_file(target, depth + 1, out)
        if len(out) >= _REF_MAX_FILES:
            return


def _report_assets(rel: str) -> frozenset[str]:
    """一篇公开报告允许匿名访问的附件集合（文件型按 mtime 缓存，库内文档按 blob sha 缓存）。"""
    path = report_html.safe_path(rel)
    if path is None:
        return frozenset()

    text: str | None = None
    key: tuple = ()
    if path.is_file():
        try:
            stat = path.stat()
        except OSError:
            return frozenset()
        key = (rel, stat.st_mtime_ns, stat.st_size)
        cache_key = key
    else:
        # v1.0：报告已迁进索引库，正文在私有 blob 里。
        doc = kb_store.get_report(rel)
        sha = (doc or {}).get("blob_sha") or ""
        if not sha:
            return frozenset()
        cache_key = (rel, sha, 0)
        try:
            text = kb_blob.get(sha).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 取不到就当没有附件
            return frozenset()

    with _CACHE_LOCK:
        cached = _ASSET_CACHE.get(cache_key)
    if cached is not None:
        return cached

    assets: set[str] = set()
    if text is not None:
        _scan_text(rel, text, 0, assets)
    else:
        _scan_file(rel, 0, assets)

    stem = posixpath.splitext(posixpath.basename(rel))[0]
    report_dir = posixpath.dirname(rel)
    for candidate_dir in (posixpath.join(report_dir, "images", stem), posixpath.join(report_dir, stem)):
        for row in kb_store.list_assets(candidate_dir):
            assets.add(row["path"])
            if len(assets) >= _REF_MAX_FILES:
                break
        target = report_html.safe_path(candidate_dir)
        if target is not None and target.is_dir():
            for child in target.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_rel = str(child.relative_to(REPORTS_DIR.resolve())).replace("\\", "/")
                except ValueError:
                    continue
                assets.add(child_rel)
                if len(assets) >= _REF_MAX_FILES:
                    break

    # 同目录的非 HTML 直接兄弟文件（data.js、配图等）。
    for row in kb_store.list_assets(report_dir):
        rel_path = row["path"]
        if posixpath.dirname(rel_path) != report_dir:
            continue
        if rel_path.lower().endswith((".html", ".htm")):
            continue
        assets.add(rel_path)
        if len(assets) >= _REF_MAX_FILES:
            break
    siblings = report_html.safe_path(report_dir)
    if siblings is not None and siblings.is_dir():
        for child in siblings.iterdir():
            if not child.is_file():
                continue
            if child.suffix.lower() in (".html", ".htm"):
                continue
            try:
                assets.add(str(child.relative_to(REPORTS_DIR.resolve())).replace("\\", "/"))
            except ValueError:
                continue
            if len(assets) >= _REF_MAX_FILES:
                break

    frozen = frozenset(assets)
    with _CACHE_LOCK:
        if len(_ASSET_CACHE) > 512:
            _ASSET_CACHE.clear()
        _ASSET_CACHE[cache_key] = frozen
    return frozen


def allowed_asset(rel: str) -> bool:
    """判断非报告文件是否可以匿名访问（必须是某篇公开报告的附件）。"""
    norm = normalize_rel(rel)
    if not norm:
        return False
    if norm.startswith(SHARED_ASSET_PREFIX):
        return _file_exists(norm)
    for report in flags():
        if norm.endswith((".html", ".htm")):
            return False
        if norm in _report_assets(report):
            return True
    return False


def public_reports() -> list[dict]:
    """公开站点首页用的报告列表（按日期倒序）。"""
    items, _ = kb_store.list_reports(view="public", sort="date_desc")
    return items


def import_legacy_state(path: str) -> dict:
    """把老站 betterlife 的 data/kb_public.json 导入本插件（幂等）。

    老站状态结构：{"version":1,"items":{rel:{"public":true,"updated_at":ts}}}。
    只处理 public=true 且报告仍存在的条目；返回 {imported, skipped, missing}，
    便于 CLI 打印和测试。
    """
    import json
    from pathlib import Path

    source = Path(path)
    result = {"imported": 0, "skipped": 0, "missing": 0, "file": str(source)}
    if not source.is_file():
        result["error"] = "文件不存在"
        return result
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    items = data.get("items") or {}
    if not isinstance(items, dict):
        result["error"] = "items 不是对象"
        return result
    for rel, meta in items.items():
        if not isinstance(meta, dict) or not meta.get("public"):
            continue
        normalized = normalize_rel(rel)
        if not normalized or not is_report_rel(normalized):
            result["skipped"] += 1
            continue
        if not report_exists(normalized):
            result["missing"] += 1
            continue
        set_public(normalized, True)
        result["imported"] += 1
    return result
