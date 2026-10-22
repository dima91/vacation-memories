"""A small HTTP server that speaks enough of the Ollama API (`/api/tags`, `/api/ps`, `/api/chat`) for the tests."""

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from system_fakes import MODEL, MODEL_SIZE

VALID_ANSWER = {
    "description": "A sunny beach with a few umbrellas",
    "category": "sea",
    "moment_type": "scenery",
    "narrative_value": 3,
    "people": False,
}


@dataclass
class Reply:
    """What the server does with one chat request. A `dict` content is sent as JSON text, a `str` as it is."""

    content: object = None
    status: int = 200
    delay: float = 0.0


class FakeOllama:
    def __init__(self, *, model: str = MODEL, installed: bool = True, placement: str = "gpu", delay: float = 0.0):
        self.model = model
        self.installed = installed
        self.placement = placement
        self.delay = delay
        self.chat_requests: list[dict] = []
        self.unload_requests = 0
        self.script: deque[Reply] = deque()  # one-shot replies, used before `on_chat` and the default answer
        self.on_chat: Callable[[dict], Reply] | None = None
        self.loaded = False
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeOllama":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def images_sent(self) -> list[str]:
        return [body["messages"][0]["images"][0] for body in self.chat_requests]

    def _ps_entry(self) -> dict:
        in_vram = {"gpu": MODEL_SIZE, "mixed": MODEL_SIZE // 2, "cpu": 0}[self.placement]
        return {"name": self.model, "model": self.model, "size": MODEL_SIZE, "size_vram": in_vram}

    def _reply_to(self, body: dict) -> Reply:
        if self.script:
            return self.script.popleft()
        if self.on_chat:
            return self.on_chat(body)
        return Reply(VALID_ANSWER)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, payload: object) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:  # the client gave up (timeout)
                    pass

            def do_GET(self) -> None:
                if self.path == "/api/tags":
                    models = [{"name": fake.model, "model": fake.model, "size": MODEL_SIZE}] if fake.installed else []
                    self._send(200, {"models": models})
                elif self.path == "/api/ps":
                    self._send(200, {"models": [fake._ps_entry()] if fake.loaded else []})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/chat":
                    self._send(404, {"error": "not found"})
                elif body.get("keep_alive") == 0:
                    with fake._lock:
                        fake.unload_requests += 1
                        fake.loaded = False
                    self._send(200, {"model": fake.model, "done": True, "done_reason": "unload"})
                else:
                    self._chat(body)

            def _chat(self, body: dict) -> None:
                with fake._lock:
                    fake.chat_requests.append(body)
                    fake._in_flight += 1
                    fake.max_in_flight = max(fake.max_in_flight, fake._in_flight)
                    reply = fake._reply_to(body)
                try:
                    time.sleep(fake.delay + reply.delay)
                    fake.loaded = True
                    if reply.status != 200:
                        self._send(reply.status, {"error": "simulated failure"})
                        return
                    content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
                    self._send(200, {"model": fake.model, "message": {"role": "assistant", "content": content},
                                     "done": True})
                finally:
                    with fake._lock:
                        fake._in_flight -= 1

        return Handler
