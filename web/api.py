"""管理 REST API + 静态页 + 登录。

基于 ThreadingHTTPServer，零第三方依赖。
静态文件目录在 PyInstaller 打包后位于 _MEIPASS（临时解包目录），
开发时位于源码目录；用 sys.frozen 区分。

认证：
- Auth.enabled=False 时完全兼容现状（无登录页，直接进）
- Auth.enabled=True 时保护 / 与全部 /api/*，未认证 302 /login（GET）或 401（API）
- 登录成功种 HttpOnly 会话 Cookie（HMAC 签名，默认 24h）
"""
from __future__ import annotations

import http.cookies
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from core.auth import Auth
from core.config import ConfigError, ConfigStore, Rule
from core.manager import RuleRegistry

if getattr(sys, "frozen", False):
    STATIC_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.argv[0])), "static")
else:
    STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

SESSION_COOKIE = "pwp_session"


def build_api(store: ConfigStore, registry: RuleRegistry,
              auth: Auth, logger: logging.Logger) -> type[BaseHTTPRequestHandler]:
    """返回绑定 store/registry/auth 的 handler 类（工厂闭包）。"""

    class PwpHandler(BaseHTTPRequestHandler):
        server_version = "PortWebProxy/1.0"

        # ---------------- 基础设施 ----------------
        def _send(self, code: int, payload, content_type: str = "application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_err(self, code: int, msg: str, err_code: str = "error"):
            self._send(code, {"error": msg, "code": err_code})

        def _send_429(self, retry_after: int, msg: str = "登录尝试过于频繁，请稍后再试"):
            body = json.dumps({"error": msg, "code": "rate_limited", "retry_after": retry_after}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(body)

        def _send_json_with_cookie(self, code: int, payload, set_cookie: Optional[str] = None):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                raise ConfigError("请求体不是合法 JSON")

        def log_message(self, fmt, *args):  # 降噪
            logger.debug("http %s", fmt % args)

        # ---------------- 认证守卫 ----------------
        def _session_cookie(self) -> Optional[str]:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return c[SESSION_COOKIE].value if SESSION_COOKIE in c else None

        def _authenticated(self) -> bool:
            return auth.verify_cookie(self._session_cookie())

        def _require_auth(self, path: str) -> bool:
            """True = 已认证可继续；False = 已发 401/302，调用方直接 return。"""
            if not auth.enabled:
                return True
            if self._authenticated():
                return True
            if path.startswith("/api/"):
                self._send_err(401, "未认证", "unauthorized")
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return False

        # ---------------- 静态 ----------------
        def _serve_file(self, fname: str, code: int = 200):
            with open(os.path.join(STATIC_DIR, fname), "rb") as f:
                data = f.read()
            self._send(code, data, "text/html; charset=utf-8")

        # ---------------- 路由 ----------------
        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/login":
                    if self._authenticated():
                        self.send_response(302)
                        self.send_header("Location", "/")
                        self.end_headers()
                        return
                    return self._serve_file("login.html")
                if path == "/":
                    if not self._require_auth(path):
                        return
                    return self._serve_file("index.html")
                if path.startswith("/api/"):
                    if not self._require_auth(path):
                        return
                    return self._api_get(path)
                return self._send_err(404, "未找到资源", "not_found")
            except ConfigError as e:
                return self._send_err(400, str(e), "invalid")
            except Exception as e:  # noqa: BLE001
                logger.exception("GET %s 失败", path)
                return self._send_err(500, f"{e!r}", "server")

        def _api_get(self, path: str):
            if path == "/api/rules":
                with store._lock:
                    rules = [r.to_dict() for r in store.rules]
                st = registry.status()
                for r in rules:
                    r["listening"] = bool(st.get(r["id"], {}).get("listening"))
                    r["sock"] = st.get(r["id"], {}).get("sock")
                return self._send(200, {
                    "rules": rules,
                    "auth": {"enabled": auth.enabled, "authenticated": self._authenticated()},
                })
            if path == "/api/status":
                st = registry.status()
                with store._lock:
                    total = len(store.rules)
                    enabled = sum(1 for r in store.rules if r.enabled)
                return self._send(200, {
                    "total": total, "enabled": enabled,
                    "listening": sum(1 for v in st.values() if v.get("listening")),
                    "listeners": st,
                })
            if path.startswith("/api/rules/"):
                rid = path.rsplit("/", 1)[-1]
                with store._lock:
                    rule = store.get(rid)
                    d = rule.to_dict() if rule else None
                if d is None:
                    return self._send_err(404, "规则不存在", "not_found")
                st = registry.status()
                d["listening"] = bool(st.get(rid, {}).get("listening"))
                return self._send(200, d)
            return self._send_err(404, "未找到资源", "not_found")

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                if path == "/login":
                    return self._login()
                if path == "/logout":
                    return self._logout()
                if path.startswith("/api/"):
                    if not self._require_auth(path):
                        return
                    if path == "/api/rules":
                        return self._create_rule()
                    if path == "/api/password":
                        return self._change_password()
                return self._send_err(404, "未找到资源", "not_found")
            except ConfigError as e:
                return self._send_err(400, str(e), "invalid")
            except OSError as e:
                logger.warning("bind 失败: %s", e)
                return self._send_err(500, f"端口绑定失败: {e}", "bind_failed")
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s 失败", path)
                return self._send_err(500, f"{e!r}", "server")

        def do_PATCH(self):
            path = self.path.split("?")[0]
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "api" or parts[1] != "rules":
                return self._send_err(404, "未找到资源", "not_found")
            if not self._require_auth(path):
                return
            rid = parts[2]
            try:
                body = self._read_body()
                allowed = {}
                for k in ("name", "listen", "target", "enabled"):
                    if k in body:
                        allowed[k] = body[k]
                if not allowed:
                    return self._send_err(400, "无可更新字段", "invalid")
                prev = store.get(rid).to_dict()
                rule = store.update(rid, **allowed)
                try:
                    registry.apply_rule(rule)
                except OSError:
                    rollback = {k: prev[k] for k in ("listen", "target", "enabled", "name")}
                    store.update(rid, **rollback)
                    registry.apply_rule(store.get(rid))
                    raise
                return self._send(200, rule.to_dict())
            except ConfigError as e:
                return self._send_err(400, str(e), "invalid")
            except OSError as e:
                logger.warning("bind 失败: %s", e)
                return self._send_err(500, f"端口绑定失败: {e}", "bind_failed")
            except Exception as e:  # noqa: BLE001
                logger.exception("PATCH 失败")
                return self._send_err(500, f"{e!r}", "server")

        def do_DELETE(self):
            path = self.path.split("?")[0]
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "api" or parts[1] != "rules":
                return self._send_err(404, "未找到资源", "not_found")
            if not self._require_auth(path):
                return
            rid = parts[2]
            try:
                registry.stop_rule(rid)
                store.delete(rid)
                return self._send(200, {"deleted": rid})
            except ConfigError as e:
                return self._send_err(404, str(e), "not_found")
            except Exception as e:  # noqa: BLE001
                logger.exception("DELETE 失败")
                return self._send_err(500, f"{e!r}", "server")

        def _create_rule(self):
            body = self._read_body()
            rule = store.add(
                name=str(body.get("name", "")),
                listen=str(body.get("listen", "")),
                target=str(body.get("target", "")),
                enabled=bool(body.get("enabled", True)),
            )
            try:
                registry.apply_rule(rule)
            except OSError as e:
                store.delete(rule.id)  # 回滚，不留「已存未监听」规则
                raise
            return self._send(201, rule.to_dict())

        # ---------------- 认证动作 ----------------
        def _login(self):
            ip = self.client_address[0]
            # 登录节流：锁定期内直接 429，不做密码校验（不泄露对错）
            allowed, retry = auth.login_allowed(ip)
            if not allowed:
                return self._send_429(retry)
            body = self._read_body()
            pwd = str(body.get("password", ""))
            if not auth.check_password(pwd):
                auth.login_failed(ip)
                return self._send_err(401, "密码错误", "bad_password")
            auth.login_ok(ip)
            cookie_val = auth.issue_cookie()
            return self._send_json_with_cookie(
                200, {"ok": True},
                f"{SESSION_COOKIE}={cookie_val}; Path=/; HttpOnly; SameSite=Strict; Max-Age={auth.expiry_hours * 3600}",
            )

        def _logout(self):
            return self._send_json_with_cookie(
                200, {"ok": True},
                f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict",
            )

        def _change_password(self):
            body = self._read_body()
            old = str(body.get("old", ""))
            new = str(body.get("new", ""))
            if not auth.enabled:
                # 从「无密码」升级为「有密码」
                if not new:
                    return self._send_err(400, "新密码不能为空", "invalid")
                store.set_password(new)
                auth.password = new  # 热生效
                return self._send(200, {"ok": True, "password_set": True})
            # 已启用密码：必须校验旧密码
            if not auth.check_password(old):
                return self._send_err(401, "旧密码错误", "bad_password")
            if new == "":
                # 允许清空密码（回退到无密码）
                store.set_password(None)
                auth.password = None
                return self._send(200, {"ok": True, "password_cleared": True})
            store.set_password(new)
            auth.password = new  # 热生效
            return self._send(200, {"ok": True, "password_changed": True})

    return PwpHandler


def start_admin_server(host: str, port: int, store: ConfigStore,
                       registry: RuleRegistry, auth: Auth, logger: logging.Logger) -> ThreadingHTTPServer:
    handler = build_api(store, registry, auth, logger)
    srv = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    logger.info("管理界面: http://%s:%d%s", host, port, "（需登录）" if auth.enabled else "（无密码）")
    return srv
