import logging
import os
import tempfile

import pytest
from fake_ollama import FakeOllama


@pytest.fixture
def fake_ollama():
    with FakeOllama() as server:
        yield server


@pytest.fixture
def isolate_process_state(monkeypatch):
    """`cli.main` changes the umask, TMPDIR, logging handlers and the priority of the process."""
    previous_umask = os.umask(0)
    os.umask(previous_umask)
    monkeypatch.setenv("TMPDIR", tempfile.gettempdir())
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    nice_calls = []
    monkeypatch.setattr(os, "nice", lambda increment: nice_calls.append(increment) or increment)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield nice_calls
    os.umask(previous_umask)
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
