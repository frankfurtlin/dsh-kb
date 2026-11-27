"""私有知识库的 blob 存储：内容寻址 + AES-256-GCM 加密 + COS 加密镜像。

设计（2026-11-25 定）：

- **本地内容寻址**：`<KB_DATA_DIR>/blobs/<sha[0:2]>/<sha>`，按 sha256 去重，写入幂等。
- **大文件主存 COS**：`<= KB_COS_THRESHOLD_BYTES`（默认 10 MB）落本地；
  超过阈值时加密后直接主存 COS，本地不留原文件（按需下载解密）。
- **全部私有 blob 都做加密镜像**：本地文件也会在后台线程里加密上传一份到 COS，
  失败不阻塞投喂，由 `blob` 表的 mirrored/error 字段记录，后台重试。
- **加密**：AES-256-GCM，随机 12 字节 nonce，AAD 绑定 sha（防调包）。
  文件格式 `KBENC1 | nonce(12) | ciphertext+tag`。
- **密钥**：`KB_BLOB_KEY_FILE`（默认 `~/.secrets/kb-blob.key`）里存 32 字节
  随机密钥的 base64；文件不存在时自动生成并 chmod 600。**密钥不进 COS、不进仓库。**
- **COS**：从 `~/.secrets/cos.env` 读 `COS_SECRET_ID/COS_SECRET_KEY/COS_REGION/COS_BUCKET`，
  key 前缀默认 `kb-private/blobs/`。桶是公开读的，所以落进去的内容**必须**是密文；
  没有密钥的人拿到对象也解不开。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KBENC1"
NONCE_BYTES = 12

BLOB_DIR = Path(
    os.environ.get("KB_BLOB_DIR")
    or (Path(__file__).resolve().parents[2] / "data" / "blobs")
)
# 密钥 / COS 凭据默认放在家目录的 .secrets 下：路径运行时推导，两个环境变量可覆盖。
_SECRETS_DIR = Path.home() / ".secrets"
KEY_FILE = Path(os.environ.get("KB_BLOB_KEY_FILE", str(_SECRETS_DIR / "kb-blob.key")))
COS_ENV = Path(os.environ.get("KB_COS_ENV", str(_SECRETS_DIR / "cos.env")))
COS_PREFIX = os.environ.get("KB_COS_PREFIX", "kb-private/blobs").strip("/")

#: 超过这个大小的文件主存 COS（本地不留原件）。
COS_THRESHOLD = int(os.environ.get("KB_COS_THRESHOLD_BYTES", str(10 * 1024 * 1024)))

_lock = threading.Lock()
_worker: threading.Thread | None = None
_worker_stop = threading.Event()
_cos_client = None
_cos_lock = threading.Lock()


# ---------------------------------------------------------------- 基础工具


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def drop(sha: str) -> bool:
    """删除一个 blob（本地文件 + COS 对象 + 台账行）。调用方负责确认没有报告再引用它。"""
    sha = (sha or "").strip()
    if not sha:
        return False
    try:
        if cos_exists(sha):
            _client().delete_object(Bucket=_bucket(), Key=cos_key(sha))
    except Exception:  # noqa: BLE001 - COS 删除失败不阻塞本地清理
        pass
    try:
        local_path(sha).unlink(missing_ok=True)
    except OSError:
        pass
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM blob WHERE sha = ?", (sha,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def local_path(sha: str) -> Path:
    sha = (sha or "").strip()
    if len(sha) < 4:
        raise ValueError("bad sha")
    return BLOB_DIR / sha[:2] / sha


def has_local(sha: str) -> bool:
    try:
        return local_path(sha).is_file()
    except ValueError:
        return False


# ---------------------------------------------------------------- 加密


def _key() -> bytes:
    """读取（必要时生成）AES-256 密钥。"""
    try:
        raw = KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        KEY_FILE.write_text(base64.b64encode(key).decode("ascii") + "\n", encoding="utf-8")
        os.chmod(KEY_FILE, 0o600)
        return key
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError(f"{KEY_FILE} 里的密钥不是 32 字节")
    return key


def encrypt(data: bytes, sha: str) -> bytes:
    """加密并返回 `KBENC1 + nonce + 密文`；AAD 绑定 sha。"""
    aes = AESGCM(_key())
    nonce = secrets.token_bytes(NONCE_BYTES)
    return MAGIC + nonce + aes.encrypt(nonce, data, sha.encode("ascii"))


def decrypt(blob: bytes, sha: str) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) <= len(MAGIC) + NONCE_BYTES:
        raise ValueError("不是 kb-blob 密文")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    return AESGCM(_key()).decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES :], sha.encode("ascii"))


# ---------------------------------------------------------------- COS


def _cos_env() -> dict:
    env = {}
    try:
        for line in COS_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        return {}
    return env


def cos_ready() -> bool:
    return bool(_cos_env().get("COS_SECRET_ID") and _cos_env().get("COS_SECRET_KEY"))


def _client():
    """惰性创建 COS 客户端；不可用时返回 None（调用方降级到本地）。"""
    global _cos_client
    if _cos_client is not None:
        return _cos_client
    with _cos_lock:
        if _cos_client is not None:
            return _cos_client
        env = _cos_env()
        if not env.get("COS_SECRET_ID") or not env.get("COS_SECRET_KEY"):
            return None
        try:
            from qcloud_cos import CosConfig, CosS3Client

            cfg = CosConfig(
                Region=env.get("COS_REGION", "ap-guangzhou"),
                SecretId=env["COS_SECRET_ID"],
                SecretKey=env["COS_SECRET_KEY"],
                Scheme="https",
            )
            _cos_client = CosS3Client(cfg)
            return _cos_client
        except Exception:  # noqa: BLE001 - 缺依赖/配置错误都降级
            return None


def cos_key(sha: str) -> str:
    return f"{COS_PREFIX}/{sha[:2]}/{sha}.enc"


def _bucket() -> str:
    return _cos_env().get("COS_BUCKET", "agent-1258181381")


def cos_upload(sha: str, data: bytes, mime: str = "") -> bool:
    """加密后上传到 COS；成功 True。调用方负责把结果写进 blob 表。"""
    client = _client()
    if client is None:
        return False
    try:
        client.put_object(
            Bucket=_bucket(),
            Body=encrypt(data, sha),
            Key=cos_key(sha),
            ContentType="application/octet-stream",
            Metadata={"sha256": sha, "mime": mime or "application/octet-stream", "enc": "aes-256-gcm-v1"},
        )
        return True
    except Exception:  # noqa: BLE001 - 网络/权限失败都当镜像失败
        return False


def cos_download(sha: str) -> bytes | None:
    """取回并解密；不存在/失败返回 None。"""
    client = _client()
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=_bucket(), Key=cos_key(sha))
        body = resp["Body"].get_raw_stream().read()
        return decrypt(body, sha)
    except Exception:  # noqa: BLE001
        return None


def cos_exists(sha: str) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        client.head_object(Bucket=_bucket(), Key=cos_key(sha))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- blob 表


def _db():
    """复用索引库连接（避免再开一个 sqlite 文件）。"""
    from app.services import kb_store

    return kb_store._connect()


def _blob_row(conn, sha: str):
    return conn.execute("SELECT * FROM blob WHERE sha = ?", (sha,)).fetchone()


def register(sha: str, size: int, mime: str, name: str, tier: str) -> None:
    conn = _db()
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO blob (sha, size, mime, name, tier, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sha) DO UPDATE SET size=excluded.size, mime=excluded.mime,"
            " name=COALESCE(NULLIF(blob.name,''), excluded.name)",
            (sha, int(size), mime or "", name or "", tier, now),
        )
        conn.commit()
    finally:
        conn.close()


def mark_mirrored(sha: str, ok: bool, error: str = "") -> None:
    conn = _db()
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "UPDATE blob SET mirrored = ?, mirror_at = ?, error = ? WHERE sha = ?",
            (1 if ok else 0, now, "" if ok else error[:300], sha),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 对外 API


def put(data: bytes, mime: str = "", name: str = "") -> dict:
    """写入一个 blob，返回 {sha, size, mime, tier, mirrored}。

    - <= 阈值：落本地，登记待镜像；后台线程会加密上传 COS。
    - >  阈值：加密后主存 COS；本地不留原件。上传失败时保底落本地（tier=local）。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data 必须是 bytes")
    data = bytes(data)
    sha = sha256_hex(data)
    size = len(data)
    if size > COS_THRESHOLD:
        if cos_upload(sha, data, mime):
            register(sha, size, mime, name, "cos")
            mark_mirrored(sha, True)
            _drop_local(sha)
            return {"sha": sha, "size": size, "mime": mime, "tier": "cos", "mirrored": True}
        # 云端不可用时不能丢数据：退回本地，等后台补镜像。
        _write_local(sha, data)
        register(sha, size, mime, name, "local")
        return {"sha": sha, "size": size, "mime": mime, "tier": "local", "mirrored": False}
    _write_local(sha, data)
    register(sha, size, mime, name, "local")
    _kick_mirror(sha)  # 本地小文件异步镜像
    return {"sha": sha, "size": size, "mime": mime, "tier": "local", "mirrored": False}


