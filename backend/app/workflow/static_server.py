"""Loopback-only UTF-8 server for isolated generated-page verification."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import sys


class Utf8Handler(SimpleHTTPRequestHandler):
    def guess_type(self, path):
        mime = super().guess_type(path)
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            return mime + "; charset=utf-8"
        return mime


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Utf8Handler).serve_forever()
