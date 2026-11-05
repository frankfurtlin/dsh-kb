"""知识库页面：`/`（直连后端）与 `/kb/`（DSH 反代 / iframe）各注册一次。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/")
def kb_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_title": "知识库"},
    )
