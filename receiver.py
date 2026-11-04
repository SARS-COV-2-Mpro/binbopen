#!/usr/bin/env python3
import http.server
import urllib.request
import urllib.error
import subprocess

BINANCE_BASE = "https://fapi.binance.com"

def is_vpn_active():
    try:
        result = subprocess.run(['ip', 'link', 'show', 'tun0'],
                                 capture_output=True, text=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False

class ReceiverHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.forward_request('GET')

    def do_POST(self):
        self.forward_request('POST')

    def do_DELETE(self):
        self.forward_request('DELETE')

    def forward_request(self, method):
        print(f"[RECEIVER] Incoming {method} request: {self.path}")

        if not is_vpn_active():
            print("[RECEIVER] ❌ VPN (tun0) is NOT active — refusing to forward")
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error": "VPN tunnel is not active on receiver"}')
            return

        target_url = BINANCE_BASE + self.path
        print(f"[RECEIVER] ✅ VPN active, forwarding to -> {target_url}")

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ['host', 'content-length']}

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(target_url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                print(f"[RECEIVER] ✅ Binance responded with status {response.status}")
                self.send_response(response.status)
                for header, value in response.getheaders():
                    if header.lower() not in ['transfer-encoding', 'connection']:
                        self.send_header(header, value)
                self.end_headers()
                self.wfile.write(response.read())
        except urllib.error.HTTPError as e:
            print(f"[RECEIVER] ⚠️ Binance returned HTTP error {e.code}")
            err_body = e.read()
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
