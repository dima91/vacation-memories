"""Helpers of the editor's tests: a small synthetic vacation and an editor server on a free port."""

import http.client
import json
import threading
from pathlib import Path

from vacation_memories.core.config import Settings, resolve_settings
from vacation_memories.evaluate.synthetic_media import make_photo, populate_vacation

HOST = "127.0.0.1:8080"
JSON_HEADERS = {"Content-Type": "application/json", "Host": HOST, "Origin": f"http://{HOST}"}


def make_media(root: Path) -> Path:
    """`trip/` (two days of synthetic photos and one clip), `other/` (two photos) and an empty folder."""
    populate_vacation(root / "trip", photos=10, videos=1, days=2, video_scenes=(3.0, 3.0), size=(320, 240))
    (root / "other").mkdir()
    for number in range(2):
        make_photo(root / "other" / f"IMG_{number}.jpg", 300 + number, 320, 240, seed=1, mosaic=True)
    (root / "empty").mkdir()
    return root


def editor_settings(media: Path, work: Path, music: Path | None = None) -> Settings:
    options = {"input": media, "workdir": work, "no_ai": True}
    if music is not None:
        options["music_dir"] = music
    return resolve_settings(options, {})


class Client:
    """A bare HTTP client for the tests: every call returns `(status, headers, body)`, JSON decoded when it is."""

    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = dict(JSON_HEADERS if method != "GET" else {"Host": HOST})
        sent.update(headers or {})
        sent = {key: value for key, value in sent.items() if value is not None}  # None removes a default header
        raw = None
        if body is not None:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            connection.request(method, path, body=raw, headers=sent)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        content_type = response.getheader("Content-Type", "")
        if content_type.startswith("application/json") and data:
            data = json.loads(data)
        return response.status, {key.lower(): value for key, value in response.getheaders()}, data

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, body=None, headers=None):
        return self.request("POST", path, {} if body is None else body, headers)

    def put(self, path, body, headers=None):
        return self.request("PUT", path, body, headers)


class RunningEditor:
    def __init__(self, app):
        from vacation_memories.editor.server import make_server

        self.app = app
        self.server = make_server(app, "127.0.0.1", 0)
        self.client = Client(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningEditor":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self._thread.join(timeout=5)
