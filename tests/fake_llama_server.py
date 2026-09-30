"""Stand-in for llama-server used by the runtime tests (speaks the same HTTP API)."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    args = sys.argv[1:]
    if "--fail-gpu" in open(args[args.index("-m") + 1], "rb").read(64).decode("latin1", "ignore") and "-ngl" in args \
            and args[args.index("-ngl") + 1] != "0":
        print("ggml_vulkan: out of device memory", flush=True)
        sys.exit(3)
    port = int(args[args.index("--port") + 1])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200 if self.path in ("/health", "/v1/models") else 404)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema")
            content = json.dumps({"ok": True, "word": "hello"}) if schema else "hello"
            reply = {"choices": [{"message": {"role": "assistant", "content": content}}], "ngl": args[args.index("-ngl") + 1]}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
