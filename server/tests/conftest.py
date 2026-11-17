"""Pytest 夹具：用临时 reports 目录 + 临时索引库跑真实 FastAPI 应用。

为什么不直接用生产 reports/：单测要可重复、不依赖 460 篇真实报告，也不污染
`server/data/kb.sqlite`。这里造 3 篇覆盖 3 个分类的合成报告，含正文段落、
站内链接与共享静态资源，足够覆盖索引/检索/公开/缓存的真实行为。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="dsh-kb-pytest-"))
REPORTS = _TMP_ROOT / "reports"
DATA = _TMP_ROOT / "data"


def _write(rel: str, html: str) -> None:
    path = REPORTS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


_A_SHARE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>A股市场全景分析报告 | 2026-01-01 收盘</title>
<link rel="stylesheet" href="/assets/shell.css">
</head><body>
<h1>A股市场全景分析报告</h1>
<p>2026年1月1日A股收盘，上证指数上涨0.86%报3612.44点，深证成指上涨1.02%，创业板指上涨1.31%。两市成交额1.42万亿元，北向资金净流入38亿元。</p>
<p>板块层面半导体、通信设备领涨，银行、煤炭走弱；涨停86家、跌停11家，市场情绪中性偏暖。</p>
<a href="/20260115/xhs-note.html">相关：小红书笔记精读</a>
</body></html>"""

_XHS = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>小红书爆款笔记精读：AI 写作工具实测</title></head><body>
<h1>AI 写作工具实测</h1>
<p>这篇笔记整理了五款 AI 写作工具的实测体验，重点比较长文结构、事实核查和小红书风格化能力，并给出选型建议。</p>
<p>结论是先用通用大模型打草稿，再用垂直工具做风格改写，最后人工核查事实。</p>
</body></html>"""

_STOCK = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>中国铝业(601600) 投资分析报告</title></head><body>
<h1>中国铝业(601600) 投资分析报告</h1>
<p>中国铝业2025年三季度资产负债率为58.3%，较年初下降1.2个百分点；经营现金流净额创近五年新高，铝价中枢上移带来盈利弹性。</p>
<p>风险提示：电解铝产能政策变化、氧化铝价格波动、海外需求走弱。</p>
</body></html>"""

_write("20260101/a-share-report.html", _A_SHARE)
_write("20260115/xhs-note.html", _XHS)
_write("20251231/stock-601800.html", _STOCK)
_write("assets/shell.css", "body{font-family:sans-serif}\n")

# config 在 import 时读取环境变量，所以必须在导入 app 之前设置。
os.environ["KB_REPORTS_DIR"] = str(REPORTS)
os.environ["KB_DATA_DIR"] = str(DATA)
os.environ["KB_EMBED_ENABLED"] = "0"
os.environ["KB_AI_AUTO"] = "0"
os.environ["KB_AI_SECRET_FILE"] = str(_TMP_ROOT / "no-ai-bridge-secret")
os.environ["KB_PUBLIC_BASE_URL"] = "https://example.test/kb-public"
os.environ["KB_GRAPH_DIR"] = str(_TMP_ROOT / "graph")
os.environ["KB_GRAPH_AUTO"] = "0"
os.environ["KB_GRAPH_TOPIC_MIN_REPORTS"] = "2"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _reset_index() -> None:
    for name in ("kb.sqlite", "kb.sqlite-wal", "kb.sqlite-shm", "digest.json"):
        path = DATA / name
        if path.exists():
            path.unlink()


@pytest.fixture()
def client():
    """每个用例一份干净索引（重建 3 篇很快），退出时关闭 lifespan。"""
    _reset_index()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def pytest_report_header(config):  # noqa: ARG001
    return f"dsh-kb tests: reports={REPORTS}"
