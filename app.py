"""PortWebProxy 入口：启动管理服务器 + 全部转发监听。

用法:
  python app.py --port 9000 --host 127.0.0.1 --config config.json
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# 允许从项目根直接运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import ConfigStore, ConfigError  # noqa: E402
from core.manager import RuleRegistry  # noqa: E402
from web.api import start_admin_server  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pwp")


def parse_args():
    p = argparse.ArgumentParser(description="PortWebProxy - 简易反向代理")
    p.add_argument("--port", type=int, default=9000, help="管理界面端口 (默认 9000)")
    p.add_argument("--host", default="127.0.0.1", help="管理界面绑定地址 (默认 127.0.0.1)")
    p.add_argument("--config", default="config.json", help="配置文件路径")
    p.add_argument("--log-level", default="INFO", help="日志级别")
    return p.parse_args()


def main():
    args = parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper(), logging.INFO))

    store = ConfigStore(args.config)
    registry = RuleRegistry(store, log)

    try:
        rules = store.load()
    except ConfigError as e:
        log.error("配置校验失败: %s", e)
        sys.exit(1)

    # 为已有启用规则绑定监听
    for rule in rules:
        if rule.enabled:
            try:
                registry.apply_rule(rule)
            except OSError as e:
                log.warning("规则 %s 监听失败（端口占用？）: %s", rule.name, e)

    start_admin_server(args.host, args.port, store, registry, log)
    log.info("PortWebProxy 已启动，%d 条规则，管理界面 http://%s:%d", len(rules), args.host, args.port)
    log.info("按 Ctrl+C 退出")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("正在退出…")
        for rid in list(registry._listeners):
            registry.stop_rule(rid)
        log.info("已退出")


if __name__ == "__main__":
    main()
