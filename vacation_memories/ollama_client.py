"""Minimal client for the Ollama `/api/chat` endpoint (standard library only).

Requests never go through a proxy: the server is on the compose internal network or on this machine.
"""

import json
import socket
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_KEEP_ALIVE = "30m"  # the model stays loaded for the duration of a job; the job unloads it at the end


class OllamaError(Exception):
    """The request failed: unreachable, timed out, error status or malformed answer."""


class OllamaClient:
    def __init__(
        self, url: str, model: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, keep_alive: str = DEFAULT_KEEP_ALIVE
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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

    def _post(self, path: str, body: dict) -> object:
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise OllamaError(f"Ollama answered with status {error.code}") from None
        except (TimeoutError, socket.timeout):
            raise OllamaError(f"the request timed out after {self.timeout:g} s") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise OllamaError(f"the request timed out after {self.timeout:g} s") from None
            raise OllamaError(f"cannot reach Ollama at {self.url}: {error.reason}") from None
        except (OSError, ValueError) as error:  # connection reset, truncated or non-JSON body
            raise OllamaError(f"invalid answer from Ollama: {error}") from None
