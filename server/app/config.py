"""运行期配置：唯一从环境变量读路径的地方。

数据源默认指向 betterlife 的只读目录（报告由 Hermes cron 写进去），插件不复制、
不改写它们；本插件唯一的写入点是 `KB_DATA_DIR` 下的 SQLite 索引库（摘要/正文缓存
与已读/收藏状态）。没有登录体系、没有公开开关——鉴权由 DSH 反代负责。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVER_DIR = APP_DIR.parent
PLUGIN_ROOT = SERVER_DIR.parent
WEB_DIR = APP_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

DEFAULT_REPORTS_DIR = "/root/workspace/betterlife/reports"
# 插件自己的索引库（SQLite：报告元数据 + 摘要/正文文本 + 已读/收藏状态）。
# 与 reports/ 分离：报告目录始终只读，本目录是插件唯一写入位置。
DEFAULT_DATA_DIR = str(SERVER_DIR / "data")


def _path_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, "").strip() or default
    return Path(raw).expanduser()


# 报告根目录（只读）：扫描这里建立索引。
REPORTS_DIR = _path_env("KB_REPORTS_DIR", DEFAULT_REPORTS_DIR)
# 索引数据库目录（唯一写入点）。
DATA_DIR = _path_env("KB_DATA_DIR", DEFAULT_DATA_DIR)
INDEX_FILE = DATA_DIR / "kb.sqlite"

# DSH 反代挂载前缀：报告正文里的内链统一改写成这个前缀下的绝对路径，
# 这样 iframe 直连 8916（/kb/...）与 DSH 内（/kb/...）都能打开。
ROUTE_PREFIX = "/kb"

# ---------- AI（P1–P5）：模型能力由 DSH 自己的 LLM（dsh-agent 默认模型）提供 ----------
# 后端不持有任何模型密钥：所有模型请求发到 host 插件的内部 AI 桥
# （/kb/_internal/ai），桥再用 DSH 的默认模型完成调用并回传文本。
# 桥只接受回环地址 + 共享密钥；密钥文件由 host 半生成（0600），后端只读。
AI_URL = (os.environ.get("KB_AI_URL", "").strip() or "http://127.0.0.1:3080/kb/_internal/ai").rstrip("/")
AI_SECRET_FILE = _path_env("KB_AI_SECRET_FILE", str(SERVER_DIR / ".ai-secret"))
# 单次模型调用超时（秒）：RAG/周报要调模型，留足余量。
AI_TIMEOUT = float(os.environ.get("KB_AI_TIMEOUT", "180") or 180)
AI_MAX_TOKENS = max(400, int(os.environ.get("KB_AI_MAX_TOKENS", "2000") or 2000))
# 同一进程两次模型请求的最小间隔（秒）；提速由 DSH 侧限流决定，默认不额外节流。
AI_MIN_INTERVAL = max(0.0, float(os.environ.get("KB_AI_MIN_INTERVAL", "0") or 0))
# 后台自动补全：每 AI_INTERVAL 秒取 AI_BATCH 条未处理的报告。
AI_AUTO = os.environ.get("KB_AI_AUTO", "1").strip().lower() not in ("0", "false", "no", "off")
AI_BATCH = max(1, int(os.environ.get("KB_AI_BATCH", "3") or 3))
AI_INTERVAL = max(10.0, float(os.environ.get("KB_AI_INTERVAL", "120") or 120))
AI_CONCURRENCY = max(1, int(os.environ.get("KB_AI_CONCURRENCY", "2") or 2))
AI_BODY_LIMIT = max(1000, int(os.environ.get("KB_AI_BODY_LIMIT", "6000") or 6000))

# ---------- AI（P2）：本地向量（fastembed + ONNX，不依赖外部 embedding API） ----------
# 模型与 HF 缓存都放在插件自己的 data/ 下（沙箱只允许写 workspace）。
EMBED_ENABLED = os.environ.get("KB_EMBED_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
EMBED_MODEL = (os.environ.get("KB_EMBED_MODEL", "").strip() or "BAAI/bge-small-zh-v1.5")
EMBED_MODEL_DIR = str(_path_env("KB_EMBED_MODEL_DIR", str(DATA_DIR / "models")))
EMBED_HF_HOME = str(_path_env("KB_EMBED_HF_HOME", str(DATA_DIR / "hf-cache")))
EMBED_HF_ENDPOINT = (os.environ.get("KB_EMBED_HF_ENDPOINT", "").strip() or "https://hf-mirror.com")
# 建向量时每篇文档送入模型的正文上限（标题/摘要/标签已在前面）。
EMBED_BODY_LIMIT = max(500, int(os.environ.get("KB_EMBED_BODY_LIMIT", "2000") or 2000))
# 后台建向量的批次大小与批次间隔（秒）。
EMBED_BATCH = max(1, int(os.environ.get("KB_EMBED_BATCH", "16") or 16))
EMBED_INTERVAL = max(0.5, float(os.environ.get("KB_EMBED_INTERVAL", "2") or 2))
# 混合检索权重：语义分 / 词法命中 / 新鲜度加成。
HYBRID_WEIGHT_SEMANTIC = float(os.environ.get("KB_HYBRID_SEMANTIC", "0.62") or 0.62)
HYBRID_WEIGHT_LEXICAL = float(os.environ.get("KB_HYBRID_LEXICAL", "0.38") or 0.38)
