import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PATH", "/tmp/bot.sqlite3")

import bot  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._send(200, {"ok": True, "service": "tg-forum-shox"})

    def do_POST(self) -> None:
        secret = os.environ.get("WEBHOOK_SECRET", "")
        if secret:
            actual = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if actual != secret:
                self._send(401, {"ok": False})
                return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        try:
            update = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "bad json"})
            return

        try:
            if "callback_query" in update:
                bot.handle_callback(update)
            elif "message" in update:
                bot.handle_message(update)
        except Exception as exc:
            print(f"Webhook error: {exc}", file=sys.stderr)
            self._send(200, {"ok": True, "handled": False})
            return

        self._send(200, {"ok": True})
