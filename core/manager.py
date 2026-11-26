"""规则注册表：监听 socket 生命周期与热更新。

与 ConfigStore 共用一把 RLock，保证「写盘 + 调整监听」原子。
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Dict, Optional

from .config import ConfigStore, Rule
from .forwarder import Forwarder


class RuleRegistry:
    def __init__(self, store: ConfigStore, logger: Optional[logging.Logger] = None):
        self.store = store
        self.log = logger or logging.getLogger("pf")
        self._active: Dict[str, Forwarder] = {}      # rule_id -> Forwarder
        self._listeners: Dict[str, socket.socket] = {}  # rule_id -> listen socket

    # ------------------------------------------------------------------
    def start_rule(self, rule: Rule) -> None:
        """为启用规则绑定监听 socket。失败抛异常由调用方处理。"""
        if rule.id in self._listeners:
            return
        host = rule.listen_host
        port = rule.listen_port
        fwd = Forwarder(rule.listen, (rule.target_host, rule.target_port),
                        rule.id)
        fwd._log = self.log.info  # 注入日志
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(128)
        self._listeners[rule.id] = s
        self._active[rule.id] = fwd
        t = threading.Thread(target=self._serve, args=(rule, s, fwd), daemon=True)
        t.start()
        self.log.info("监听 %s -> %s:%s  [%s]", rule.listen, rule.target_host, rule.target_port, rule.name)

    def _serve(self, rule: Rule, s: socket.socket, fwd: Forwarder) -> None:
        try:
            while True:
                conn, addr = s.accept()
                threading.Thread(target=fwd.handle_client, args=(conn, addr),
                                 daemon=True).start()
        except OSError:
            pass  # socket 被关闭（热更新 / 禁用）
        finally:
            try:
                s.close()
            except Exception:
                pass

    def stop_rule(self, rid: str) -> None:
        s = self._listeners.pop(rid, None)
        self._active.pop(rid, None)
        if s:
            try:
                s.close()
            except Exception:
                pass

    def stop_all(self) -> None:
        """关闭全部监听（退出清理用）。"""
        for rid in list(self._listeners):
            self.stop_rule(rid)

    # ------------------------------------------------------------------
    def apply_rule(self, rule: Rule) -> None:
        """根据 enabled 状态与规则内容启停/重建监听。

        - 停用：关闭监听
        - 启用且未监听：新建监听
        - 启用且已在监听：仅当 listen/target 变化时重建，避免无谓中断
        """
        with self.store._lock:
            if not rule.enabled:
                self.stop_rule(rule.id)
                return
            live = self._active.get(rule.id)
            changed = live is None or (
                live.target != (rule.target_host, rule.target_port)
                or live.listen_label != rule.listen
            )
            if changed:
                self.stop_rule(rule.id)
                self.start_rule(rule)

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self.store._lock:
            out = {}
            for rid, s in self._listeners.items():
                out[rid] = {"listening": True, "sock": f"{s.getsockname()[0]}:{s.getsockname()[1]}"}
            for rid in self._active:
                out.setdefault(rid, {"listening": False, "sock": None})
            return out
