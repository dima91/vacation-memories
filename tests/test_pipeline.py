"""The pipeline through the CLI, in this process: modes, paths, resume, durations, AI. Synthetic media only."""

import json
import logging
import socket
import sqlite3

import pytest
from fake_ollama import FakeOllama

from vacation_memories import cli, exit_codes, pipeline, technical_analysis
from vacation_memories.media_probe import probe
from vacation_memories.process import ToolRunner
from vacation_memories.renderer import REPORT_NAME, RenderResult
from vacation_memories.synthetic_media import populate_vacation

pytestmark = pytest.mark.usefixtures("isolate_process_state")
SMALL_FRAME = ["--resolution", "568x320"]  # the smallest 16:9-like frame: quick to render


@pytest.fixture(scope="module")
def vacation(tmp_path_factory):
    """Enough material for 5 minutes: 120 photos and 6 clips over 3 days."""
    root = tmp_path_factory.mktemp("vacation")
    populate_vacation(root, photos=120, videos=6, days=3)
    return root


@pytest.fixture(scope="module")
def few(tmp_path_factory):
    root = tmp_path_factory.mktemp("few")
    populate_vacation(root, photos=6, videos=0, days=1, first_scene=500)
    return root


@pytest.fixture(scope="module")
def shared_work(tmp_path_factory):
    """One workdir for the tests that may share the analysis cache."""
    return tmp_path_factory.mktemp("shared-work")


def run(media, work, *args, environ=None):
    return cli.main(["--input", str(media), "--workdir", str(work), *args], environ=environ or {})


def duration_of(path):
    return probe(path, ToolRunner()).format_duration


def log_of(work):
    return (work / "run.log").read_text(encoding="utf-8", errors="replace")


class TestDuration:
    @pytest.mark.parametrize("target", [30, 90, 300])
    def test_the_final_video_lasts_the_requested_time_within_5_percent(self, vacation, shared_work, target, capsys):
        output = f"memories_{target}.mp4"
        code = run(vacation, shared_work, "--no-ai", "--duration", str(target), "--output", output, *SMALL_FRAME)
        assert code == exit_codes.OK, capsys.readouterr().err
        video = shared_work / output  # a relative --output ends up in the workdir
        assert video.exists() and not (shared_work / f"memories_{target}.partial.mp4").exists()
        assert duration_of(video) == pytest.approx(target, rel=0.05)
        out = capsys.readouterr().out
        assert f"Duration: requested {target:.1f} s" in out and "obtained" in out
        plan = json.loads((shared_work / "edit_plan.json").read_text())
        assert plan["requested_duration"] == target and plan["output"] == {"width": 568, "height": 320, "fps": 30.0}
        assert plan["runtime"]["ai"]["used"] is False
        assert json.loads((shared_work / "status.json").read_text())["state"] == "done"

    def test_insufficient_material_gives_a_shorter_video_a_warning_and_code_0(self, few, tmp_path, capsys):
        work = tmp_path / "work"
        code = run(few, work, "--no-ai", "--duration", "60", *SMALL_FRAME)
        assert code == exit_codes.OK
        assert duration_of(work / "memories.mp4") < 60 * 0.95
        assert "insufficient material" in log_of(work)
        plan = json.loads((work / "edit_plan.json").read_text())
        assert plan["runtime"]["insufficient_material"] is True
        sources = [element["source"] for element in plan["elements"]]
        assert len(sources) == len(set(sources))  # no clip repeated to fill the gap

    def test_a_very_long_duration_is_only_a_warning(self, few, tmp_path):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "21m") == exit_codes.OK
        assert "very long" in log_of(work)


