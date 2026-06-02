#!/usr/bin/env python3
"""Small external server for the LLM Gateway Manager UI.

Serves the built Vue Manager UI and proxies only manager API calls.
PIN/session authorization is handled by the upstream gateway.
"""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import os
from pathlib import Path
import urllib.parse


LISTEN_HOST = os.environ.get("GATEWAY_UI_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("GATEWAY_UI_PORT", "3025"))
UPSTREAM_HOST = os.environ.get("GATEWAY_UI_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("GATEWAY_UI_UPSTREAM_PORT", "8080"))
STATIC_ROOT = Path(os.environ.get(
    "GATEWAY_UI_STATIC_ROOT",
    "/home/sfproject/model-gateway-v2/manager-ui/dist",
)).resolve()

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "proxy-connection",
}


class GatewayUIProxy(BaseHTTPRequestHandler):
    server_version = "GatewayUIProxy/1.0"
    protocol_version = "HTTP/1.1"

    def _send_plain(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _upstream_path(self) -> str | None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        if path.startswith("/manager/api/"):
            return f"{path}{query}"
        return None

    def _proxy(self) -> None:
        upstream_path = self._upstream_path()
        if upstream_path is None:
            self._send_plain(404, "Gateway Manager UI is available at /")
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP
        }
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        headers["X-Forwarded-For"] = self.client_address[0] if self.client_address else ""
        headers["X-Forwarded-Proto"] = "http"

        body = None
        content_length = self.headers.get("Content-Length")
        if content_length:
            body = self.rfile.read(int(content_length))
            headers["Content-Length"] = str(len(body))

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
        try:
            conn.request(self.command, upstream_path, body=body, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except Exception as exc:
            self._send_plain(502, f"Gateway upstream unavailable: {exc}")
            return
        finally:
            conn.close()

        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in HOP_BY_HOP:
                continue
            if name.lower() == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _static_path(self) -> Path | None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in {"/", "/manager", "/manager/", "/manager/html"}:
            candidate = STATIC_ROOT / "index.html"
        else:
            candidate = (STATIC_ROOT / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(STATIC_ROOT)
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if candidate.exists() and candidate.is_file():
            return candidate
        return STATIC_ROOT / "index.html"

    def _serve_static(self) -> None:
        path = self._static_path()
        if path is None or not path.exists():
            self._send_plain(404, "Gateway Manager UI build is missing")
            return
        body = b"" if self.command == "HEAD" else path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-cache" if path.name == "index.html" else "public, max-age=31536000, immutable")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _handle(self) -> None:
        if self._upstream_path() is not None:
            self._proxy()
            return
        if self.command in {"GET", "HEAD"}:
            self._serve_static()
            return
        self._send_plain(405, "Method not allowed")

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GatewayUIProxy)
    print(
        f"[gateway-ui] {LISTEN_HOST}:{LISTEN_PORT} -> "
        f"{STATIC_ROOT} + http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/manager/api",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
