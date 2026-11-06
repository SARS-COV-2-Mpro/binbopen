#!/usr/bin/env python3
import http.server
import urllib.request
import urllib.error
import subprocess

BINANCE_BASE = "https://fapi.binance.com"

STRIP_HEADERS = [
    'host', 'content-length',
    'x-forwarded-for', 'x-real-ip', 'x-client-ip', 'cf-connecting-ip',
    'true-client-ip', 'forwarded', 'forwarded-for', 'x-forwarded',
    'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-port', 'via',
    'cf-ray', 'cf-ipcountry', 'cdn-loop', 'cf-visitor', 'cf-worker', 'cf-ew-via',
]

def is_vpn_active():
    try:
        r = subprocess.run(['ip', 'link', 'show', 'tun0'],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False

class ReceiverHandler(http.server.BaseHTTPRequestHandler):
    # Mark every response as "this really came from the receiver"
    def end_headers(self):
        self.send_header('X-Via-Receiver', '1')
        super().end_headers()

    def do_GET(self):    self.forward_request('GET')
    def do_POST(self):   self.forward_request('POST')
    def do_PUT(self):    self.forward_request('PUT')
    def do_DELETE(self): self.forward_request('DELETE')

    def forward_request(self, method):
        print(f"[RECEIVER] Incoming {method} request: {self.path}")

        # Read body first so the connection stays clean even if we refuse
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        if not is_vpn_active():
            print("[RECEIVER] ❌ VPN (tun0) is NOT active — refusing to forward")
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('X-Not-Forwarded', '1')
            self.end_headers()
            self.wfile.write(b'{"error": "VPN tunnel is not active on receiver"}')
            return

        target_url = BINANCE_BASE + self.path
        print(f"[RECEIVER] ✅ VPN active, forwarding to -> {target_url}")

        headers = {}
        stripped = []
        for k, v in self.headers.items():
            if k.lower() in STRIP_HEADERS:
                stripped.append(k)
            else:
                headers[k] = v
        if stripped:
            print(f"[RECEIVER] ⚠️ Stripped headers: {stripped}")
        print(f"[RECEIVER] Final header names sent: {list(headers.keys())}")

        req = urllib.request.Request(target_url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                print(f"[RECEIVER] ✅ Binance responded with status {response.status}")
                data = response.read()
                self.send_response(response.status)
                for header, value in response.getheaders():
                    if header.lower() not in ['transfer-encoding', 'connection']:
                        self.send_header(header, value)
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            print(f"[RECEIVER] ⚠️ Binance returned HTTP error {e.code}")
            err_body = e.read()
            print(f"[RECEIVER] ⚠️ Binance error body: {err_body}")
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            print(f"[RECEIVER] ❌ Exception while forwarding: {e}")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        print("[RECEIVER LOG] " + (format % args))

if __name__ == '__main__':
    print("✅ Receiver starting on port 8081 -> forwarding to fapi.binance.com")
    server = http.server.ThreadingHTTPServer(('0.0.0.0', 8081), ReceiverHandler)
    server.serve_forever()
