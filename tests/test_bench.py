import base64
import io
import json
import os
from pathlib import Path

import pytest
from fake_ollama import VALID_ANSWER, Reply
from PIL import Image
from system_fakes import make_env

from vacation_memories import cli, exit_codes
from vacation_memories import synthetic_media as sm
from vacation_memories.bench import run_bench_vision
from vacation_memories.config import resolve_settings
from vacation_memories.runtime_profile import detect_profile

pytestmark = pytest.mark.usefixtures("isolate_process_state")  # main() changes process-wide state


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def real_http_env(tmp_path):
    """Detection that really asks the simulated Ollama server."""
    from vacation_memories.runtime_profile import _http_get_json

    env = make_env(tmp_path / "sys")
    env.http_get_json = _http_get_json
    return env


def bench(tmp_path, server, *args, environ=None, env=None):
    argv = ["bench", "vision", "--workdir", str(tmp_path / "work"), "--ollama-url", server.url, *args]
    return cli.main(argv, environ=environ or {}, detection_env=env or real_http_env(tmp_path))


def sent_images(server) -> list[Image.Image]:
    return [Image.open(io.BytesIO(base64.b64decode(data))) for data in server.images_sent()]


def test_by_default_it_asks_about_five_synthetic_images_and_reports_the_speed(tmp_path, fake_ollama, capsys):
    assert bench(tmp_path, fake_ollama) == exit_codes.OK
    out = capsys.readouterr().out
    assert len(fake_ollama.chat_requests) == 5
    assert "seconds per frame" in out
    images = sent_images(fake_ollama)
    assert len({image.tobytes() for image in images}) == 5  # five different scenes


def test_count_sets_the_number_of_images(tmp_path, fake_ollama):
    assert bench(tmp_path, fake_ollama, "--count", "3") == exit_codes.OK
    assert len(fake_ollama.chat_requests) == 3


def test_the_images_option_uses_the_photos_of_a_folder_and_leaves_it_untouched(tmp_path, fake_ollama):
    folder = tmp_path / "photos"
    folder.mkdir()
    for index in range(3):
        sm.make_photo(folder / f"p{index}.jpg", index + 1, 1600, 1200)
    sm.make_video(folder / "clip.mp4", [(1, 1.0)], 160, 120, fps=10)
    (folder / "notes.txt").write_text("not media")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir())

    assert bench(tmp_path, fake_ollama, "--images", str(folder)) == exit_codes.OK

    assert len(fake_ollama.chat_requests) == 3  # the photos only, fewer than --count
    assert {image.size for image in sent_images(fake_ollama)} == {(512, 384)}
    assert sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir()) == before


def test_with_more_photos_than_images_requested_they_are_spread_over_the_folder(tmp_path, fake_ollama):
    folder = tmp_path / "photos"
    folder.mkdir()
    for index in range(10):
        sm.make_photo(folder / f"p{index:02d}.jpg", index + 1, 160, 120)
    assert bench(tmp_path, fake_ollama, "--images", str(folder), "--count", "2") == exit_codes.OK
    first, second = sent_images(fake_ollama)
    assert first.tobytes() != second.tobytes()
    assert len(fake_ollama.chat_requests) == 2


@pytest.mark.parametrize("make", ["empty", "missing"])
def test_a_folder_without_photos_is_a_usage_error(tmp_path, fake_ollama, capsys, make):
    folder = tmp_path / "photos"
    if make == "empty":
        folder.mkdir()
    assert bench(tmp_path, fake_ollama, "--images", str(folder)) == exit_codes.USAGE
    assert capsys.readouterr().err.startswith("error:")
    assert fake_ollama.chat_requests == []


def test_it_never_uses_the_cache_so_every_run_measures_the_model(tmp_path, fake_ollama):
    bench(tmp_path, fake_ollama, "--count", "2")
    bench(tmp_path, fake_ollama, "--count", "2")
    assert len(fake_ollama.chat_requests) == 4
    assert not (tmp_path / "work" / "cache.sqlite3").exists()


def test_the_model_is_unloaded_at_the_end(tmp_path, fake_ollama):
    bench(tmp_path, fake_ollama, "--count", "2")
    assert fake_ollama.unload_requests == 1


def test_where_the_model_runs_is_shown_and_recorded_in_the_runtime_profile(tmp_path, fake_ollama, capsys):
    fake_ollama.placement = "mixed"
    assert bench(tmp_path, fake_ollama, "--count", "2") == exit_codes.OK
    assert "mixed" in capsys.readouterr().out
    profile = json.loads((tmp_path / "work" / "runtime_profile.json").read_text())
    assert profile["ollama"]["placement"] == "mixed"
    assert profile["ollama"]["model_loaded"] is True


def test_the_model_option_selects_the_model(tmp_path, capsys):
    from fake_ollama import FakeOllama

    with FakeOllama(model="other:tag") as server:
        assert bench(tmp_path, server, "--model", "other:tag", "--count", "1") == exit_codes.OK
        assert server.chat_requests[0]["model"] == "other:tag"


