"""规则模型、config.json 读写与校验。

所有读写都在 ConfigStore 的锁内进行，保证与 manager 的热更新原子。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import List

DEFAULT_CONFIG = "config.json"


class ConfigError(Exception):
    """配置校验失败。"""


def _parse_endpoint(value: str, field_name: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    """解析 'port' 或 'host:port'。返回 (host, port)。

    - 纯数字 '8080'      -> (default_host, 8080)
    - 'host:8080'        -> (host, 8080)
    - ':8080'            -> (default_host, 8080)
    - '0.0.0.0:8080'     -> ('0.0.0.0', 8080)  （listen 场景表示全网卡）
    """
    value = (value or "").strip()
    if not value:
        raise ConfigError(f"{field_name} 不能为空")
    if ":" in value:
        host, _, port_s = value.rpartition(":")
        host = host.strip()
        # 允许 :port 形式省略 host
        if host == "":
            host = default_host
    else:
        host = default_host
        port_s = value
    try:
        port = int(port_s)
    except ValueError:
        raise ConfigError(f"{field_name} 端口不是数字: {port_s!r}")
    if not 0 < port < 65536:
        raise ConfigError(f"{field_name} 端口越界: {port}")
    # 空 host（不应到达）回退默认
    if host == "":
        host = default_host
    return host, port


@dataclass
class Rule:
    id: str
    name: str
    listen: str          # 'port' 或 'host:port'
    target: str          # 'host:port' 或 'port'（按目标主机）
    enabled: bool = True
    created_at: float = field(default_factory=lambda: __import__("time").time())

    @property
    def listen_host(self) -> str:
        # listen 场景：纯端口默认绑定 127.0.0.1
        return _parse_endpoint(self.listen, "listen", default_host="127.0.0.1")[0]

    @property
    def listen_port(self) -> int:
        return _parse_endpoint(self.listen, "listen", default_host="127.0.0.1")[1]

    @property
    def target_host(self) -> str:
        return _parse_endpoint(self.target, "target", default_host="127.0.0.1")[0]

    @property
    def target_port(self) -> int:
        return _parse_endpoint(self.target, "target", default_host="127.0.0.1")[1]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        required = ("listen", "target")
        for key in required:
            if key not in data or not data[key]:
                raise ConfigError(f"规则 {data.get('name') or data.get('id')} 缺少 {key}")
        host, port = _parse_endpoint(str(data["listen"]), "listen", "127.0.0.1")
        thost, tport = _parse_endpoint(str(data["target"]), "target", "127.0.0.1")
        # listen 保持用户原始写法；target 规范为 host:port
        listen = str(data["listen"]).strip()
        target = f"{thost}:{tport}"
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "unnamed").strip()[:64] or "unnamed",
            listen=listen,
            target=target,
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at") or 0) or __import__("time").time(),
        )
        # 注：旧 config.json 的 "ws" 键被静默忽略（向前兼容），不报错

    def validate(self) -> None:
        _parse_endpoint(self.listen, "listen", "127.0.0.1")
        _parse_endpoint(self.target, "target", "127.0.0.1")


def _unique_id(existing: List[str]) -> str:
    while True:
        rid = uuid.uuid4().hex[:12]
        if rid not in existing:
            return rid


class ConfigStore:
    """线程安全的 config.json 读写。"""

    def __init__(self, path: str = DEFAULT_CONFIG):
        self.path = path
        self._lock = threading.RLock()
        self.rules: List[Rule] = []

    # ---------- 持久化 ----------
    def load(self) -> List[Rule]:
        with self._lock:
            if not os.path.exists(self.path):
                self.rules = []
                self._save_locked()
                return self.rules
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rules_raw = data.get("rules", [])
            rules = [Rule.from_dict(r) for r in rules_raw]
            for r in rules:
                r.validate()
            self._check_listen_unique(rules)
            self.rules = rules
            return rules

    def _save_locked(self) -> None:
        with self._lock:
            payload = {"rules": [r.to_dict() for r in self.rules]}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def _check_listen_unique(self, rules: List[Rule]) -> None:
        seen = set()
        for r in rules:
            key = (r.listen_host, r.listen_port)
            if key in seen:
                raise ConfigError(f"监听端口冲突: {r.listen}")
            seen.add(key)

    # ---------- 操作（均在锁内完成校验与落盘） ----------
    def add(self, name: str, listen: str, target: str, enabled: bool = True) -> Rule:
        with self._lock:
            candidate = Rule(
                id=_unique_id([r.id for r in self.rules]),
                name=(name or "unnamed").strip()[:64] or "unnamed",
                listen=listen,
                target=target,
                enabled=enabled,
            )
            candidate.validate()
            self._check_listen_unique(self.rules + [candidate])
            self.rules.append(candidate)
            self._save_locked()
            return candidate

    def get(self, rid: str) -> Rule | None:
        with self._lock:
            for r in self.rules:
                if r.id == rid:
                    return r
            return None

    def update(self, rid: str, **fields) -> Rule:
        """支持字段: name, listen, target, enabled。listen 变更时校验冲突。

        先在校验副本上完成所有校验，通过后再改回原对象并落盘，
        保证校验失败不会把内存中的规则留在半改状态。
        """
        allowed = {"name", "listen", "target", "enabled"}
        bad = set(fields) - allowed
        if bad:
            raise ConfigError(f"不支持的字段: {bad}")
        with self._lock:
            rule = self.get(rid)
            if rule is None:
                raise ConfigError(f"规则不存在: {rid}")
            # 构造候选值
            new_listen = fields.get("listen", rule.listen)
            new_target = fields.get("target", rule.target)
            new_name = fields.get("name", rule.name)
            new_enabled = fields.get("enabled", rule.enabled)
            # 校验候选（不触碰原对象）
            candidate_listen = str(new_listen).strip()
            candidate_target = str(new_target).strip()
            _parse_endpoint(candidate_listen, "listen", "127.0.0.1")
            _parse_endpoint(candidate_target, "target", "127.0.0.1")
            if "listen" in fields:
                others = [r for r in self.rules if r.id != rid]
                probe = Rule(rid, "probe", candidate_listen, candidate_target,
                              bool(new_enabled))
                self._check_listen_unique(others + [probe])
            # 校验通过，提交到原对象
            rule.listen = candidate_listen
            rule.target = candidate_target
            rule.name = str(new_name).strip()[:64] or rule.name
            rule.enabled = bool(new_enabled)
            self._save_locked()
            return rule

    def delete(self, rid: str) -> None:
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.id != rid]
            if len(self.rules) == before:
                raise ConfigError(f"规则不存在: {rid}")
            self._save_locked()
