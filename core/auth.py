"""Web 管理面认证：单用户会话 Cookie + 密码常量时间比较。

纯标准库实现，不引入第三方。
- 会话 Cookie = 过期戳 + HMAC-SHA256 签名（secret 为进程内随机）
- 密码比较用 secrets.compare_digest 防时序侧信道
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional


class Auth:
    """管理面认证上下文。

    password 为 None 时 = 未启用密码（完全兼容无登录现状）。
    """

    def __init__(self, password: Optional[str], expiry_hours: int = 24):
        self.password = password
        self.expiry_hours = max(1, int(expiry_hours))
        self.secret = secrets.token_hex(32)  # 进程内唯一，重启会话失效

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # ------------------------------------------------------------------
    def check_password(self, provided: str) -> bool:
        if not self.enabled:
            return True
        if not provided:
            return False
        return secrets.compare_digest(provided, self.password)

    # ------------------------------------------------------------------
    def issue_cookie(self) -> str:
        """签发会话 Cookie 值：'expiry_ts.hexsig'。"""
        exp = int(time.time()) + self.expiry_hours * 3600
        sig = hmac.new(self.secret.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def verify_cookie(self, cookie_value: Optional[str]) -> bool:
        """校验会话 Cookie。"""
        if not self.enabled:
            return True
        if not cookie_value or "." not in cookie_value:
            return False
        exp_s, _, sig = cookie_value.partition(".")
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < int(time.time()):
            return False  # 已过期
        expected = hmac.new(self.secret.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
