"""Minimal client for the Ollama `/api/chat` endpoint (standard library only).

Requests never go through a proxy: the server is on the compose internal network or on this machine.
`abort` cuts off the requests in progress, so a shutdown signal does not wait for the model.
"""

import http.client
import json
import socket
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_KEEP_ALIVE = "30m"  # the model stays loaded for the duration of a job; the job unloads it at the end


class OllamaError(Exception):
    """The request failed: unreachable, timed out, error status or malformed answer."""


class OllamaAborted(OllamaError):
    """The job is stopping: the request was cut off, and no new one is sent."""


class OllamaClient:
    def __init__(
        self, url: str, model: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_alive: str = DEFAULT_KEEP_ALIVE
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.aborted = False
        self._connections: set[http.client.HTTPConnection] = set()

    def chat(self, *, prompt: str, image_b64: str, schema: dict, num_predict: int) -> str:
        """Ask about one image and return the text of the answer, constrained by `schema`."""
        answer = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
                "stream": False,
                "format": schema,
                "options": {"temperature": 0, "num_predict": num_predict},
                "keep_alive": self.keep_alive,
            },
        )
        content = answer.get("message", {}).get("content") if isinstance(answer, dict) else None
        if not isinstance(content, str):
            raise OllamaError("the answer has no message content")
        return content

    def unload(self) -> None:
        """Ask Ollama to free the model's memory."""
        self._post("/api/chat", {"model": self.model, "keep_alive": 0})

    def abort(self) -> None:
        """Cut off the requests in progress and refuse new ones. Safe from a signal handler and other threads."""
        self.aborted = True
        for connection in list(self._connections):
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _post(self, path: str, body: dict) -> object:
        if self.aborted:
            raise OllamaAborted("the job is stopping")
        parts = urlsplit(self.url)
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = factory(parts.hostname, parts.port, timeout=self.timeout)
        self._connections.add(connection)
        try:
            connection.request(
                "POST", parts.path + path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            data = response.read()
        except (TimeoutError, socket.timeout):
            raise OllamaError(f"the request timed out after {self.timeout:g} s") from None
        except (OSError, http.client.HTTPException) as error:  # refused, reset, cut off by `abort`
            if self.aborted:
                raise OllamaAborted("the job is stopping") from None
            raise OllamaError(f"cannot reach Ollama at {self.url}: {error}") from None
        finally:
            self._connections.discard(connection)
            connection.close()
        if response.status != 200:
            raise OllamaError(f"Ollama answered with status {response.status}")
        try:
            return json.loads(data)
        except ValueError as error:  # truncated or non-JSON body
            raise OllamaError(f"invalid answer from Ollama: {error}") from None
