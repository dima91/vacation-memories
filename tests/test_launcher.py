"""The single-file launcher (U8): no Docker and no browser are needed, every command goes through a fake."""
import http.client
import importlib.util
import json
import logging
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "launcher.py"
spec = importlib.util.spec_from_file_location("vm_launcher", SCRIPT)
launcher_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher_module  # `dataclass` looks the module up while the class is created
spec.loader.exec_module(launcher_module)
L = launcher_module

IMAGE = "example/vacation-memories"


class FakeProcess:
    def __init__(self, lines, code):
        self.stdout = iter(lines)
        self._code = code
        self.terminated = False

    def wait(self, timeout=None):
        return self._code

    def terminate(self):
        self.terminated = True


class FakePopen:
    """Records the commands; `respond(command)` returns `(code, lines)` for each one."""

    def __init__(self, respond=None):
        self.commands = []
        self._respond = respond or (lambda command: (0, []))

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        outcome = self._respond(command)
        if outcome is FileNotFoundError:
            raise FileNotFoundError(command[0])
        code, lines = outcome
        return FakeProcess([line + "\n" for line in lines], code)

    def compose(self):
        return [command for command in self.commands if command[:2] == ["docker", "compose"]
                and command[2:4] != ["version"]]

    def verbs(self):
        """The compose commands without the global options: ("pull", "editor"), ("up", "-d", ...)."""
        verbs = []
        for command in self.compose():
            rest = command[2:]
            while rest and rest[0] in ("--project-name", "--project-directory", "--env-file", "-f"):
                rest = rest[2:]
            verbs.append(tuple(rest))
        return verbs


