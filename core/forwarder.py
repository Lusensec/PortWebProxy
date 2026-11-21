"""纯 TCP 双向字节管道转发器。

连上即管道：不解析协议头，任意字节（含 CRLF、二进制）原样透传。
- 任一侧 EOF/异常即双向关闭
- 每方向 300s 空闲自动回收，防半开连接僵尸

设计说明：
  1. 每个入站连接 spawn 两个线程做双向管道（A->B 与 B->A）
  2. 头交换阶段已移除：纯字节管道对 HTTP/TCP/任意协议都正确透传
  3. 每方向独立 300s 空闲超时（recv 成功重置计时），超时对 dst 做
     shutdown(SHUT_WR) 半关闭，借对方 EOF 收掉整条连接
"""
from __future__ import annotations

import socket
import threading
from typing import Optional


class Forwarder:
    """一个转发规则的处理器：连接上游 + 双向字节管道。"""

    def __init__(self, listen_label: str, target: tuple[str, int],
                 rule_id: str):
        self.listen_label = listen_label
        self.target = target
        self.rule_id = rule_id
        self._log = None  # 由 manager 注入

    def log(self, msg: str) -> None:
        if self._log:
            self._log(f"[{self.rule_id}] {msg}")

    # ------------------------------------------------------------------
    def handle_client(self, conn: socket.socket, addr) -> None:
        """处理一个入站连接：连上游 + 双向管道。"""
        upstream: Optional[socket.socket] = None
        try:
            upstream = socket.create_connection(self.target, timeout=10)
            self._pump(conn, upstream)
        except socket.timeout:
            self.log(f"upstream connect timeout -> {self.target[0]}:{self.target[1]}")
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            self.log(f"upstream unreachable {self.target[0]}:{self.target[1]} ({e})")
        except Exception as e:  # noqa: BLE001
            self.log(f"forward error: {e!r}")
        finally:
            for s in (conn, upstream):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    def _pump(self, a: socket.socket, b: socket.socket) -> None:
        """双向字节管道，直到任一侧 EOF/异常/空闲超时。"""

        def one_way(src: socket.socket, dst: socket.socket) -> None:
            src.settimeout(300)  # 5 分钟空闲回收
            try:
                while True:
                    data = src.recv(16384)
                    if not data:
                        break
                    # 处理粘包/部分写
                    view = memoryview(data)
                    while view:
                        n = dst.send(view)
                        view = view[n:]
                    del view
            except (ConnectionResetError, BrokenPipeError, OSError, socket.timeout):
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=one_way, args=(a, b), daemon=True)
        t2 = threading.Thread(target=one_way, args=(b, a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