def get(sha: str) -> bytes | None:
    """按 sha 取回原始字节：先本地，再 COS（下载后解密）。"""
    sha = (sha or "").strip()
    if not sha:
        return None
    try:
        p = local_path(sha)
    except ValueError:
        return None
    if p.is_file():
        return p.read_bytes()
    return cos_download(sha)


def _write_local(sha: str, data: bytes) -> Path:
    p = local_path(sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p


def _drop_local(sha: str) -> None:
    try:
        local_path(sha).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def mirror_one(sha: str, *, data: bytes | None = None) -> bool:
    """把一个 blob 加密镜像到 COS（幂等）。"""
    if cos_exists(sha):
        mark_mirrored(sha, True)
        return True
    raw = data if data is not None else get(sha)
    if raw is None:
        mark_mirrored(sha, False, "blob 本地与 COS 都不存在")
        return False
    ok = cos_upload(sha, raw, "")
    mark_mirrored(sha, ok, "" if ok else "上传失败")
    return ok


def mirror_pending(limit: int = 100) -> dict:
    """把还没镜像的 blob 批量补传（供后台线程与 CLI 使用）。"""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT sha, size FROM blob WHERE mirrored = 0 AND tier = 'local'"
            " ORDER BY size ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    ok = failed = 0
    for row in rows:
        if _worker_stop.is_set() and _worker is not None:
            break
        if mirror_one(row["sha"]):
            ok += 1
        else:
            failed += 1
    return {"ok": ok, "failed": failed, "pending": len(rows)}


def _kick_mirror(sha: str) -> None:
    """触发一次后台补传（如果有 worker 在跑）。"""
    if _worker is None:
        return
    try:
        mirror_one(sha)
    except Exception:  # noqa: BLE001 - 镜像失败不能影响投喂
        pass


def start_worker(interval: int = 60) -> None:
    """后台镜像线程：每 interval 秒补传一次（幂等，可重复调用）。"""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker_stop.clear()

    def loop() -> None:
        while not _worker_stop.wait(interval):
            try:
                mirror_pending(50)
            except Exception:  # noqa: BLE001 - 线程不能死
                pass

    _worker = threading.Thread(target=loop, name="kb-blob-mirror", daemon=True)
    _worker.start()


def stop_worker() -> None:
    global _worker
    _worker_stop.set()
    if _worker is not None:
        _worker.join(timeout=3)
        _worker = None


def meta(sha: str) -> dict | None:
    """blob 台账里的一行（供路由决定 mime/文件名/下载方式）。"""
    sha = (sha or "").strip()
    if not sha:
        return None
    conn = _db()
    try:
        row = _blob_row(conn, sha)
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "sha": row["sha"],
        "size": row["size"],
        "mime": row["mime"] or "",
        "name": row["name"] or "",
        "tier": row["tier"] or "local",
        "mirrored": bool(row["mirrored"]),
    }


def stats() -> dict:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes,"
            " SUM(CASE WHEN mirrored=1 THEN 1 ELSE 0 END) mirrored,"
            " SUM(CASE WHEN tier='cos' THEN 1 ELSE 0 END) on_cos"
            " FROM blob"
        ).fetchone()
    finally:
        conn.close()
    return {
        "blobs": row["n"],
        "bytes": row["bytes"],
        "mirrored": row["mirrored"] or 0,
        "on_cos": row["on_cos"] or 0,
        "threshold": COS_THRESHOLD,
        "cos_ready": cos_ready(),
    }