class HealthyAfter:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    def __call__(self, url, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise urllib.error.URLError("not yet")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "payload"
    for name in L.PAYLOAD_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {name}\n")
    (root / L.DIST_CONFIG).write_text(json.dumps({"image": IMAGE, "version": "1.2.3"}))
    return root


@pytest.fixture
def layout(tmp_path):
    return L.Layout(tmp_path / "config", tmp_path / "data", "1.2.3")


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media"
    folder.mkdir()
    return folder


def good_answers(tmp_path, media):
    return {"media_dir": str(media), "work_dir": str(tmp_path / "work"), "music_dir": "", "assume_tz": "Europe/Rome"}


def make_launcher(layout, payload, tmp_path, popen=None, urlopen=None, dist=None):
    popen = popen or FakePopen()
    launcher = L.Launcher(
        layout, dist or L.load_dist_config(payload), payload, runner=L.Runner(popen=popen),
        urlopen=urlopen or HealthyAfter(), sleep=lambda seconds: None, uid=1000, gid=1001,
        environ={"HOME": str(tmp_path / "home")},
    )
    return launcher, popen


def finish(launcher):
    for thread in list(launcher._threads):
        thread.join(timeout=10)
        assert not thread.is_alive()


# --- files and answers ---


def test_layout_follows_xdg_and_versioned_data_dir():
    layout = L.default_layout("1.0/β", {"HOME": "/home/u", "XDG_CONFIG_HOME": "/cfg"})
    assert layout.config_dir == Path("/cfg/vacation-memories")
    assert layout.data_root == Path("/home/u/.local/share/vacation-memories")
    assert layout.data_dir.name == "1.0__"
    assert layout.env_file.parent == layout.config_dir


def test_dist_config_defaults_to_a_local_build(tmp_path):
    dist = L.load_dist_config(tmp_path)
    assert dist.image is None and dist.runtime_image is None and dist.version == "dev"


def test_dist_config_gives_the_runtime_image(payload):
    dist = L.load_dist_config(payload)
    assert dist.runtime_image == f"{IMAGE}:1.2.3"


def test_extract_payload_copies_once_and_only_changes(payload, layout):
    changed = L.extract_payload(payload, layout)
    assert changed == list(L.PAYLOAD_FILES)
    assert (layout.data_dir / "docker" / "nginx.conf").read_text() == "# docker/nginx.conf\n"
    assert L.extract_payload(payload, layout) == []
    (payload / "compose.yaml").write_text("# new\n")
    assert L.extract_payload(payload, layout) == ["compose.yaml"]


def test_extract_payload_reports_a_missing_file(payload, layout):
    (payload / "docker" / "compose.toml").unlink()
    with pytest.raises(L.StartError, match="compose.toml"):
        L.extract_payload(payload, layout)


def test_validate_accepts_good_answers(tmp_path, media, layout):
    answers, errors = L.validate_answers(good_answers(tmp_path, media), layout)
    assert errors == {}
    assert answers.media_dir == str(media) and answers.music_dir == ""


@pytest.mark.parametrize("change, field", [
    ({"media_dir": ""}, "media_dir"),
    ({"media_dir": "relative/photos"}, "media_dir"),
    ({"media_dir": "/does/not/exist"}, "media_dir"),
    ({"media_dir": "/tmp/it's"}, "media_dir"),
    ({"work_dir": ""}, "work_dir"),
    ({"music_dir": "/does/not/exist"}, "music_dir"),
    ({"assume_tz": "Europe Rome"}, "assume_tz"),
    ({"ollama_model": "bad model"}, "ollama_model"),
])
def test_validate_reports_the_field(tmp_path, media, layout, change, field):
    raw = {**good_answers(tmp_path, media), **change}
    _, errors = L.validate_answers(raw, layout)
    assert field in errors


def test_validate_rejects_a_file_as_folder_and_an_uncreatable_work_dir(tmp_path, media, layout):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    _, errors = L.validate_answers({**good_answers(tmp_path, media), "media_dir": str(a_file)}, layout)
    assert errors["media_dir"] == "This is not a folder."
    _, errors = L.validate_answers({**good_answers(tmp_path, media), "work_dir": str(a_file / "child")}, layout)
    assert "work_dir" in errors


def test_env_file_has_the_variables_compose_needs(tmp_path, media, layout, payload):
    answers, _ = L.validate_answers(good_answers(tmp_path, media), layout)
    env = L.build_env(answers, layout, L.load_dist_config(payload), 1000, 1001)
    assert env["MEDIA_DIR"] == str(media)
    assert env["MUSIC_DIR"] == str(layout.default_music_dir)
    assert env["HOST_UID"] == "1000" and env["HOST_GID"] == "1001"
    assert env["VM_IMAGE"] == f"{IMAGE}:1.2.3" and env["APP_VERSION"] == "1.2.3"
    assert env["ASSUME_TZ"] == "Europe/Rome" and "OLLAMA_MODEL" not in env
    assert f"MEDIA_DIR='{media}'\n" in L.render_env(env)


def test_compose_files_follow_the_detected_hardware():
    assert L.compose_files(L.Answers()) == ["compose.yaml"]
    assert L.compose_files(L.Answers(intel_gpu=True, selinux=True)) == [
        "compose.yaml", "compose.intel-gpu.yaml", "compose.selinux.yaml"]


def test_answers_are_saved_and_reloaded(tmp_path, media, layout):
    answers, _ = L.validate_answers(good_answers(tmp_path, media), layout)
    L.save_answers(layout, answers)
    assert L.load_answers(layout)["media_dir"] == str(media)
    assert oct(layout.answers_file.stat().st_mode & 0o777) == "0o600"
    layout.answers_file.write_text("not json")
    assert L.load_answers(layout) is None


# --- docker ---


def test_compose_command_carries_project_directory_env_and_files(layout):
    compose = L.Compose(L.Runner(popen=FakePopen()), layout, ["compose.yaml", "compose.intel-gpu.yaml"])
    command = compose.command("stop")
    assert command[:2] == ["docker", "compose"]
    assert command[command.index("--project-name") + 1] == L.PROJECT_NAME
    assert command[command.index("--project-directory") + 1] == str(layout.data_dir)
    assert command[command.index("--env-file") + 1] == str(layout.env_file)
    assert [command[i + 1] for i, part in enumerate(command) if part == "-f"] == [
        str(layout.data_dir / "compose.yaml"), str(layout.data_dir / "compose.intel-gpu.yaml")]
    assert command[-1] == "stop"


@pytest.mark.parametrize("responses, expected", [
    ({("docker", "compose", "version"): FileNotFoundError}, "not installed"),
    ({("docker", "compose", "version"): (1, ["unknown command"])}, "Compose plugin"),
    ({("docker", "info"): (1, ["permission denied while trying to connect"])}, "docker' group"),
    ({("docker", "info"): (1, ["Cannot connect to the Docker daemon"])}, "not running"),
])
def test_check_docker_messages(responses, expected):
    def respond(command):
        return responses.get(tuple(command[:3]) if command[1] == "compose" else tuple(command[:2]), (0, []))
    assert expected in L.check_docker(L.Runner(popen=FakePopen(respond)))


def test_check_docker_passes():
    assert L.check_docker(L.Runner(popen=FakePopen())) is None


def test_runner_cancel_terminates_running_children():
    runner = L.Runner(popen=FakePopen())
    process = FakeProcess([], 0)
    runner._children.append(process)
    runner.cancel()
    assert process.terminated


# --- the start sequence ---


def ready_launcher(layout, payload, tmp_path, media, popen=None, urlopen=None, dist=None):
    launcher, popen = make_launcher(layout, payload, tmp_path, popen, urlopen, dist)
    errors = launcher.submit_setup(good_answers(tmp_path, media))
    assert errors == {}
    finish(launcher)
    return launcher, popen


def test_first_start_pulls_then_starts_then_runs_ollama(layout, payload, tmp_path, media):
    popen = FakePopen(lambda command: (1, []) if command[:3] == ["docker", "image", "inspect"] else (0, []))
    launcher, popen = ready_launcher(layout, payload, tmp_path, media, popen)
    verbs = popen.verbs()
    assert verbs[0] == ("pull", "editor")
    up = verbs[1]
    assert up[:4] == ("up", "-d", "--no-build", "--wait") and up[-2:] == ("editor", "nginx")
    assert ("up", "-d", "ollama", "ollama-pull") in verbs
    assert launcher.session.snapshot()["phase"] == "ready"
    assert launcher.stack_started
    assert (Path(launcher.answers.work_dir)).is_dir() and layout.default_music_dir.is_dir()
    assert "VM_IMAGE='example/vacation-memories:1.2.3'" in layout.env_file.read_text()


def test_next_start_skips_the_pull_when_the_image_is_present(layout, payload, tmp_path, media):
    launcher, popen = ready_launcher(layout, payload, tmp_path, media)
    assert not [verb for verb in popen.verbs() if verb[0] == "pull"]
    assert launcher.session.snapshot()["phase"] == "ready"


def test_a_local_build_never_pulls(layout, payload, tmp_path, media):
    launcher, popen = ready_launcher(layout, payload, tmp_path, media, dist=L.DistConfig(None, "dev"))
    assert not [command for command in popen.commands if command[:3] == ["docker", "image", "inspect"]]
    assert not [verb for verb in popen.verbs() if verb[0] == "pull"]


def test_a_failed_pull_is_an_error_with_a_retry(layout, payload, tmp_path, media):
    def respond(command):
        if command[:3] == ["docker", "image", "inspect"]:
            return 1, []
        return (1, ["denied"]) if "pull" in command else (0, [])
    launcher, popen = ready_launcher(layout, payload, tmp_path, media, FakePopen(respond))
    state = launcher.session.snapshot()
    assert state["phase"] == "error" and "could not be downloaded" in state["error"]
    assert state["lines"] == ["denied"]
    assert not launcher.stack_started
    popen._respond = lambda command: (0, [])
    launcher.retry()
    finish(launcher)
    assert launcher.session.snapshot()["phase"] == "ready"


def test_a_failed_up_still_stops_the_stack_on_close(layout, payload, tmp_path, media):
    popen = FakePopen(lambda command: (1, ["boom"]) if "--wait" in command else (0, []))
    launcher, popen = ready_launcher(layout, payload, tmp_path, media, popen)
    assert launcher.session.snapshot()["phase"] == "error"
    launcher.shutdown()
    assert popen.verbs()[-1] == ("stop",)


def test_an_ollama_failure_is_not_fatal(layout, payload, tmp_path, media):
    popen = FakePopen(lambda command: (1, ["no gpu"]) if "ollama-pull" in command else (0, []))
    launcher, _ = ready_launcher(layout, payload, tmp_path, media, popen)
    assert launcher.session.snapshot()["phase"] == "ready"


def test_the_editor_must_answer_its_health_check(layout, payload, tmp_path, media):
    clock = iter(range(0, 1000, 10))
    launcher, popen = make_launcher(layout, payload, tmp_path, urlopen=HealthyAfter(failures=10 ** 6))
    launcher._clock = lambda: next(clock)
    launcher.submit_setup(good_answers(tmp_path, media))
    finish(launcher)
    state = launcher.session.snapshot()
    assert state["phase"] == "error" and "does not answer" in state["error"]


def test_the_health_check_retries_until_the_editor_answers(layout, payload, tmp_path, media):
    urlopen = HealthyAfter(failures=3)
    launcher, _ = ready_launcher(layout, payload, tmp_path, media, urlopen=urlopen)
    assert launcher.session.snapshot()["phase"] == "ready" and urlopen.calls == 4


def test_missing_docker_is_reported_before_anything_else(layout, payload, tmp_path, media):
    popen = FakePopen(lambda command: FileNotFoundError)
    launcher, popen = ready_launcher(layout, payload, tmp_path, media, popen)
    assert "not installed" in launcher.session.snapshot()["error"]
    assert not layout.data_dir.exists()


def test_shutdown_stops_only_what_was_started(layout, payload, tmp_path, media):
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher.shutdown()
    assert popen.verbs() == []
    started, popen = ready_launcher(layout, payload, tmp_path, media)
    started.shutdown()
    assert popen.verbs()[-1] == ("stop",)


def test_a_close_during_the_start_cancels_it(layout, payload, tmp_path, media):
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher._stopping.set()
    launcher.submit_setup(good_answers(tmp_path, media))
    finish(launcher)
    assert launcher.session.snapshot()["phase"] != "ready"
    assert not launcher.stack_started


# --- first start, saved answers ---


def test_without_saved_answers_the_setup_is_shown(layout, payload, tmp_path):
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher.begin(reconfigure=False)
    assert launcher.session.snapshot()["phase"] == "setup"
    assert popen.commands == []


def test_saved_answers_start_the_stack_directly(layout, payload, tmp_path, media):
    first, _ = ready_launcher(layout, payload, tmp_path, media)
    second, popen = make_launcher(layout, payload, tmp_path)
    second.begin(reconfigure=False)
    finish(second)
    assert second.session.snapshot()["phase"] == "ready"
    assert popen.verbs()


def test_reconfigure_shows_the_saved_answers_again(layout, payload, tmp_path, media):
    ready_launcher(layout, payload, tmp_path, media)
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher.begin(reconfigure=True)
    state = launcher.session.snapshot()
    assert state["phase"] == "setup" and state["errors"] == {}
    assert launcher.answers.media_dir == str(media)
    assert popen.commands == []


def test_a_saved_folder_that_is_gone_asks_again(layout, payload, tmp_path, media):
    ready_launcher(layout, payload, tmp_path, media)
    media.rmdir()
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher.begin(reconfigure=False)
    state = launcher.session.snapshot()
    assert state["phase"] == "setup" and "media_dir" in state["errors"]
    assert popen.commands == []


# --- the page's server ---


@pytest.fixture
def served(layout, payload, tmp_path):
    launcher, popen = make_launcher(layout, payload, tmp_path)
    launcher.begin(reconfigure=False)
    server = L.BootstrapServer(launcher)
    server.start()
    yield launcher, server, popen
    server.stop()
    finish(launcher)


def request(server, method, path, body=None, headers=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    base = {"Host": host or f"127.0.0.1:{server.port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        base.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.port}"})
    base.update(headers or {})
    connection.request(method, path, body=payload, headers=base)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.headers, data


def token_header(server):
    return {"X-Launcher-Token": server.token}


def test_the_page_carries_the_token_and_a_strict_policy(served):
    _, server, _ = served
    status, headers, body = request(server, "GET", "/")
    assert status == 200
    assert server.token.encode() in body
    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy
    nonce = policy.split("script-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"'.encode() in body
    assert b"http://" not in body.replace(b"http://127.0.0.1", b"")  # no external resource


def test_the_state_needs_the_token(served):
    _, server, _ = served
    assert request(server, "GET", "/api/state")[0] == 403
    status, _, body = request(server, "GET", "/api/state", headers=token_header(server))
    state = json.loads(body)
    assert status == 200 and state["phase"] == "setup" and state["editor_url"] == L.EDITOR_URL
    assert "media_dir" in state["answers"]


def test_a_foreign_host_is_refused_for_every_request(served):
    _, server, _ = served
    assert request(server, "GET", "/", host="evil.example:80")[0] == 403
    assert request(server, "GET", "/api/state", headers=token_header(server), host="evil.example")[0] == 403


def test_post_needs_the_right_origin_token_and_content_type(served):
    _, server, _ = served
    good = {**token_header(server)}
    assert request(server, "POST", "/api/setup", {}, headers={**good, "Origin": "http://evil.example"})[0] == 403
    assert request(server, "POST", "/api/setup", {}, headers={"X-Launcher-Token": "wrong"})[0] == 403
    assert request(server, "POST", "/api/setup", {}, headers={**good, "Content-Type": "text/plain"})[0] == 415
    assert request(server, "POST", "/api/nowhere", {}, headers=good)[0] == 404


def test_invalid_answers_come_back_as_field_errors(served):
    launcher, server, popen = served
    status, _, body = request(server, "POST", "/api/setup", {"media_dir": "/does/not/exist"},
                              headers=token_header(server))
    assert status == 400
    assert "media_dir" in json.loads(body)["errors"]
    assert launcher.session.snapshot()["phase"] == "setup" and popen.commands == []


def test_valid_answers_are_saved_and_start_the_stack(served, tmp_path, media, layout):
    launcher, server, popen = served
    status, _, _ = request(server, "POST", "/api/setup", good_answers(tmp_path, media), headers=token_header(server))
    assert status == 200
    finish(launcher)
    assert launcher.session.snapshot()["phase"] == "ready"
    assert L.load_answers(layout)["media_dir"] == str(media)
    status, _, _ = request(server, "POST", "/api/setup", good_answers(tmp_path, media), headers=token_header(server))
    assert status == 409


def test_retry_and_reconfigure_follow_the_phase(served, tmp_path, media):
    launcher, server, popen = served
    request(server, "POST", "/api/setup", good_answers(tmp_path, media), headers=token_header(server))
    finish(launcher)
    request(server, "POST", "/api/reconfigure", {}, headers=token_header(server))
    assert launcher.session.snapshot()["phase"] == "ready"  # only from setup or error
    launcher.session.update("error", error="x")
    request(server, "POST", "/api/reconfigure", {}, headers=token_header(server))
    assert launcher.session.snapshot()["phase"] == "setup"


# --- window, notifications, desktop entry ---


def test_find_browser_prefers_google_chrome():
    found = {"google-chrome": "/usr/bin/google-chrome", "google-chrome-stable": "/usr/bin/gcs"}
    assert L.find_browser(None, which=found.get) == "/usr/bin/google-chrome"
    assert L.find_browser(None, which={"google-chrome-stable": "/usr/bin/gcs"}.get) == "/usr/bin/gcs"
    assert L.find_browser("custom", which={"custom": "/opt/custom"}.get) == "/opt/custom"
    assert L.find_browser(None, which=lambda name: None) is None


def test_browser_command_is_an_app_window_with_its_own_profile(tmp_path):
    command = L.browser_command("/usr/bin/google-chrome", "http://127.0.0.1:5000/", tmp_path / "profile")
    assert command[0] == "/usr/bin/google-chrome"
    assert "--app=http://127.0.0.1:5000/" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert "--no-first-run" in command


def test_notify_uses_notify_send_only_when_present():
    popen = FakePopen()
    L.notify("hello", which=lambda name: None, popen=popen)
    assert popen.commands == []
    L.notify("hello", which=lambda name: "/usr/bin/notify-send", popen=popen)
    assert popen.commands == [["/usr/bin/notify-send", L.WINDOW_TITLE, "hello"]]


def test_desktop_entry_quotes_the_executable_and_adds_the_reconfigure_action():
    entry = L.desktop_entry(["/home/u/My Apps/vacation-memories"])
    assert 'Exec="/home/u/My Apps/vacation-memories"\n' in entry
    assert 'Exec="/home/u/My Apps/vacation-memories" --reconfigure\n' in entry
    assert "Terminal=false" in entry and "Actions=reconfigure;stop;" in entry
    assert 'Exec="/home/u/My Apps/vacation-memories" --stop\n' in entry


def test_install_desktop_writes_the_entry(tmp_path):
    target = L.install_desktop(tmp_path / "applications", ["/opt/vm"])
    assert target.name == "vacation-memories.desktop" and 'Exec="/opt/vm"' in target.read_text()


def test_wait_for_window_closes_the_window_on_a_signal():
    class Window:
        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0

    window, stop = Window(), threading.Event()
    stop.set()
    L.wait_for_window(window, stop, poll=0.01)
    assert not window.alive


def test_wait_for_window_returns_when_the_window_closes():
    class Closed:
        def poll(self):
            return 0

    L.wait_for_window(Closed(), threading.Event(), poll=0.01)


def test_single_instance_lock(tmp_path):
    lock = tmp_path / "launcher.lock"
    with L.single_instance(lock) as first:
        assert first
        with L.single_instance(lock) as second:
            assert not second
    with L.single_instance(lock) as again:
        assert again


# --- the command line ---


@pytest.fixture
def keep_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_version_and_self_check_from_the_repository(capsys):
    assert L.main(["--version"], environ={}) == 0
    assert capsys.readouterr().out.strip() == "dev"
    assert L.main(["--self-check"], environ={}) == 0
    assert "payload at" in capsys.readouterr().out


def test_self_check_fails_when_a_file_is_missing(payload, capsys):
    (payload / "compose.selinux.yaml").unlink()
    assert L.self_check(payload) == 1
    assert "compose.selinux.yaml" in capsys.readouterr().err


def test_install_desktop_from_the_command_line(tmp_path, capsys):
    environ = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "share")}
    assert L.main(["--install-desktop"], environ=environ) == 0
    assert (tmp_path / "share" / "applications" / "vacation-memories.desktop").is_file()