class TestModes:
    @pytest.fixture
    def work(self, tmp_path):
        return tmp_path / "work"

    def test_analyze_only_fills_the_cache_and_stops(self, few, work, capsys):
        assert run(few, work, "--no-ai", "--analyze-only") == exit_codes.OK
        assert "Analysis saved" in capsys.readouterr().out
        with sqlite3.connect(work / "cache.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM technical").fetchone()[0] == 6
        assert not (work / "edit_plan.json").exists()
        assert not list(work.glob("*.mp4"))

    def test_dry_run_plans_from_the_cache_without_analyzing_again_and_does_not_render(self, few, work, capsys,
                                                                                    monkeypatch):
        assert run(few, work, "--no-ai", "--analyze-only") == exit_codes.OK

        def no_analysis(*args, **kwargs):
            raise AssertionError("the technical analysis ran again")

        monkeypatch.setattr(technical_analysis, "_photo_candidate", no_analysis)
        monkeypatch.setattr(technical_analysis, "_video_candidates", no_analysis)
        capsys.readouterr()
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "20") == exit_codes.OK
        out = capsys.readouterr().out
        assert "Plan (compressed structure)" in out and "body" in out and "estimate:" in out
        assert json.loads((work / "edit_plan.json").read_text())["requested_duration"] == 20
        assert not list(work.glob("*.mp4"))

        # Another duration: again only selection and plan.
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "15") == exit_codes.OK
        assert json.loads((work / "edit_plan.json").read_text())["requested_duration"] == 15

    def test_render_only_renders_the_saved_plan_without_any_analysis(self, few, work, monkeypatch, capsys):
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "15", *SMALL_FRAME) == exit_codes.OK

        def no_scan(*args, **kwargs):
            raise AssertionError("--render-only scanned the media")

        monkeypatch.setattr(pipeline, "scan_media", no_scan)
        assert run(few, work, "--render-only", "--duration", "60", "--require-ai") == exit_codes.OK
        assert (work / "memories.mp4").exists()
        assert "--duration 60.0 is ignored" in log_of(work)

    def test_render_only_takes_resolution_and_aspect_from_the_command_line(self, few, work, monkeypatch):
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "15", *SMALL_FRAME) == exit_codes.OK
        rendered = []

        def fake_render(plan, **kwargs):
            rendered.append((plan.output.width, plan.output.height))
            return RenderResult(True, kwargs["output"], {"obtained_duration": plan.planned_duration})

        monkeypatch.setattr(pipeline, "render_plan", fake_render)
        assert run(few, work, "--render-only", "--resolution", "480p") == exit_codes.OK
        assert run(few, work, "--render-only", "--aspect", "1:1") == exit_codes.OK
        assert run(few, work, "--render-only", "--resolution", "720p", "--aspect", "9:16") == exit_codes.OK
        assert rendered == [(852, 480), (320, 320), (720, 1280)]

    def test_above_1080p_a_warning_with_an_estimate(self, few, work, monkeypatch):
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "15", *SMALL_FRAME) == exit_codes.OK
        monkeypatch.setattr(pipeline, "render_plan",
                            lambda plan, **kwargs: RenderResult(True, kwargs["output"], {"obtained_duration": 15.0}))
        assert run(few, work, "--render-only", "--resolution", "1440p") == exit_codes.OK
        assert "above 1080p the software encoder is much slower (rendering about" in log_of(work)

    @pytest.mark.parametrize(
        "modes",
        [["--analyze-only", "--dry-run"], ["--dry-run", "--render-only"], ["--analyze-only", "--render-only"],
         ["--analyze-only", "--dry-run", "--render-only"]],
    )
    def test_modes_are_mutually_exclusive(self, few, work, modes, capsys):
        assert run(few, work, "--no-ai", *modes) == exit_codes.USAGE
        assert "mutually exclusive" in capsys.readouterr().err
        assert not (work / "status.json").exists()

    def test_a_plan_with_an_unsupported_schema_version_is_rejected(self, few, work, capsys):
        work.mkdir()
        (work / "edit_plan.json").write_text(json.dumps({
            "schema_version": 99, "output": {"width": 640, "height": 360, "fps": 30},
            "elements": [{"source": "x.jpg", "type": "photo", "duration": 3}],
        }))
        assert run(few, work, "--render-only") == exit_codes.USAGE
        assert "schema_version" in capsys.readouterr().err

    def test_render_only_without_a_plan_is_a_usage_error(self, few, work, capsys):
        assert run(few, work, "--render-only") == exit_codes.USAGE
        assert "plan not found" in capsys.readouterr().err


