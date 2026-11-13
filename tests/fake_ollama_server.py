"""The simulated Ollama as a service: the `ollama` service of `compose.test.yaml` runs `python tests/fake_ollama_server.py`.

It answers on port 11434 with the model named by `OLLAMA_MODEL`, so no model is downloaded.
"""

import os
import signal
import threading

from fake_ollama import FakeOllama
from system_fakes import MODEL

PORT = 11434


def main() -> int:
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    with FakeOllama(model=os.environ.get("OLLAMA_MODEL") or MODEL, host="0.0.0.0", port=PORT):
        stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
