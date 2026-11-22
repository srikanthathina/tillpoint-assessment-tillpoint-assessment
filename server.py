"""Run the API: python3 server.py [db-file] [port]"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from tillpoint import db
from tillpoint.api import App, dumps

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            if self.path == "/" and self.command in ("GET", "HEAD"):
                data = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command == "GET":
                    self.wfile.write(data)
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = app.handle(self.command, self.path, body)
            data = dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_HEAD = _serve
    return Handler


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "tillpoint.db"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    HTTPServer(("0.0.0.0", port), make_handler(App(db.connect(path)))).serve_forever()
