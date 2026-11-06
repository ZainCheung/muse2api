"""生成媒体的外部存储（S3 兼容：Cloudflare R2 / AWS S3 / MinIO 等）。

默认 ``MUSE2API_STORAGE=local``：媒体只保存在 ``data/media``，行为与之前完全一致。

设为 ``s3`` 后：

* 每个生成的文件在本地落盘后，后台线程上传到 bucket（``<prefix><文件名>``）；
* 本地文件作为缓存保留 ``MUSE2API_LOCAL_CACHE_HOURS`` 小时（默认 72，0 表示永久保留），
  过期且确认已在 bucket 中的文件会被清理，释放服务器磁盘；
* ``GET /v1/media/{name}``：本地有就直接返回；本地没有则 302 跳转到
  ``MUSE2API_S3_PUBLIC_BASE``（公开访问域名）或一个短期有效的预签名地址；
* 启动时把本地已有、bucket 里还没有的文件补传上去（从纯本地存储切换过来时用）。

R2 示例::

    MUSE2API_STORAGE=s3
    MUSE2API_S3_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
    MUSE2API_S3_REGION=auto
    MUSE2API_S3_BUCKET=deepoke-media
    MUSE2API_S3_ACCESS_KEY_ID=...
    MUSE2API_S3_SECRET_ACCESS_KEY=...
    MUSE2API_S3_PREFIX=muse2api/media/
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from typing import Any

log = logging.getLogger("muse2api.storage")

_VIDEO_EXT = (".mp4", ".webm", ".mov")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def safe_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def media_kind(name: str) -> str:
    return "video" if os.path.splitext(name)[1].lower() in _VIDEO_EXT else "image"


class MediaStorage:
    """本地目录 + 可选的 S3 兼容 bucket。

    ``client`` 参数仅供测试注入假客户端；正常运行时按环境变量懒加载 boto3。
    """

    def __init__(self, media_dir: str, client: Any = None):
        self.media_dir = media_dir
        self.backend = _env("MUSE2API_STORAGE", "local").lower()
        self.bucket = _env("MUSE2API_S3_BUCKET")
        prefix = _env("MUSE2API_S3_PREFIX", "media/")
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.public_base = _env("MUSE2API_S3_PUBLIC_BASE").rstrip("/")
        self.presign_seconds = int(_env("MUSE2API_S3_PRESIGN_SECONDS", "3600"))
        self.cache_hours = float(_env("MUSE2API_LOCAL_CACHE_HOURS", "72"))
        self._client = client
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        if self.backend == "s3" and not self.bucket:
            log.error("MUSE2API_STORAGE=s3 但没有设置 MUSE2API_S3_BUCKET，回退为本地存储")
            self.backend = "local"

    # ------------------------------------------------------------------ 基础

    @property
    def enabled(self) -> bool:
        return self.backend == "s3"

    def describe(self) -> dict:
        if not self.enabled:
            return {"backend": "local"}
        return {
            "backend": "s3",
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint": _env("MUSE2API_S3_ENDPOINT") or "aws",
            "public_base": self.public_base or None,
            "local_cache_hours": self.cache_hours,
        }

    @property
    def client(self):
        if self._client is None:
            import boto3  # 懒加载：只有启用 S3 时才需要
            from botocore.config import Config as BotoConfig

            self._client = boto3.client(
                "s3",
                endpoint_url=_env("MUSE2API_S3_ENDPOINT") or None,
                region_name=_env("MUSE2API_S3_REGION", "auto"),
                aws_access_key_id=_env("MUSE2API_S3_ACCESS_KEY_ID") or None,
                aws_secret_access_key=_env("MUSE2API_S3_SECRET_ACCESS_KEY") or None,
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 4, "mode": "standard"},
                    # R2 不支持 virtual-hosted 以外的部分特性，path 风格最通用
                    s3={"addressing_style": _env("MUSE2API_S3_ADDRESSING", "path")},
                ),
            )
        return self._client

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def local_path(self, name: str) -> str:
        return os.path.join(self.media_dir, name)

    # ------------------------------------------------------------------ 写入

    def upload(self, path: str, name: str) -> bool:
        """同步上传一个本地文件；成功返回 True。"""
        if not self.enabled or not os.path.isfile(path):
            return False
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            self.client.upload_file(
                path, self.bucket, self.key(name),
                ExtraArgs={"ContentType": ctype, "CacheControl": "public, max-age=31536000, immutable"},
            )
            log.info("已上传到对象存储: %s (%d bytes)", name, os.path.getsize(path))
            return True
        except Exception as exc:  # noqa: BLE001 - 上传失败不影响本地可用
            log.warning("上传到对象存储失败 %s: %s", name, exc)
            return False

    def publish(self, path: str | None, name: str | None) -> None:
        """生成完成后调用：后台上传，不阻塞响应。本地文件保留作为缓存。"""
        if not self.enabled or not path or not name or not safe_name(name):
            return
        with self._lock:
            if name in self._pending:
                return
            self._pending.add(name)

        def run():
            try:
                self.upload(path, name)
            finally:
                with self._lock:
                    self._pending.discard(name)

        threading.Thread(target=run, name=f"upload-{name[:12]}", daemon=True).start()

    # ------------------------------------------------------------------ 读取

    def remote_exists(self, name: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(name))
            return True
        except Exception:  # noqa: BLE001
            return False

    def remote_url(self, name: str) -> str:
        if self.public_base:
            return f"{self.public_base}/{self.key(name)}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self.key(name)},
            ExpiresIn=self.presign_seconds,
        )

    def list_remote(self) -> dict[str, dict]:
        """bucket 中的媒体：{文件名: {bytes, mtime}}。"""
        out: dict[str, dict] = {}
        if not self.enabled:
            return out
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
                for obj in page.get("Contents") or []:
                    name = obj["Key"][len(self.prefix):]
                    if not safe_name(name):
                        continue
                    mtime = obj.get("LastModified")
                    out[name] = {
                        "bytes": int(obj.get("Size") or 0),
                        "mtime": int(mtime.timestamp()) if hasattr(mtime, "timestamp") else int(time.time()),
                    }
        except Exception as exc:  # noqa: BLE001
            log.warning("列出对象存储失败: %s", exc)
        return out

    def list_all(self) -> list[dict]:
        """本地 + bucket 合并后的媒体列表（按时间倒序）。"""
        items: dict[str, dict] = {}
        if os.path.isdir(self.media_dir):
            for name in os.listdir(self.media_dir):
                p = self.local_path(name)
                if not os.path.isfile(p) or not safe_name(name) or name.startswith("."):
                    continue
                items[name] = {"name": name, "bytes": os.path.getsize(p),
                               "mtime": int(os.path.getmtime(p)), "local": True, "remote": False}
        for name, meta in self.list_remote().items():
            if name in items:
                items[name]["remote"] = True
            else:
                items[name] = {"name": name, **meta, "local": False, "remote": True}
        for it in items.values():
            it["kind"] = media_kind(it["name"])
        return sorted(items.values(), key=lambda x: x["mtime"], reverse=True)

    # ------------------------------------------------------------------ 删除

    def delete(self, name: str) -> bool:
        """删除本地与 bucket 中的同名文件；任一处删除成功即返回 True。"""
        removed = False
        p = self.local_path(name)
        if os.path.isfile(p):
            os.remove(p)
            removed = True
        if self.enabled:
            try:
                if self.remote_exists(name):
                    self.client.delete_object(Bucket=self.bucket, Key=self.key(name))
                    removed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("删除对象存储文件失败 %s: %s", name, exc)
                raise
        return removed

    # ------------------------------------------------------------------ 维护

    def backfill(self) -> int:
        """把本地已有、bucket 里没有的文件补传上去。"""
        if not self.enabled or not os.path.isdir(self.media_dir):
            return 0
        remote = self.list_remote()
        count = 0
        for name in sorted(os.listdir(self.media_dir)):
            p = self.local_path(name)
            if name in remote or not os.path.isfile(p) or not safe_name(name) or name.startswith("."):
                continue
            if self.upload(p, name):
                count += 1
        if count:
            log.info("已补传 %d 个本地媒体到对象存储", count)
        return count

    def sweep_local(self, now: float | None = None) -> int:
        """清理超过缓存时长、且已确认在 bucket 中的本地文件。"""
        if not self.enabled or self.cache_hours <= 0 or not os.path.isdir(self.media_dir):
            return 0
        cutoff = (now or time.time()) - self.cache_hours * 3600
        removed = 0
        with self._lock:
            pending = set(self._pending)
        for name in os.listdir(self.media_dir):
            p = self.local_path(name)
            if name in pending or not os.path.isfile(p) or os.path.getmtime(p) > cutoff:
                continue
            if self.remote_exists(name):
                os.remove(p)
                removed += 1
        if removed:
            log.info("已清理 %d 个已上传的本地缓存文件", removed)
        return removed

    def start_maintenance(self, interval_seconds: int = 3600) -> None:
        """后台：启动时补传一次，之后定期清理本地缓存。"""
        if not self.enabled:
            return

        def loop():
            try:
                self.backfill()
            except Exception as exc:  # noqa: BLE001
                log.warning("补传失败: %s", exc)
            while True:
                try:
                    self.sweep_local()
                except Exception as exc:  # noqa: BLE001
                    log.warning("清理本地缓存失败: %s", exc)
                time.sleep(interval_seconds)

        threading.Thread(target=loop, name="media-storage-maintenance", daemon=True).start()
