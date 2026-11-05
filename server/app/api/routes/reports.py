"""报告正文与静态资源：在 `/reports/...` 下提供 reports 目录内容。

- HTML 报告读入后经 `rewrite_html()` 改写内链（/kb/reports/...），保证在 iframe
  内点标题/内链不会跳出面板；`Cache-Control: no-cache` 让新报告立刻可见。
- 非 HTML（css/js/png…）用 FileResponse 原样流式返回。
- 路径一律经 `safe_path()` 校验，`..` 逃逸会被 404。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.services import report_html

router = APIRouter()


def _serve(rel: str):
    if rel in ("", "/"):
        rel = "index.html"
    path = report_html.safe_path(rel)
    if path is not None and path.is_dir():
        index = path / "index.html"
        if index.is_file():
            path = index
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="报告不存在")
    if path.suffix.lower() in (".html", ".htm"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(
            report_html.rewrite_html(text),
            headers={"Cache-Control": "no-cache"},
        )
    return FileResponse(path)


@router.get("/reports")
def reports_index():
    return _serve("index.html")


@router.get("/reports/")
def reports_root():
    return _serve("index.html")


@router.get("/reports/{path:path}")
def reports_file(path: str):
    return _serve(path)
