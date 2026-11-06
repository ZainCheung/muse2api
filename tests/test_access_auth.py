"""Cloudflare Access JWT login for the admin API (signature, audience, issuer, email allowlist)."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "app.py"

TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag-123"
GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EVIL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    """Stands in for PyJWKClient: always returns the team's real public key."""

    def get_signing_key_from_jwt(self, token):
        class K:
            key = GOOD_KEY.public_key()
        return K()


def token(email="owner@example.com", aud=AUD, iss=f"https://{TEAM}", exp_in=300, key=GOOD_KEY):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in, "sub": "u1"},
                      key, algorithm="RS256", headers={"kid": "k1"})


def unsigned(email="owner@example.com"):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    now = int(time.time())
    return b({"alg": "none", "typ": "JWT"}) + "." + b({"email": email, "aud": [AUD], "iss": f"https://{TEAM}",
                                                     "iat": now, "exp": now + 300}) + "."


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




def load_app(tmp, extra):
    os.environ.update({"MUSE2API_HOME": tmp, "MUSE2API_KEY": "test-secret", **extra})
    spec = importlib.util.spec_from_file_location(f"app_{len(extra)}_{time.time_ns()}", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-access-test-") as tmp:
        os.makedirs(os.path.join(tmp, "data", "media"), exist_ok=True)
        mod = load_app(tmp, {
            "MUSE2API_ACCESS_TEAM_DOMAIN": TEAM, "MUSE2API_ACCESS_AUD": AUD,
            "MUSE2API_ACCESS_ALLOWED_EMAILS": "Owner@Example.com", "MUSE2API_KEY_LOCKED": "1",
        })
        assert mod.access.enabled
        mod.access._jwks = FakeJWKS()

        async def status(headers):
            async with Client(mod.app, headers) as c:
                return (await c.get("/admin/status")).status_code

        H = "Cf-Access-Jwt-Assertion"
        # valid login (header or cookie), email match is case-insensitive
        assert await status({H: token()}) == 200
        assert await status({"Cookie": f"CF_Authorization={token()}"}) == 200
        # a wrong API key does not override a valid Access login
        assert await status({H: token(), "Authorization": "Bearer wrong"}) == 200
        # API key alone still works
        assert await status({"Authorization": "Bearer test-secret"}) == 200

        # rejected tokens
        bad = {
            "no credentials": {},
            "email not allowed": {H: token(email="intruder@example.com")},
            "wrong audience": {H: token(aud="another-app")},
            "wrong issuer": {H: token(iss="https://evil.cloudflareaccess.com")},
            "expired": {H: token(exp_in=-3600)},
            "signed by another key": {H: token(key=EVIL_KEY)},
            "unsigned alg=none": {H: unsigned()},
            "garbage": {H: "not-a-jwt"},
            "wrong api key": {"Authorization": "Bearer nope"},
        }
        for name, headers in bad.items():
            code = await status(headers)
            assert code == 401, f"{name}: expected 401, got {code}"

        async with Client(mod.app, {H: token()}) as c:
            s = (await c.get("/admin/session")).json()
            assert s == {"authenticated": True, "via": "cloudflare-access", "email": "owner@example.com",
                         "key_locked": True}, s
            k = (await c.get("/admin/apikey")).json()
            assert k["api_key"] == "test-secret" and k["key_locked"] is True
            # key managed by the deployment: rotation refused
            r = await c.post("/admin/apikey/rotate")
            assert r.status_code == 409, r.status_code
        async with Client(mod.app, {}) as c:
            s = (await c.get("/admin/session")).json()
            assert s["authenticated"] is False and s["access_enabled"] is True, s

        # Access not configured: JWTs are ignored entirely, behaviour unchanged
        for k in ("MUSE2API_ACCESS_TEAM_DOMAIN", "MUSE2API_ACCESS_AUD", "MUSE2API_ACCESS_ALLOWED_EMAILS", "MUSE2API_KEY_LOCKED"):
            os.environ.pop(k, None)
        plain = load_app(tmp, {})
        assert not plain.access.enabled
        async with Client(plain.app, {H: token()}) as c:
            assert (await c.get("/admin/status")).status_code == 401
        async with Client(plain.app, {"Authorization": "Bearer test-secret"}) as c:
            assert (await c.post("/admin/apikey/rotate")).status_code == 200
    print("access auth tests passed")


if __name__ == "__main__":
    asyncio.run(main())
