"""Web 管理面认证：单用户会话 Cookie + 密码常量时间比较 + 登录节流。

纯标准库实现，不引入第三方。
- 会话 Cookie = 过期戳 + HMAC-SHA256 签名（secret 为进程内随机）
- 密码比较用 secrets.compare_digest 防时序侧信道
- 登录节流：按来源 IP 滑动窗口失败计数，窗口内失败 N 次锁 M 秒，
  锁定期内拒绝尝试验证（直接 429，不泄露密码对错），防暴力破解
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class Auth:
    """管理面认证上下文 + 登录节流。

    password 为 None 时 = 未启用密码（完全兼容无登录现状）。
    """

    # 登录节流默认参数（进程内常量，可调改此处）
    RATE_WINDOW = 60      # 秒：统计窗口
    RATE_MAX_FAIL = 5     # 窗口内失败上限
    RATE_LOCK = 300       # 秒：触发后锁定期
    RATE_MAX_IPS = 10000  # 内存上限：最多跟踪的 IP 数（防灌爆）

    def __init__(self, password: Optional[str], expiry_hours: int = 24):
        self.password = password
        self.expiry_hours = max(1, int(expiry_hours))
        self.secret = secrets.token_hex(32)  # 进程内唯一，重启会话失效
        # 登录节流状态（多线程管理面 → 加锁保护）
        self._fail_times: Dict[str, Deque[float]] = {}
        self._lock_until: Dict[str, float] = {}
        self._rate_lock = threading.Lock()

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

    # ------------------------------------------------------------------
    # 登录节流（按来源 IP）
    def login_allowed(self, ip: str) -> Tuple[bool, int]:
        """当前是否允许该 IP 尝试登录。

        返回 (允许, 重试秒数)：锁定期内 = (False, 剩余秒)；否则 = (True, 0)。
        锁定期内调用方应直接回 429，不做密码校验。
        """
        if not self.enabled:
            return True, 0
        now = time.time()
        with self._rate_lock:
            until = self._lock_until.get(ip, 0)
            if now < until:
                return False, int(until - now) + 1
            self._gc_locked(now)
            return True, 0

    def login_failed(self, ip: str) -> None:
        """记录一次失败；窗口内达到阈值则锁定。"""
        if not self.enabled:
            return
        now = time.time()
        with self._rate_lock:
            dq = self._fail_times.setdefault(ip, deque())
            dq.append(now)
            # 滑出窗口
            while dq and now - dq[0] > self.RATE_WINDOW:
                dq.popleft()
            if len(dq) >= self.RATE_MAX_FAIL:
                self._lock_until[ip] = now + self.RATE_LOCK
                dq.clear()  # 锁定后清零，锁定期过后可重新计数
            self._gc_locked(now)

    def login_ok(self, ip: str) -> None:
        """登录成功，清零该 IP 的失败计数。"""
        if not self.enabled:
            return
        with self._rate_lock:
            self._fail_times.pop(ip, None)
            # 不清 lock_until：即使成功，若刚在锁定期边缘，保持锁定直到过期更安全

    def _gc_locked(self, now: float) -> None:
        """内存上限保护。

        1. 摘掉已过期的锁
        2. 若仍超过上限，淘汰 lock_until 最早（最该过期）之外的多余 key
        调用方须持有 _rate_lock。
        """
        # 1. 清过期锁
        for ip in [k for k, v in self._lock_until.items() if now >= v]:
            self._lock_until.pop(ip, None)
            self._fail_times.pop(ip, None)
        # 2. 超上限则淘汰 lock_until 最大（最新锁定、最不该丢）以外的？
        #    反过来：保留最早过期的，淘汰最晚锁定的（新攻击者 IP 可再试）
        if len(self._lock_until) > self.RATE_MAX_IPS:
            keep = set(k for k, _ in sorted(self._lock_until.items(), key=lambda kv: kv[1])[: self.RATE_MAX_IPS])
            for ip in list(self._lock_until):
                if ip not in keep:
                    self._lock_until.pop(ip, None)
                    self._fail_times.pop(ip, None)
