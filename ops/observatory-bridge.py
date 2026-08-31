#!/usr/bin/env python3
"""observatory-bridge.py — make the tailnet Observatory reachable from the Windows browser.

Tailscale runs INSIDE WSL (interface tailscale0, this node is `factory-workstation`). WSL2 has its
own network namespace, so a tailnet address resolves from a WSL shell and NOT from Windows, where
the browser actually is. That is why the dashboard "could not be reached" while curl from WSL got
HTTP 200 — two different machines, one of them on the tailnet.

This listens on 0.0.0.0 inside WSL and forwards to the Observatory over the tailnet. WSL2's
localhostForwarding (default on; no .wslconfig here overrides it) then makes it reachable from
Windows at http://localhost:<port>. The bind is host-only in practice: WSL2 sits behind a NAT'd
virtual switch, so this is not published to the LAN.

This is a workaround for one machine. The durable fix is Tailscale ON WINDOWS, which also puts the
dashboard on the phone — where an escalation notification is actually read.

    python3 observatory-bridge.py [--listen-port 7788] [--target 100.103.168.32:7788]
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading

BUFSIZE = 65536


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUFSIZE)
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
    target: tuple[str, int]

    def handle(self) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=15)
        except OSError as ex:
            sys.stderr.write(f"bridge: upstream {self.target} unreachable: {ex}\n")
            return
        with upstream:
            a = threading.Thread(target=_pump, args=(self.request, upstream), daemon=True)
            b = threading.Thread(target=_pump, args=(upstream, self.request), daemon=True)
            a.start(); b.start(); a.join(); b.join()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=7788)
    ap.add_argument("--target", default="100.103.168.32:7788")
    a = ap.parse_args(argv)
    host, _, port = a.target.rpartition(":")
    Handler.target = (host, int(port))
    with Server(("0.0.0.0", a.listen_port), Handler) as srv:
        print(f"bridge: 0.0.0.0:{a.listen_port} -> {a.target}  "
              f"(open http://localhost:{a.listen_port} in Windows)", flush=True)
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