class TestPaths:
    def test_an_output_inside_the_input_is_refused(self, few, tmp_path, capsys):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--output", str(few / "memories.mp4")) == exit_codes.USAGE
        assert "inside the input folder" in capsys.readouterr().err
        assert not (few / "memories.mp4").exists() and not (few / "memories.partial.mp4").exists()

    def test_an_output_in_a_missing_folder_is_refused_at_startup(self, few, tmp_path, capsys):
        assert run(few, tmp_path / "work", "--no-ai", "--output", str(tmp_path / "nope" / "x.mp4")) == exit_codes.USAGE
        assert "does not exist" in capsys.readouterr().err
        assert not (tmp_path / "work" / "cache.sqlite3").exists()  # nothing was analyzed

    def test_a_missing_input_folder_or_music_file_is_a_usage_error(self, few, tmp_path):
        assert run(tmp_path / "nope", tmp_path / "work", "--no-ai") == exit_codes.USAGE
        assert run(few, tmp_path / "work", "--no-ai", "--music", str(tmp_path / "none.mp3")) == exit_codes.USAGE

    def test_an_absolute_output_outside_the_workdir_is_allowed(self, few, tmp_path):
        elsewhere = tmp_path / "videos"
        elsewhere.mkdir()
        code = run(few, tmp_path / "work", "--no-ai", "--duration", "15", "--output", str(elsewhere / "a.mp4"),
                   *SMALL_FRAME)
        assert code == exit_codes.OK and (elsewhere / "a.mp4").exists()


