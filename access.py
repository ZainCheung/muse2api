"""Cloudflare Access 登录校验。

把管理后台放在 Cloudflare Access 后面时，Cloudflare 会在每个经过它的请求上附带
``Cf-Access-Jwt-Assertion``（以及 ``CF_Authorization`` cookie），内容是由团队域名签发的 JWT。

这里**验证签名**而不是简单信任请求头：服务器 IP 仍可能被直接访问、绕过 Cloudflare，
伪造的请求头没有合法签名，会被拒绝。

配置（三项都填才启用；任何一项为空则只接受 API Key，行为与之前一致）::

    MUSE2API_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
    MUSE2API_ACCESS_AUD=<Access 应用的 Application Audience (AUD) Tag>
    MUSE2API_ACCESS_ALLOWED_EMAILS=you@example.com,other@example.com
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("muse2api.access")

HEADER = "cf-access-jwt-assertion"
COOKIE = "CF_Authorization"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class AccessVerifier:
    def __init__(self):
        team = _env("MUSE2API_ACCESS_TEAM_DOMAIN").removeprefix("https://").rstrip("/")
        self.team_domain = team
        self.issuer = f"https://{team}" if team else ""
        self.audience = [a.strip() for a in _env("MUSE2API_ACCESS_AUD").split(",") if a.strip()]
        self.allowed = {e.strip().lower() for e in _env("MUSE2API_ACCESS_ALLOWED_EMAILS").split(",") if e.strip()}
        self._jwks = None
        self._lock = threading.Lock()
        if self.enabled:
            log.info("Cloudflare Access 登录已启用：%s，允许 %d 个邮箱", team, len(self.allowed))
        elif team or self.audience or self.allowed:
            log.warning("Cloudflare Access 配置不完整（需要 TEAM_DOMAIN、AUD、ALLOWED_EMAILS），已忽略")

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.audience and self.allowed)

    def _client(self):
        with self._lock:
            if self._jwks is None:
                from jwt import PyJWKClient

                # 公钥按 kid 缓存；Cloudflare 轮换密钥时遇到新 kid 会自动重新拉取
                self._jwks = PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                         cache_keys=True, lifespan=3600, timeout=10)
            return self._jwks

    def verify(self, token: str) -> str | None:
        """校验通过返回小写邮箱，否则返回 None。"""
        if not self.enabled or not token:
            return None
        try:
            import jwt

            key = self._client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]}, leeway=30,
            )
        except Exception as exc:  # noqa: BLE001 - 任何校验失败都视为未登录
            log.info("Cloudflare Access 凭证校验失败: %s", exc.__class__.__name__)
            return None
        email = str(claims.get("email") or "").strip().lower()
        if not email or email not in self.allowed:
            log.warning("Cloudflare Access 邮箱不在允许列表中: %s", email or "<无>")
            return None
        return email

    def email_from_request(self, headers, cookies) -> str | None:
        """依次尝试请求头与 cookie 中的凭证。"""
        if not self.enabled:
            return None
        for token in (headers.get(HEADER), cookies.get(COOKIE)):
            if token:
                email = self.verify(token)
                if email:
                    return email
        return None
