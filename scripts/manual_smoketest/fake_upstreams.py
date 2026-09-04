"""claude-spillwayの実プロセス検証用に、AnthropicとOllama Cloudを模した
ダミーHTTPサーバーを2つ立てるスクリプト(検証専用、リポジトリには含めない)。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 呼び出す毎に utilization を切り替える(1回目=低負荷, 2回目以降=高負荷)
_anthropic_call_count = {"n": 0}
_UTILIZATIONS = [0.5, 0.95, 0.95, 0.95]


class AnthropicFakeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        idx = min(_anthropic_call_count["n"], len(_UTILIZATIONS) - 1)
        util = _UTILIZATIONS[idx]
        _anthropic_call_count["n"] += 1

        if self.path == "/v1/messages":
            body = json.dumps(
                {"id": "msg_from_anthropic", "content": [{"type": "text", "text": "hi from anthropic"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("anthropic-ratelimit-unified-5h-utilization", str(util))
            self.send_header("anthropic-ratelimit-unified-7d-utilization", "0.1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = json.dumps({"error": {"message": f"unsupported path {self.path}"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[fake-anthropic:{self.server.server_port}] " + (fmt % args))


class OllamaFakeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        data = json.loads(raw) if raw else {}
        auth = self.headers.get("Authorization", "")
        body = json.dumps(
            {
                "id": "msg_from_ollama",
                "model": data.get("model"),
                "auth_seen": auth,
                "content": [{"type": "text", "text": "hi from ollama"}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[fake-ollama:{self.server.server_port}] " + (fmt % args))


def main() -> None:
    anthropic_server = ThreadingHTTPServer(("127.0.0.1", 9101), AnthropicFakeHandler)
    ollama_server = ThreadingHTTPServer(("127.0.0.1", 9102), OllamaFakeHandler)

    t1 = threading.Thread(target=anthropic_server.serve_forever, daemon=True)
    t2 = threading.Thread(target=ollama_server.serve_forever, daemon=True)
    t1.start()
    t2.start()
    print("fake anthropic on :9101 / fake ollama on :9102 (Ctrl+C to stop)")
    t1.join()
    t2.join()


if __name__ == "__main__":
    main()