def test_the_ai_resolution_setting_sets_the_size_of_the_images(tmp_path, fake_ollama):
    environ = {"VACATION_MEMORIES_AI_RESOLUTION": "256"}
    bench(tmp_path, fake_ollama, "--count", "1", environ=environ)
    assert max(sent_images(fake_ollama)[0].size) == 256


def test_a_model_that_is_not_installed_exits_with_4(tmp_path, capsys):
    from fake_ollama import FakeOllama

    with FakeOllama(installed=False) as server:
        assert bench(tmp_path, server) == exit_codes.REQUIREMENTS
        assert "not installed" in capsys.readouterr().err
        assert server.chat_requests == []


def test_an_unreachable_ollama_exits_with_4(tmp_path, capsys):
    assert cli.main(
        ["bench", "vision", "--workdir", str(tmp_path / "work"), "--ollama-url", "http://127.0.0.1:1"],
        environ={}, detection_env=real_http_env(tmp_path),
    ) == exit_codes.REQUIREMENTS
    assert "not reachable" in capsys.readouterr().err


def test_without_the_ai_there_is_nothing_to_measure(tmp_path, fake_ollama, capsys):
    assert bench(tmp_path, fake_ollama, "--no-ai") == exit_codes.USAGE
    assert "--no-ai" in capsys.readouterr().err


def test_all_frames_failing_is_an_error(tmp_path, fake_ollama, capsys):
    fake_ollama.on_chat = lambda body: Reply(status=500)
    assert bench(tmp_path, fake_ollama, "--count", "2") == exit_codes.ERROR
    assert "no frame" in capsys.readouterr().err


def test_a_failed_frame_is_reported_and_the_others_still_count(tmp_path, fake_ollama, capsys):
    fake_ollama.script.extend([Reply(status=500)] * 2)
    assert bench(tmp_path, fake_ollama, "--count", "3") == exit_codes.OK
    out = capsys.readouterr().out
    assert "failed" in out


def test_the_options_after_bench_follow_the_usual_configuration_layers(tmp_path, fake_ollama):
    config = tmp_path / "config.toml"
    config.write_text("ai_resolution = 192\n")
    assert bench(tmp_path, fake_ollama, "--count", "1", "--config", str(config)) == exit_codes.OK
    assert max(sent_images(fake_ollama)[0].size) == 192


class TestReport:
    """The statistics, with a clock the simulated model advances."""

    def run(self, tmp_path, fake_ollama, durations, capsys):
        clock = FakeClock()
        steps = iter(durations)

        def on_chat(body):
            clock.now += next(steps)
            return Reply(VALID_ANSWER)

        fake_ollama.on_chat = on_chat
        settings = resolve_settings(
            {"workdir": tmp_path / "work", "ollama_url": fake_ollama.url}, {}
        )
        (tmp_path / "work").mkdir()
        profile = detect_profile(settings, real_http_env(tmp_path))
        code = run_bench_vision(settings, profile, real_http_env(tmp_path), images=None, count=len(durations),
                                clock=clock)
        return code, capsys.readouterr().out

    def test_the_first_frame_is_reported_apart_because_it_includes_loading_the_model(self, tmp_path, fake_ollama,
                                                                                     capsys):
        code, out = self.run(tmp_path, fake_ollama, [9.0, 2.0, 4.0, 3.0], capsys)
        assert code == exit_codes.OK
        assert "first frame (includes loading the model): 9.00 s" in out
        assert "median 3.00 s" in out
        assert "mean 3.00 s" in out
        assert "min 2.00 s" in out and "max 4.00 s" in out

    def test_a_single_frame_has_only_the_first_figure(self, tmp_path, fake_ollama, capsys):
        code, out = self.run(tmp_path, fake_ollama, [5.0], capsys)
        assert code == exit_codes.OK
        assert "first frame (includes loading the model): 5.00 s" in out
        assert "median" not in out

    def test_each_frame_is_listed_with_its_answer(self, tmp_path, fake_ollama, capsys):
        _, out = self.run(tmp_path, fake_ollama, [1.0, 1.0], capsys)
        assert out.count("moment_type=scenery") == 2
        assert "narrative_value=3" in out and "category=sea" in out


def test_a_count_below_one_is_a_usage_error(tmp_path, fake_ollama, capsys):
    assert bench(tmp_path, fake_ollama, "--count", "0") == exit_codes.USAGE
    assert "--count" in capsys.readouterr().err


class TestRemoteOllama:
    REMOTE = "http://ollama.example.test:11434"

    def run(self, tmp_path, *args):
        return cli.main(["bench", "vision", "--workdir", str(tmp_path / "work"), "--ollama-url", self.REMOTE, *args],
                        environ={}, detection_env=real_http_env(tmp_path))

    def test_a_non_local_server_needs_the_explicit_permission(self, tmp_path, capsys):
        assert self.run(tmp_path) == exit_codes.USAGE
        assert "--allow-remote-ollama" in capsys.readouterr().err

    def test_with_the_permission_it_goes_ahead_with_a_warning(self, tmp_path, capsys):
        # nothing answers at that name here, so the run stops at the missing requirement
        assert self.run(tmp_path, "--allow-remote-ollama") == exit_codes.REQUIREMENTS
        assert "not local" in capsys.readouterr().err
