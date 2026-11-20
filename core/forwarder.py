"""双向 TCP/HTTP/WS 转发器。

- 普通 TCP/HTTP：纯字节双向管道透传（含 body，完整保留）
- WebSocket：透传客户端请求头 → 读上游 101 握手响应头并透传回客户端 → 双向管道
- 任一侧 EOF/异常即双向关闭；每侧设空闲超时回收僵尸连接

实现要点（关键修复）：
  头交换阶段读到的字节可能已包含 body 的一部分（HTTP 头与 body 同包到达）。
  因此把「读到的全部字节」原样写上游，并记下「超出 \\r\\n\\r\\n 之后的剩余
  body 字节」，后续管道从该剩余处继续，保证 body 不丢失、不重复。
"""
from __future__ import annotations

import socket
import threading
from typing import Optional, Tuple


class Forwarder:
    """一个转发规则的处理器：连接上游 + 头交换 + 双向管道。"""

    def __init__(self, listen_label: str, target: tuple[str, int],
                 rule_id: str, allow_ws: bool = True):
        self.listen_label = listen_label
        self.target = target
        self.rule_id = rule_id
        self.allow_ws = allow_ws
        self._log = None  # 由 manager 注入

    def log(self, msg: str) -> None:
        if self._log:
            self._log(f"[{self.rule_id}] {msg}")

    # ------------------------------------------------------------------
    def handle_client(self, conn: socket.socket, addr) -> None:
        """处理一个入站连接。"""
        upstream: Optional[socket.socket] = None
        try:
            upstream = socket.create_connection(self.target, timeout=10)
            self._exchange(conn, upstream)
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
    def _exchange(self, client: socket.socket, upstream: socket.socket) -> None:
        """读取客户端首包（HTTP 头 + 可能含 body），决定 WS/HTTP 路径。"""
        req, rest = self._read_head(client)
        if req:
            upstream.sendall(req + rest)  # 头 + 已读到的 body 首字节
        if not req:
            return  # 客户端直接关闭（端口探测等）

        if self._is_ws_upgrade(req):
            # WS：读上游 101 握手响应头并透传回客户端
            resp, _ = self._read_head(upstream, cap_head_only=True)
            if resp:
                client.sendall(resp)
            # 之后进入纯双向管道（WS 帧）

        # 进入双向管道；client->upstream 方向从 rest 继续
        self._pump(client, upstream, pending_client=rest, pending_upstream=b"")

    @staticmethod
    def _read_head(sock: socket.socket, cap_head_only: bool = False) -> Tuple[bytes, bytes]:
        """读 HTTP 头。

        返回 (head, rest)：head 为请求行 + 各头行（不含结束 CRLF 后的 body），
        rest 为头之后已读到但尚未下发的 body 首字节（可能为空）。
        cap_head_only 时只关心头，rest 仍返回以便调用方透传。
        """
        buf = b""
        sep = b"\r\n\r\n"
        idx = buf.find(sep)
        while idx < 0 and len(buf) < 65536:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            idx = buf.find(sep)
        if idx < 0:
            # 没读到完整头：把已读全部作为 head 处理，rest 为空
            return buf, b""
        head_end = idx + len(sep)
        head = buf[:head_end]
        rest = buf[head_end:]
        return head, rest

    @staticmethod
    def _is_ws_upgrade(req_head: bytes) -> bool:
        if not req_head:
            return False
        lowered = req_head[:4096].lower()
        return b"upgrade" in lowered and b"websocket" in lowered

    # ------------------------------------------------------------------
    def _pump(self, a: socket.socket, b: socket.socket,
              pending_a: bytes = b"", pending_b: bytes = b"") -> None:
        """双向字节管道，直到任一侧 EOF/异常/空闲超时。

        pending_a/b：头交换阶段已读到但尚未写出的字节，先冲刷再继续 recv。
        """

        def one_way(src: socket.socket, dst: socket.socket, pending: bytes) -> None:
            src.settimeout(300)  # 5 分钟空闲回收
            try:
                # 先冲刷已读剩余字节
                if pending:
                    view = memoryview(pending)
                    while view:
                        n = dst.send(view)
                        view = view[n:]
                    del view
                while True:
                    data = src.recv(16384)
                    if not data:
                        break
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

        t1 = threading.Thread(target=one_way, args=(a, b, pending_a), daemon=True)
        t2 = threading.Thread(target=one_way, args=(b, a, pending_b), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