class TestResume:
    def test_resume_with_different_parameters_exits_2(self, few, tmp_path, capsys):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "20") == exit_codes.OK
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "25", "--resume") == exit_codes.USAGE
        assert "duration" in capsys.readouterr().err
        assert run(few, work, "--no-ai", "--dry-run", "--structure", "chronological", "--resume") == exit_codes.USAGE

    def test_resume_takes_the_saved_parameters_and_accepts_other_performance_options(self, few, tmp_path):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "20") == exit_codes.OK
        assert run(few, work, "--no-ai", "--dry-run", "--resume", "--jobs", "1", "--nice", "1") == exit_codes.OK
        assert json.loads((work / "edit_plan.json").read_text())["requested_duration"] == 20
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "20", "--resume") == exit_codes.OK

    def test_resume_without_a_previous_run_exits_2(self, few, tmp_path, capsys):
        assert run(few, tmp_path / "work", "--no-ai", "--resume") == exit_codes.USAGE
        assert "nothing to resume" in capsys.readouterr().err

    def test_a_run_whose_plan_was_saved_resumes_from_the_rendering(self, few, tmp_path, monkeypatch):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--duration", "15", *SMALL_FRAME) == exit_codes.OK
        config = json.loads((work / "run_config.json").read_text())
        assert config["plan_ready"] and config["state"] == "done" and config["params"]["duration"] == 15

        monkeypatch.setattr(pipeline, "scan_media", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned")))
        assert run(few, work, "--no-ai", "--resume") == exit_codes.OK
        assert json.loads((work / REPORT_NAME).read_text())["segments"]["reused"] > 0


class TestAi:
    def test_the_vision_model_is_used_and_its_answers_reach_the_plan(self, few, tmp_path, fake_ollama):
        work = tmp_path / "work"
        code = run(few, work, "--dry-run", "--duration", "20", "--ollama-url", fake_ollama.url)
        assert code == exit_codes.OK
        assert len(fake_ollama.chat_requests) == 6 and fake_ollama.unload_requests == 1
        plan = json.loads((work / "edit_plan.json").read_text())
        assert plan["runtime"]["ai"]["used"] and plan["runtime"]["ai"]["coverage"]["analyzed"] == 6
        assert all(element["moment_type"] == "scenery" for element in plan["elements"])
        profile = json.loads((work / "runtime_profile.json").read_text())
        assert profile["ollama"]["placement"] == "gpu"

        # Another duration: the answers come from the cache, Ollama is not even contacted, so a missing
        # Ollama does not matter even with --require-ai.
        closed = f"http://127.0.0.1:{_free_port()}"
        code = run(few, work, "--dry-run", "--duration", "25", "--ollama-url", closed, "--require-ai")
        assert code == exit_codes.OK
        assert len(fake_ollama.chat_requests) == 6
        assert json.loads((work / "edit_plan.json").read_text())["runtime"]["ai"]["coverage"]["from_cache"] == 6

    def test_require_ai_without_ollama_exits_4(self, few, tmp_path, capsys):
        closed = f"http://127.0.0.1:{_free_port()}"
        assert run(few, tmp_path / "work", "--dry-run", "--ollama-url", closed, "--require-ai") == exit_codes.REQUIREMENTS
        assert "not reachable" in capsys.readouterr().err

    def test_without_ollama_and_without_require_ai_the_run_goes_on_without_ai(self, few, tmp_path):
        work = tmp_path / "work"
        closed = f"http://127.0.0.1:{_free_port()}"
        assert run(few, work, "--dry-run", "--duration", "20", "--ollama-url", closed) == exit_codes.OK
        plan = json.loads((work / "edit_plan.json").read_text())
        assert plan["runtime"]["ai"]["used"] is False
        assert "without AI" in log_of(work)

    def test_a_missing_model_with_require_ai_exits_4(self, few, tmp_path):
        with FakeOllama(installed=False) as server:
            code = run(few, tmp_path / "work", "--dry-run", "--ollama-url", server.url, "--require-ai")
        assert code == exit_codes.REQUIREMENTS

    def test_a_non_local_ollama_needs_permission_and_gives_a_warning(self, few, tmp_path, capsys):
        address = _own_address()
        if address is None:
            pytest.skip("no non-loopback address on this machine")
        with FakeOllama(host="0.0.0.0") as server:
            url = f"http://{address}:{server.port}"
            assert run(few, tmp_path / "w1", "--dry-run", "--ollama-url", url) == exit_codes.USAGE
            assert "--allow-remote-ollama" in capsys.readouterr().err
            work = tmp_path / "w2"
            code = run(few, work, "--dry-run", "--duration", "20", "--ollama-url", url, "--allow-remote-ollama")
            assert code == exit_codes.OK and server.chat_requests
        assert "is not local" in log_of(work)

    def test_require_ai_does_not_apply_to_render_only(self, few, tmp_path):
        work = tmp_path / "work"
        assert run(few, work, "--no-ai", "--dry-run", "--duration", "15", *SMALL_FRAME) == exit_codes.OK
        closed = f"http://127.0.0.1:{_free_port()}"
        assert run(few, work, "--render-only", "--ollama-url", closed, "--require-ai") == exit_codes.OK


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _own_address() -> str | None:
    """An address of this machine that is not a loopback one (the container's, in the compose network)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # no packet is sent: it only picks the outgoing interface
            address = sock.getsockname()[0]
    except OSError:
        try:
            address = socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    return None if address.startswith("127.") else address


def test_a_phase_that_counts_by_itself_moves_the_status_without_logging(tmp_path, caplog):
    from vacation_memories.renderer import Progress
    from vacation_memories.workdir import StatusWriter

    status = StatusWriter(tmp_path / "status.json")
    status.start()
    progress = Progress(status)
    progress.phase("AI analysis", 36)
    with caplog.at_level(logging.INFO):
        progress.advance_to(23)  # e.g. 3 analyzed and 20 left out by the time budget
        progress.advance_to(36)
    data = json.loads((tmp_path / "status.json").read_text())
    assert (data["phase"], data["done"], data["total"]) == ("AI analysis", 36, 36)
    assert "AI analysis:" not in caplog.text
