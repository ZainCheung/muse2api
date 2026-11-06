"""Object storage (S3/R2) for generated media, with an in-memory fake S3 client."""
import asyncio
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "app.py"


class FakeS3:
    """Implements the boto3 calls MediaStorage uses."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        with open(path, "rb") as f:
            self.objects[(bucket, key)] = (f.read(), dict(ExtraArgs or {}), dt.datetime.now(dt.timezone.utc))

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [
                    {"Key": k, "Size": len(v[0]), "LastModified": v[2]}
                    for (b, k), v in fake.objects.items() if b == Bucket and k.startswith(Prefix)
                ]}
        return P()


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body) if self.body else {}


class Client:
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, body=b"", headers=None):
        hdr = {**self.headers, **(headers or {})}
        hdr["content-length"] = str(len(body))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
            "server": ("test", 80), "client": ("test", 1)
        }
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, json_data=None, **kwargs):
        body = json.dumps(json_data).encode() if json_data is not None else b""
        headers = {"content-type": "application/json"}
        return await self.request("POST", path, body=body, headers=headers, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass




def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-media-s3-test-") as tmp:
        os.environ.update({
            "MUSE2API_HOME": tmp, "MUSE2API_KEY": "test-secret",
            "MUSE2API_STORAGE": "s3", "MUSE2API_S3_BUCKET": "bkt",
            "MUSE2API_S3_PREFIX": "muse2api/media", "MUSE2API_LOCAL_CACHE_HOURS": "1",
        })
        media_dir = os.path.join(tmp, "data", "media")
        os.makedirs(media_dir, exist_ok=True)

        spec = importlib.util.spec_from_file_location("app_candidate", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake = FakeS3()
        store = module.media_store
        store._client = fake
        assert store.enabled and store.prefix == "muse2api/media/", store.describe()

        # 1. publish uploads in the background with a content type, local copy kept
        vid = os.path.join(media_dir, "a.mp4")
        with open(vid, "wb") as f:
            f.write(b"video-bytes")
        store.publish(vid, "a.mp4")
        assert wait_for(lambda: ("bkt", "muse2api/media/a.mp4") in fake.objects), "upload did not happen"
        body, extra, _ = fake.objects[("bkt", "muse2api/media/a.mp4")]
        assert body == b"video-bytes" and extra["ContentType"] == "video/mp4"
        assert os.path.isfile(vid)

        async with Client(module.app, {"Authorization": "Bearer test-secret"}) as client:
            # 2. local file is served directly
            res = await client.get("/v1/media/a.mp4")
            assert res.status_code == 200 and res.body == b"video-bytes", res.status_code

            # 3. sweep removes only expired local copies that exist remotely
            old = time.time() - 2 * 3600
            os.utime(vid, (old, old))
            only_local = os.path.join(media_dir, "b.webp")
            with open(only_local, "wb") as f:
                f.write(b"img")
            os.utime(only_local, (old, old))
            assert store.sweep_local() == 1
            assert not os.path.exists(vid) and os.path.exists(only_local)

            # 4. missing locally -> 302 to object storage
            res = await client.get("/v1/media/a.mp4")
            assert res.status_code == 302, res.status_code

            # 5. unknown -> 404, traversal -> 400
            assert (await client.get("/v1/media/nope.mp4")).status_code == 404

            # 6. admin list merges local and remote
            data = (await client.get("/admin/media")).json()
            by = {m["name"]: m for m in data["media"]}
            assert by["a.mp4"]["remote"] and not by["a.mp4"]["local"] and by["a.mp4"]["kind"] == "video"
            assert by["b.webp"]["local"] and not by["b.webp"]["remote"]
            assert data["storage"]["backend"] == "s3"

            # 7. backfill uploads local-only files
            assert store.backfill() == 1 and ("bkt", "muse2api/media/b.webp") in fake.objects

            # 8. delete removes from both places
            res = await client.post("/admin/media/delete", json_data={"names": ["a.mp4", "b.webp", "../x"]})
            assert res.json()["removed"] == 2, res.json()
            assert not fake.objects and not os.path.exists(only_local)
            assert (await client.delete("/admin/media/a.mp4")).status_code == 404

        # 9. public base -> direct URL instead of a presigned one
        store.public_base = "https://media.example.com"
        assert store.remote_url("x.mp4") == "https://media.example.com/muse2api/media/x.mp4"

    # 10. default backend stays local and never touches S3
    from storage import MediaStorage
    os.environ["MUSE2API_STORAGE"] = "local"
    local = MediaStorage("/nonexistent")
    assert not local.enabled and local.describe() == {"backend": "local"}
    local.publish("/nonexistent/x.mp4", "x.mp4")
    print("media storage tests passed")


if __name__ == "__main__":
    asyncio.run(main())
