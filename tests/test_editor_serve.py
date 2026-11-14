"""The `serve` subcommand, started the way the container does: a process that answers and stops on SIGTERM."""

import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from editor_fakes import make_media

from vacation_memories import cli
from vacation_memories.core import exit_codes

REPO = Path(__file__).parents[1]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(port, path, host):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.fixture
def started(tmp_path):
    media = make_media(tmp_path / "media")
    processes = []

    def start(*extra, environment=None):
        port = free_port()
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(REPO), "LANG": "C",
               "TMPDIR": "/nonexistent", **(environment or {})}
        process = subprocess.Popen(
            [sys.executable, "-m", "vacation_memories", "serve", "--input", str(media), "--workdir",
             str(tmp_path / "work"), "--port", str(port), *extra],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        processes.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if get(port, "/api/health", "anything")[0] == 200:
                    return process, port
            except OSError:
                time.sleep(0.1)
        raise AssertionError(f"the editor did not start: {process.stdout.read().decode() if process.poll() else ''}")

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def test_the_editor_needs_a_media_root(tmp_path, capsys, isolate_process_state):
    assert cli.main(["serve", "--workdir", str(tmp_path / "work")], environ={}) == exit_codes.USAGE
    assert "--input is required" in capsys.readouterr().err
    assert cli.main(["serve", "--workdir", str(tmp_path / "work"), "--input", str(tmp_path / "missing")],
                    environ={}) == exit_codes.USAGE


def test_it_serves_without_a_display_and_stops_in_an_orderly_way_on_sigterm(started):
    process, port = started()
    assert get(port, "/api/folders", "127.0.0.1:8080")[0] == 200
    started_at = time.monotonic()
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=10) == 143
    assert time.monotonic() - started_at < 5


def test_sigint_stops_it_too(started):
    process, _ = started()
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=10) == 130


def test_the_allowed_hosts_come_from_the_option(started):
    _, port = started("--allowed-host", "127.0.0.1:9191")
    assert get(port, "/api/folders", "127.0.0.1:9191")[0] == 200
    assert get(port, "/api/folders", "127.0.0.1:8080")[0] == 403


def test_the_allowed_hosts_come_from_the_environment_when_there_is_no_option(started):
    _, port = started(environment={"VACATION_MEMORIES_EDITOR_HOSTS": "127.0.0.1:9292, localhost:9292"})
    assert get(port, "/api/folders", "localhost:9292")[0] == 200
    assert get(port, "/api/folders", "127.0.0.1:8080")[0] == 403


def test_by_default_it_accepts_the_published_address(started):
    _, port = started()
    assert get(port, "/api/folders", "127.0.0.1:8080")[0] == 200 and get(port, "/api/folders", "localhost:8080")[0] == 200


def test_it_works_with_ollama_unreachable_and_never_contacts_it(started, tmp_path):
    """The editor starts and serves the page with Ollama stopped (it checks it only for an analysis with AI)."""
    _, port = started(environment={"OLLAMA_HOST": "http://127.0.0.1:9"})
    status, body = get(port, "/", "127.0.0.1:8080")
    assert status == 200 and b"Vacation Memories" in body
    assert get(port, "/api/ai", "127.0.0.1:8080")[0] == 200  # it answers: not available
