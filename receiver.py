#!/usr/bin/env python3
import os
import ssl
import sys
import json
import threading
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RECEIVER_PORT", "8081"))
TIMEOUT = 15
POOL_SIZE = 8
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


class Pool:
    def __init__(self, host):
        self.host = host
        self.idle = []
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.idle:
                return self.idle.pop()
        return http.client.HTTPSConnection(self.host, timeout=TIMEOUT, context=CTX)

    def fresh(self):
        return http.client.HTTPSConnection(self.host, timeout=TIMEOUT, context=CTX)

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


def do_upstream(host, method, path, headers, body):
    pool = POOLS[host]
    conn = pool.get()
    reused = conn.sock is not None
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        if not reused:
            raise
        # stale pooled connection: retry once on a brand new one
        conn = pool.fresh()
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    status, reason, rheaders = resp.status, resp.reason, resp.getheaders()
    if resp.will_close:
        try:
            conn.close()
        except Exception:
            pass
    else:
        pool.put(conn)
    return status, rheaders, data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "receiver/2"

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

        if not os.path.exists("/sys/class/net/tun0"):
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
            status, rheaders, data = do_upstream(host, self.command, self.path, out, body)
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
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
