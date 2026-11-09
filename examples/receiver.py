"""A minimal third-party webhook receiver for local demos.

    WEBHOOK_SECRET=whsec_... python examples/receiver.py   # listens on :9000

Verifies the HMAC signature and timestamp, dedupes on Idempotency-Key, and
prints each event. Mirrors what a real marketplace app should do.
"""

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ.get("WEBHOOK_SECRET", "")
TOLERANCE = 300
seen: set[str] = set()


def verify(header: str, body: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, signature = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > TOLERANCE:
        return False
    expected = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(self.headers.get("X-AppConnect-Signature", ""), body):
            self.send_response(401)
            self.end_headers()
            return
        key = self.headers["Idempotency-Key"]
        if key in seen:
            print(f"duplicate {key}, ignoring")
        else:
            seen.add(key)
            event = json.loads(body)
            print(f"{event['type']}: {json.dumps(event['data'])}")
        self.send_response(200)
        self.end_headers()

    def do_GET(self):  # the OAuth redirect lands here in the demo
        print(f"OAuth callback: {self.path}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Installed. Exchange the code at /oauth/token/.")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("Listening on :9000")
    HTTPServer(("127.0.0.1", 9000), Handler).serve_forever()
