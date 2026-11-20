"""管理 REST API + 静态页。

基于 ThreadingHTTPServer，零第三方依赖。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from core.config import ConfigError, ConfigStore, Rule
from core.manager import RuleRegistry

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def build_api(store: ConfigStore, registry: RuleRegistry, logger: logging.Logger) -> type[BaseHTTPRequestHandler]:
    """返回绑定 store/registry 的 handler 类（工厂闭包）。"""

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

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                raise ConfigError("请求体不是合法 JSON")

        def log_message(self, fmt, *args):  # 降噪
            logger.debug("http %s", fmt % args)

        # ---------------- 路由 ----------------
        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    return self._serve_index()
                if path == "/api/rules":
                    with store._lock:
                        rules = [r.to_dict() for r in store.rules]
                    st = registry.status()
                    for r in rules:
                        r["listening"] = bool(st.get(r["id"], {}).get("listening"))
                        r["sock"] = st.get(r["id"], {}).get("sock")
                    return self._send(200, {"rules": rules})
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
            except ConfigError as e:
                return self._send_err(400, str(e), "invalid")
            except Exception as e:  # noqa: BLE001
                logger.exception("GET %s 失败", path)
                return self._send_err(500, f"{e!r}", "server")

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                if path != "/api/rules":
                    return self._send_err(404, "未找到资源", "not_found")
                body = self._read_body()
                rule = store.add(
                    name=str(body.get("name", "")),
                    listen=str(body.get("listen", "")),
                    target=str(body.get("target", "")),
                    ws=bool(body.get("ws", True)),
                    enabled=bool(body.get("enabled", True)),
                )
                try:
                    registry.apply_rule(rule)
                except OSError as e:
                    # 端口绑定失败（占用/权限）：回滚，不让配置留下「已存未监听」的规则
                    store.delete(rule.id)
                    raise
                return self._send(201, rule.to_dict())
            except ConfigError as e:
                return self._send_err(400, str(e), "invalid")
            except OSError as e:
                logger.warning("bind 失败: %s", e)
                return self._send_err(500, f"端口绑定失败: {e}", "bind_failed")
            except Exception as e:  # noqa: BLE001
                logger.exception("POST 失败")
                return self._send_err(500, f"{e!r}", "server")

        def do_PATCH(self):
            path = self.path.split("?")[0]
            parts = path.strip("/").split("/")
            # 形如 api/rules/{id}
            if len(parts) != 3 or parts[0] != "api" or parts[1] != "rules":
                return self._send_err(404, "未找到资源", "not_found")
            rid = parts[2]
            try:
                body = self._read_body()
                allowed = {}
                for k in ("name", "listen", "target", "enabled", "ws"):
                    if k in body:
                        allowed[k] = body[k]
                if not allowed:
                    return self._send_err(400, "无可更新字段", "invalid")
                prev = store.get(rid).to_dict()  # 记录旧值用于回滚
                rule = store.update(rid, **allowed)
                try:
                    registry.apply_rule(rule)
                except OSError:
                    # 回滚到旧值并恢复旧监听
                    rollback = {k: prev[k] for k in ("listen", "target", "enabled", "ws", "name")}
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

        # ---------------- 静态 ----------------
        def _serve_index(self):
            idx = os.path.join(STATIC_DIR, "index.html")
            with open(idx, "rb") as f:
                data = f.read()
            self._send(200, data, "text/html; charset=utf-8")

    return PwpHandler


def start_admin_server(host: str, port: int, store: ConfigStore,
                       registry: RuleRegistry, logger: logging.Logger) -> ThreadingHTTPServer:
    handler = build_api(store, registry, logger)
    srv = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    logger.info("管理界面: http://%s:%d", host, port)
    return srv
