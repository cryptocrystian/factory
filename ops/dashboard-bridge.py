#!/usr/bin/env python3
"""Bridge the tailnet observatory into Windows' localhost.

Tailscale runs INSIDE WSL, so the tailnet address 100.103.168.32 is reachable from this Linux
environment but not from the Windows browser — which is why the dashboard looked unreachable while
being perfectly healthy. WSL2 forwards Windows' localhost to services bound on 0.0.0.0 inside the
distro, so listening here and forwarding over the tailnet closes the gap without installing
anything on Windows.

    python3 ops/dashboard-bridge.py          # then open http://localhost:7788 in Windows
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import threading

TARGET = ("100.103.168.32", 7788)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            up = socket.create_connection(self.server.target, timeout=10)
        except OSError:
            return
        t = threading.Thread(target=_pipe, args=(self.request, up), daemon=True)
        t.start()
        _pipe(up, self.request)


class Bridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=7788)
    ap.add_argument("--target-host", default=TARGET[0])
    ap.add_argument("--target-port", type=int, default=TARGET[1])
    a = ap.parse_args()
    srv = Bridge(("0.0.0.0", a.listen_port), Handler)
    srv.target = (a.target_host, a.target_port)
    print(f"bridge: localhost:{a.listen_port} -> {a.target_host}:{a.target_port}", flush=True)
    print("open http://localhost:7788 in your Windows browser", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
