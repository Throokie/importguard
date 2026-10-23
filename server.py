"""Loopback-only, stateless UI server for ImportGuard."""
import argparse
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from importguard import InputError, VERSION, analyze, export_bundle, inspect
from localization import error_payload, request_language, format_message

ROOT = Path(__file__).resolve().parent
MAX_REQUEST = 15 * 1024 * 1024


def demo():
    import base64
    def file(name):
        return {"name": name, "content": base64.b64encode((ROOT / "examples" / name).read_bytes()).decode()}
    config = json.loads((ROOT / "examples" / "config.json").read_text())
    return {"existing": file("existing.csv"), "incoming": file("incoming.csv"), **config}


class Handler(BaseHTTPRequestHandler):
    server_version = "ImportGuard/" + VERSION
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def send(self, status, body, content_type="application/json; charset=utf-8", attachment=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def fail(self, status, code, **params):
        return self.send(status, error_payload(InputError(code, **params), self.language))

    def do_GET(self):
        self.language = request_language(accept_language=self.headers.get("Accept-Language"))
        if not self.valid_host():
            return self.fail(403, "http.loopback")
        routes = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/i18n.json": ("i18n.json", "application/json; charset=utf-8"),
                  "/messages.json": ("messages.json", "application/json; charset=utf-8")}
        if self.path == "/api/demo":
            return self.send(200, demo())
        if self.path == "/api/health":
            return self.send(200, {"status": "ok", "version": VERSION, "storage": "memory only", "bind": "127.0.0.1"})
        if self.path not in routes:
            return self.fail(404, "http.not_found")
        filename, content_type = routes[self.path]
        self.send(200, (ROOT / "static" / filename).read_bytes(), content_type)

    def do_POST(self):
        self.language = request_language(accept_language=self.headers.get("Accept-Language"))
        if not self.valid_host():
            return self.fail(403, "http.loopback")
        accepted = (self.origin(), f"http://localhost:{self.server.server_port}")
        if self.headers.get("Origin") and self.headers["Origin"] not in accepted:
            return self.fail(403, "http.origin")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.fail(415, "http.json")
        if self.headers.get("Transfer-Encoding"):
            return self.fail(400, "http.chunked")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST:
                return self.fail(413, "http.size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise InputError("http.object")
            self.language = request_language(payload, self.headers.get("Accept-Language"))
            payload = payload | {"language": self.language}
            if self.path == "/api/inspect":
                return self.send(200, inspect(payload.get("file"), payload.get("config")))
            if self.path == "/api/analyze":
                return self.send(200, analyze(payload))
            if self.path == "/api/export":
                return self.send(200, export_bundle(payload), "application/zip", "importguard-review.zip")
            return self.fail(404, "http.endpoint")
        except (InputError, ValueError, UnicodeDecodeError, KeyError, TypeError, OverflowError) as exc:
            self.send(400, error_payload(exc, self.language))
        except (TimeoutError, socket.timeout):
            self.fail(408, "http.timeout")


def main():
    parser = argparse.ArgumentParser(description="Start local ImportGuard. Uploaded data is never stored.")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en", help="Console message language")
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        parser.exit(2, format_message("cli.server_start_failed", {"detail": str(exc)}, args.language) + "\n")
    print(f"ImportGuard {VERSION}: http://127.0.0.1:{server.server_port}\n" + format_message("cli.server_ready", language=args.language), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n" + format_message("cli.server_stopped", language=args.language))
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