def test_main_reports_a_browser_that_was_asked_for_and_is_missing(tmp_path, capsys, keep_logging):
    environ = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert L.main(["--browser", "no-such-browser-xyz"], environ=environ) == 1
    assert "no-such-browser-xyz was not found" in capsys.readouterr().err


def test_main_without_chrome_and_without_xdg_open_fails_clearly(tmp_path, capsys, keep_logging, monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    environ = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert L.main([], environ=environ) == 1
    assert "xdg-open is not available" in capsys.readouterr().err


def test_main_without_chrome_falls_back_to_xdg_open(tmp_path, keep_logging, monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None)
    calls = {}

    def fake_run_window(launcher, browser, reconfigure, opener=None):
        calls.update(browser=browser, opener=opener, reconfigure=reconfigure)
        return 0

    monkeypatch.setattr(L, "run_window", fake_run_window)
    environ = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert L.main(["--reconfigure"], environ=environ) == 0
    assert calls == {"browser": None, "opener": "/usr/bin/xdg-open", "reconfigure": True}


def test_main_prefers_chrome_over_xdg_open(tmp_path, keep_logging, monkeypatch):
    found = {"google-chrome": "/usr/bin/google-chrome", "xdg-open": "/usr/bin/xdg-open"}
    monkeypatch.setattr(L.shutil, "which", found.get)
    calls = {}
    monkeypatch.setattr(L, "run_window", lambda launcher, browser, reconfigure, opener=None:
                        calls.update(browser=browser, opener=opener) or 0)
    environ = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert L.main([], environ=environ) == 0
    assert calls == {"browser": "/usr/bin/google-chrome", "opener": None}


class FakeWindowLauncher:
    """Enough of a launcher for `run_window`: a layout, `begin` and `shutdown`."""

    def __init__(self, layout):
        self.layout = layout
        self.session = L.Session()
        self.answers = L.Answers()
        self.events = []

    def begin(self, reconfigure):
        self.events.append(("begin", reconfigure))

    def shutdown(self):
        self.events.append(("shutdown",))


def test_the_fallback_waits_for_a_stop_signal_then_shuts_down(layout, monkeypatch):
    opened, notices, handlers = [], [], {}

    class Opener:
        def __init__(self, command, **kwargs):
            opened.append(command)

    monkeypatch.setattr(L.subprocess, "Popen", Opener)
    monkeypatch.setattr(L, "notify", lambda message, **kwargs: notices.append(message))
    # `signal.signal` only works in the main thread: record the handlers instead of installing them
    monkeypatch.setattr(L.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    launcher = FakeWindowLauncher(layout)
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(code=L.run_window(launcher, None, False, "/usr/bin/xdg-open")))
    thread.start()
    for _ in range(200):
        if opened:
            break
        threading.Event().wait(0.02)
    assert opened and thread.is_alive(), "without a window to watch the launcher must keep running"
    handlers[L.signal.SIGTERM](L.signal.SIGTERM, None)
    thread.join(timeout=10)
    assert not thread.is_alive() and result["code"] == 0
    assert opened[0][0] == "/usr/bin/xdg-open" and opened[0][1].startswith("http://127.0.0.1:")
    assert launcher.events == [("begin", False), ("shutdown",)]
    assert any("Stop Vacation Memories" in notice for notice in notices)


# --- --stop ---


def test_the_lock_file_holds_the_pid_of_the_running_launcher(tmp_path):
    lock = tmp_path / "launcher.lock"
    with L.single_instance(lock):
        assert int(lock.read_text()) == L.os.getpid()
        with L.single_instance(lock) as second:
            assert not second
        assert int(lock.read_text()) == L.os.getpid()  # the failed attempt did not wipe it


def test_stop_with_nothing_running_and_nothing_saved(layout, payload, capsys):
    assert L.stop_running(layout, L.load_dist_config(payload), payload) == 0
    assert "Nothing to stop" in capsys.readouterr().out


def test_stop_with_nothing_running_stops_what_an_earlier_run_left_up(layout, payload, tmp_path, media, capsys):
    ready_launcher(layout, payload, tmp_path, media)
    popen = FakePopen()
    assert L.stop_running(layout, L.load_dist_config(payload), payload, runner=L.Runner(popen=popen)) == 0
    assert popen.verbs() == [("stop",)]
    assert "Stopped" in capsys.readouterr().out


def test_stop_signals_the_running_launcher_and_reports_when_it_does_not_exit(layout, payload, capsys):
    sent = []
    with L.single_instance(layout.lock_file) as running:
        assert running
        code = L.stop_running(layout, L.load_dist_config(payload), payload, kill=lambda pid, number: sent.append(
            (pid, number)), sleep=lambda seconds: None, clock=iter(range(0, 100, 20)).__next__, timeout=50)
    assert sent == [(L.os.getpid(), L.signal.SIGTERM)]
    assert code == 1 and "did not stop in time" in capsys.readouterr().err


def test_stop_returns_once_the_running_launcher_has_released_the_lock(layout, payload, capsys):
    holder = L.single_instance(layout.lock_file)
    assert holder.__enter__() is True

    def kill(pid, number):  # the launcher receives the signal and exits
        holder.__exit__(None, None, None)

    code = L.stop_running(layout, L.load_dist_config(payload), payload, kill=kill, sleep=lambda seconds: None,
                          timeout=5)
    assert code == 0 and "Stopped" in capsys.readouterr().out


def test_stop_reports_an_unreadable_pid(layout, payload, capsys):
    with L.single_instance(layout.lock_file):
        layout.lock_file.write_text("not a pid")
        assert L.stop_running(layout, L.load_dist_config(payload), payload, kill=lambda *a: None) == 1
    assert "could not be reached" in capsys.readouterr().err


# --- --uninstall ---


def installed(layout, payload, tmp_path, media):
    """A machine where the launcher ran once: saved answers, extracted files, a menu entry and work data."""
    ready_launcher(layout, payload, tmp_path, media)
    layout.desktop_file.parent.mkdir(parents=True)
    layout.desktop_file.write_text("[Desktop Entry]\n")
    layout.profile_dir.mkdir()
    work = tmp_path / "work"
    (work / "projects" / "paris-1a2b3c4d").mkdir(parents=True)
    (work / "projects" / "paris-1a2b3c4d" / "paris.mp4").write_bytes(b"video")
    (work / "tmp").mkdir()
    (work / "run.log").write_text("log")
    (work / "mine.txt").write_text("not ours")
    return work


def uninstall(layout, payload, popen, **options):
    return L.uninstall(layout, L.load_dist_config(payload), payload, runner=L.Runner(popen=popen), **options)


def test_layout_knows_the_menu_entry(layout):
    assert layout.desktop_file == layout.data_root.parent / "applications" / "vacation-memories.desktop"


def test_uninstall_removes_the_stack_and_the_launcher_folders_but_not_the_work_data(
        layout, payload, tmp_path, media, capsys):
    work = installed(layout, payload, tmp_path, media)
    popen = FakePopen()
    assert uninstall(layout, payload, popen) == 0
    assert popen.verbs() == [("stop",), ("down", "--remove-orphans")]
    assert not layout.config_dir.exists() and not layout.data_root.exists() and not layout.desktop_file.exists()
    assert (work / "projects" / "paris-1a2b3c4d" / "paris.mp4").is_file()
    assert media.is_dir()
    assert not any(command[:2] in (["docker", "rmi"], ["docker", "volume"]) for command in popen.commands)


def test_uninstall_stops_the_stack_before_it_deletes_anything(layout, payload, tmp_path, media):
    installed(layout, payload, tmp_path, media)
    seen = []

    def respond(command):
        seen.append(layout.config_dir.exists() and layout.data_dir.exists())
        return 0, []

    assert uninstall(layout, payload, FakePopen(respond)) == 0
    assert seen and all(seen)  # compose needs the env file and the compose files: they go last


def test_uninstall_images_and_models_only_when_asked(layout, payload, tmp_path, media):
    installed(layout, payload, tmp_path, media)
    popen = FakePopen(lambda command: (0, ["sha1", "sha1", "sha2"]) if command[:2] == ["docker", "images"]
                      else (0, []))
    assert uninstall(layout, payload, popen, images=True, models=True) == 0
    assert ("down", "--remove-orphans", "--rmi", "all") in popen.verbs()
    assert ["docker", "images", IMAGE, "-q"] in popen.commands
    assert ["docker", "rmi", "sha1", "sha2"] in popen.commands
    assert ["docker", "volume", "rm", "vacation-ollama-models"] in popen.commands


def test_uninstall_goes_on_when_docker_fails(layout, payload, tmp_path, media, capsys):
    installed(layout, payload, tmp_path, media)
    popen = FakePopen(lambda command: (1, ["No such volume"]) if command[:3] == ["docker", "volume", "rm"]
                      else (0, []))
    assert uninstall(layout, payload, popen, models=True) == 0
    assert "docker volume rm failed: No such volume" in capsys.readouterr().err
    assert not layout.config_dir.exists()


def test_uninstall_work_data_removes_only_what_the_application_wrote(layout, payload, tmp_path, media):
    work = installed(layout, payload, tmp_path, media)
    assert uninstall(layout, payload, FakePopen(), work_data=True, assume_yes=True) == 0
    assert sorted(path.name for path in work.iterdir()) == ["mine.txt"]


def test_uninstall_work_data_removes_the_folder_when_nothing_else_is_in_it(layout, payload, tmp_path, media):
    work = installed(layout, payload, tmp_path, media)
    (work / "mine.txt").unlink()
    assert uninstall(layout, payload, FakePopen(), work_data=True, assume_yes=True) == 0
    assert not work.exists()


def test_uninstall_work_data_asks_first(layout, payload, tmp_path, media, capsys):
    work = installed(layout, payload, tmp_path, media)
    popen = FakePopen()
    assert uninstall(layout, payload, popen, work_data=True, ask=lambda prompt: "no") == 1
    assert popen.commands == [] and (work / "projects").is_dir() and layout.config_dir.exists()
    assert uninstall(layout, payload, popen, work_data=True, ask=lambda prompt: "yes") == 0
    assert not (work / "projects").exists()


def test_uninstall_work_data_without_a_terminal_and_without_yes_does_nothing(
        layout, payload, tmp_path, media, capsys):
    work = installed(layout, payload, tmp_path, media)
    popen = FakePopen()
    assert uninstall(layout, payload, popen, work_data=True) == 1
    assert "--yes" in capsys.readouterr().err
    assert popen.commands == [] and (work / "projects").is_dir() and layout.config_dir.exists()


def test_uninstall_with_nothing_installed_is_not_an_error(layout, payload, capsys):
    popen = FakePopen()
    assert uninstall(layout, payload, popen) == 0
    assert popen.compose() == []
    assert "No saved start found" in capsys.readouterr().out


def test_uninstall_does_not_delete_while_a_launcher_cannot_be_stopped(layout, payload, tmp_path, media):
    installed(layout, payload, tmp_path, media)
    with L.single_instance(layout.lock_file):
        code = uninstall(layout, payload, FakePopen(), kill=lambda pid, number: None, sleep=lambda seconds: None,
                         clock=iter(range(0, 100, 20)).__next__, timeout=50)
    assert code == 1 and layout.answers_file.is_file() and layout.desktop_file.is_file()


def test_uninstall_from_the_command_line_leaves_no_log_behind(tmp_path, capsys, keep_logging):
    environ = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "share"),
               "XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert L.main(["--install-desktop"], environ=environ) == 0
    assert L.main(["--uninstall"], environ=environ) == 0
    assert not (tmp_path / "config" / "vacation-memories").exists()
    assert not (tmp_path / "share" / "applications" / "vacation-memories.desktop").exists()


def test_all_means_images_models_and_work_data():
    args = L.parse_args(["--uninstall", "--all"])
    assert args.remove_images and args.remove_models and args.remove_work_data


@pytest.mark.parametrize("flag", ["--all", "--remove-images", "--remove-models", "--remove-work-data"])
def test_the_remove_flags_need_uninstall(flag, capsys):
    with pytest.raises(SystemExit) as stopped:
        L.parse_args([flag])
    assert stopped.value.code == 2
    assert "--uninstall" in capsys.readouterr().err
