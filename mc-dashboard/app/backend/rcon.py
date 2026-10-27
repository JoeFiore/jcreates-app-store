"""Minimal Minecraft RCON client (stdlib only).

The RCON protocol: each packet is int32 length + int32 request id +
int32 type + UTF-8 payload + two null bytes. Type 3 = auth, type 2 =
execute command, type 0 = response. Auth failure comes back with
request id -1.
"""
from __future__ import annotations

import socket
import struct
import time

_AUTH = 3
_EXECCOMMAND = 2
_AUTH_FAILED = -1


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._req_id = 0

    # ---------- low level ----------
    def _packet(self, req_id: int, ptype: int, payload: str) -> bytes:
        body = (struct.pack("<ii", req_id, ptype) +
                payload.encode("utf-8") + b"\x00\x00")
        return struct.pack("<i", len(body)) + body

    def _recvall(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))  # type: ignore[union-attr]
            if not chunk:
                raise RconError("connection closed by server")
            buf += chunk
        return buf

    def _read_packet(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recvall(4))
        if length < 10 or length > 4096 + 10:
            raise RconError(f"bad packet length {length}")
        data = self._recvall(length)
        req_id, ptype = struct.unpack("<ii", data[:8])
        payload = data[8:-2].decode("utf-8", errors="replace")
        return req_id, ptype, payload

    # ---------- public ----------
    def connect(self) -> None:
        self.close()
        try:
            s = socket.create_connection((self.host, self.port),
                                         timeout=self.timeout)
        except OSError as e:
            raise RconError(f"cannot connect to {self.host}:{self.port}: {e}")
        s.settimeout(self.timeout)
        self._sock = s
        self._req_id += 1
        rid = self._req_id
        s.sendall(self._packet(rid, _AUTH, self.password))
        # vanilla sometimes sends a stray empty packet first; read a few
        s.settimeout(0.5)
        ok = False
        try:
            for _ in range(3):
                r, _, _ = self._read_packet()
                if r == _AUTH_FAILED:
                    raise RconError("authentication failed (wrong password?)")
                if r == rid:
                    ok = True
                    break
        except socket.timeout:
            pass
        finally:
            s.settimeout(self.timeout)
        if not ok:
            self.close()
            raise RconError("no auth response from server")

    def command(self, cmd: str) -> str:
        """Send one command; returns its output. Opens a fresh connection."""
        self.connect()
        assert self._sock is not None
        self._req_id += 1
        rid = self._req_id
        self._sock.sendall(self._packet(rid, _EXECCOMMAND, cmd))
        # response may arrive fragmented across packets; collect until a gap.
        # the server may also close the connection instead (e.g. after "stop")
        # — treat that as end-of-response, not an error.
        out: list[str] = []
        deadline = time.time() + self.timeout
        self._sock.settimeout(0.4)
        try:
            while True:
                try:
                    r, _, payload = self._read_packet()
                except (socket.timeout, RconError):
                    break
                if r == rid:
                    out.append(payload)
                if time.time() > deadline:
                    break
        finally:
            self.close()
        return "".join(out).strip()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
