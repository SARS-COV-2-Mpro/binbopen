#!/usr/bin/env python3
import os
import ssl
import sys
import json
import time
import random
import socket
import threading
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RECEIVER_PORT", "8081"))
TIMEOUT = 15
POOL_SIZE = 8
WARM = 2
KEEPALIVE_SEC = 20
SECRET = os.environ.get("RECEIVER_SECRET", "")

HOST_FUTURES = "fapi.binance.com"
HOST_SPOT = "api.binance.com"

CTX = ssl.create_default_context()

DROP_REQ_EXACT = {
    "host", "content-length", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "upgrade", "te", "trailer", "x-proxy-secret",
    "cdn-loop", "true-client-ip", "x-real-ip",
}
DROP_REQ_PREFIX = ("cf-", "x-forwarded-")
DROP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-length"}

_dns = {}
_dns_lock = threading.Lock()
DNS_TTL = 300


def resolve(host):
    now = time.time()
    with _dns_lock:
        hit = _dns.get(host)
        if hit and now - hit[0] < DNS_TTL:
            return random.choice(hit[1])
    infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    ips = sorted({i[4][0] for i in infos})
    with _dns_lock:
        _dns[host] = (now, ips)
    return random.choice(ips)


def forget_dns(host):
    with _dns_lock:
        _dns.pop(host, None)


class Conn(http.client.HTTPSConnection):
    def connect(self):
        try:
            ip = resolve(self.host)
            sock = socket.create_connection((ip, self.port), self.timeout)
        except Exception:
            forget_dns(self.host)
            raise
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Pool:
    def __init__(self, host):
        self.host = host
        self.idle = []
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.idle:
                return self.idle.pop()
        return Conn(self.host, timeout=TIMEOUT, context=CTX)

    def fresh(self):
        return Conn(self.host, timeout=TIMEOUT, context=CTX)

    def put(self, conn):
        with self.lock:
            if len(self.idle) < POOL_SIZE:
                self.idle.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass


POOLS = {HOST_FUTURES: Pool(HOST_FUTURES), HOST_SPOT: Pool(HOST_SPOT)}


def pick_host(path):
    if path.startswith("/api/") or path.startswith("/sapi/"):
        return HOST_SPOT
    return HOST_FUTURES


def vpn_up():
    return os.path.exists("/sys/class/net/tun0")


def do_upstream(host, method, path, headers, body):
    pool = POOLS[host]
    t0 = time.time()
    conn = pool.get()
    mode = "reused" if conn.sock is not None else "new"
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        if mode == "new":
            raise
        # stale pooled connection: retry once on a brand new one
        mode = "retry"
        conn = pool.fresh()
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    status, rheaders = resp.status, resp.getheaders()
    if resp.will_close:
        try:
            conn.close()
        except Exception:
            pass
    else:
        pool.put(conn)
    ms = int((time.time() - t0) * 1000)
    return status, rheaders, data, mode, ms


def warm_and_keepalive():
    # open a few connections up front, then keep one alive
    time.sleep(1)
    for host in (HOST_FUTURES, HOST_SPOT):
        for _ in range(WARM):
            try:
                if not vpn_up():
                    break
                c = POOLS[host].fresh()
                c.connect()
                POOLS[host].put(c)
            except Exception:
                pass
    while True:
        time.sleep(KEEPALIVE_SEC)
        try:
            if vpn_up():
                do_upstream(HOST_FUTURES, "GET", "/fapi/v1/ping", {}, None)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "receiver/3"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None

        if SECRET and self.headers.get("X-Proxy-Secret") != SECRET:
            return self._reply(403, b'{"error":"forbidden"}',
                               {"Content-Type": "application/json", "X-Not-Forwarded": "1"})

        if not vpn_up():
            return self._reply(503, b'{"error":"vpn down"}',
                               {"Content-Type": "application/json", "X-Not-Forwarded": "1"})

        out = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in DROP_REQ_EXACT or kl.startswith(DROP_REQ_PREFIX):
                continue
            out[k] = v

        host = pick_host(self.path)
        try:
            status, rheaders, data, mode, ms = do_upstream(
                host, self.command, self.path, out, body)
        except Exception as e:
            msg = json.dumps({"error": "upstream", "detail": str(e)[:200]}).encode()
            return self._reply(502, msg,
                               {"Content-Type": "application/json", "X-Not-Forwarded": "1"})

        self.send_response(status)
        for k, v in rheaders:
            if k.lower() in DROP_RESP:
                continue
            self.send_header(k, v)
        self.send_header("X-Via-Receiver", "1")
        self.send_header("X-Pool", mode)
        self.send_header("X-Upstream-Ms", str(ms))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == "__main__":
    print(f"receiver listening on 0.0.0.0:{PORT}", flush=True)
    threading.Thread(target=warm_and_keepalive, daemon=True).start()
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
