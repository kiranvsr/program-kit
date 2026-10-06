"""tiny-service: GET /health and POST /echo over HTTP, standard library only."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8080
MAX_BODY = 1024  # bytes; a larger request body is refused


def handle(method, path, body):
    """Turn one request into (status, JSON-able payload). `body` is at most MAX_BODY + 1 bytes."""
    if path == "/health":
        if method != "GET":
            return 405, {"error": "method not allowed"}
        return 200, {"status": "ok"}
    if path == "/echo":
        if method != "POST":
            return 405, {"error": "method not allowed"}
        if len(body) > MAX_BODY:
            return 413, {"error": "body too large"}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"error": "body is not valid JSON"}
        return 200, {"echo": value}
    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    """Reads at most MAX_BODY + 1 bytes of the body, asks `handle`, writes the JSON answer."""

    def _answer(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            status, payload = 400, {"error": "bad Content-Length"}
        else:
            status, payload = handle(self.command, self.path, self.rfile.read(min(length, MAX_BODY + 1)))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def log_message(self, format, *args):
        pass  # no access log yet: that is the next task


def make_server(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve /health and /echo.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
