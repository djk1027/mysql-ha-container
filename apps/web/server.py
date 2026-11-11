"""HTTP 서버 (표준 라이브러리만 사용).

  GET /                 대시보드
  GET /static/<file>    정적 파일 (화이트리스트)
  GET /api/cluster      최신 스냅샷
  GET /api/history      시계열 (최근 1시간)
  GET /api/events       이벤트 로그 (HA Manager)
  GET /api/stream       SSE 실시간 스트림
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/processlist.js": ("processlist.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
SSE_HEARTBEAT = 15  # 초. 프록시/브라우저가 연결을 끊지 않도록 주기적으로 신호

log = logging.getLogger("web")


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mysql-ha-dashboard"

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path in STATIC_FILES:
                return self._static(*STATIC_FILES[path])
            if path == "/api/cluster":
                return self._json(store.latest())
            if path == "/api/history":
                return self._json(store.history())
            if path == "/api/events":
                return self._json(store.events())
            if path == "/api/stream":
                return self._stream()

            self.send_error(404)

        # ---------- responses ----------
        def _static(self, name, content_type):
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data):
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            last_seq = 0
            try:
                while True:
                    snap = store.wait_for_update(last_seq, timeout=SSE_HEARTBEAT)
                    if snap is None:
                        self.wfile.write(b": heartbeat\n\n")
                    else:
                        last_seq = snap["seq"]
                        payload = {"snapshot": snap, "events": store.events()[-50:]}
                        data = json.dumps(payload, default=str)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # 브라우저 탭이 닫힘 → 정상 종료

        def log_message(self, fmt, *args):
            # SSE/폴링 요청으로 로그가 넘치지 않도록 debug 레벨로
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True          # Ctrl+C 시 SSE 연결 스레드가 종료를 막지 않게
    allow_reuse_address = True     # 재실행 시 "Address already in use" 방지


def serve(store, host, port):
    httpd = Server((host, port), make_handler(store))
    log.info("대시보드: http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
