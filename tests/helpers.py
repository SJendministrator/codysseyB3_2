"""테스트 도우미: 임시 Git 저장소 + 가짜 AI API 서버 (실제 키·비용 없이 전체 흐름 검증)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="gitgen-test-"))
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "test@example.com")
    git(d, "config", "user.name", "tester")
    (d / "app.py").write_text("def hello():\n    return 'hi'\n")
    git(d, "add", ".")
    git(d, "commit", "-q", "-m", "init")
    return d


def run_cli(cwd, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY") and not k.startswith("AI_")}
    env["NO_COLOR"] = "1"
    env.update(env_extra or {})
    p = subprocess.run(["python3", str(MAIN), *args], cwd=cwd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


class FakeAI:
    """OpenAI 호환 / Anthropic 형식 응답을 흉내 내는 로컬 서버."""

    def __init__(self, reply="feat: 테스트\n\n- 본문", status=200, style="openai"):
        self.reply, self.status, self.style = reply, status, style
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if outer.status != 200:
                    data = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode()
                elif outer.style == "anthropic":
                    data = json.dumps({"model": body["model"], "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": outer.reply}],
                                       "usage": {"input_tokens": 100, "output_tokens": 20}}).encode()
                else:
                    data = json.dumps({"model": body["model"],
                                       "choices": [{"message": {"content": outer.reply}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 100, "completion_tokens": 20}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
