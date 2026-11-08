"""`evaluate synthetic` end to end: dataset, analysis without AI, full and random plans, report."""

import json
import signal

import pytest

from vacation_memories import cli, exit_codes, technical_analysis
from vacation_memories.config import resolve_settings
from vacation_memories.evaluate import synthetic
from vacation_memories.evaluate.ground_truth import GROUND_TRUTH_NAME
from vacation_memories.evaluate.metrics import METRICS
from vacation_memories.evaluate.synthetic import REPORT_NAME, SyntheticRequest, dataset_folder
from vacation_memories.process import Interrupted
from vacation_memories.renderer import Progress
from vacation_memories.workdir import WorkdirLock

pytestmark = pytest.mark.usefixtures("isolate_process_state")
# A remote Ollama that is not allowed, and a configuration that requires the AI: neither concerns
# `evaluate synthetic`, which never uses it.
REMOTE_OLLAMA = {"OLLAMA_HOST": "http://ollama:11434"}


def evaluate(work, *args, environ=REMOTE_OLLAMA):
    config = work.parent / "require-ai.toml"
    config.write_text("require_ai = true\n")
    return cli.main(["--workdir", str(work), "--config", str(config), "evaluate", "synthetic", *args],
                    environ=environ)


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    return tmp_path_factory.mktemp("synthetic") / "work"


@pytest.fixture(scope="module")
def report(work):
    """One evaluation of the default vacation, shared by the tests that only read its report.

    It runs before the per-test isolation of the process state, so not through `cli.main`.
    """
    settings = resolve_settings({"workdir": work}, {})
    assert synthetic.run_synthetic(settings, SyntheticRequest(duration=60, seeds=8), out=lambda line: None) == 0
    return json.loads((dataset_folder(work, SyntheticRequest()) / REPORT_NAME).read_text())


def test_the_report_puts_every_metric_of_the_full_plan_against_the_random_plans(report):
    assert set(report["metrics"]) == set(METRICS)
    assert report["random_seeds"] == 8 and report["duration"] == 60
    assert report["dataset"]["seed"] == 0 and report["dataset"]["files"] > 50
    for entry in report["metrics"].values():
        assert {"full", "direction", "random_mean", "random_std", "random_min", "random_max"} <= set(entry)
    assert "do not say whether the video is pleasant" in report["note"]


def test_the_full_plan_does_better_than_chance_where_the_mvp_can_see(report):
    """A regression check on the default vacation: quality, duplicates and motion are what the MVP knows."""
    metrics = report["metrics"]
    assert metrics["defects_selected"]["full"] == 0 < metrics["defects_selected"]["random_max"]
    assert metrics["near_duplicate_repeats"]["full"] < metrics["near_duplicate_repeats"]["random_mean"]
    assert metrics["important_recall_motion"]["full"] > metrics["important_recall_motion"]["random_mean"]
    assert metrics["durations_in_limits"]["full"] == 1.0


def test_a_second_run_reuses_the_vacation_and_the_analysis(work, report, monkeypatch, capsys):
    def no_work(*args, **kwargs):
        raise AssertionError("generated or analyzed again")

    monkeypatch.setattr(synthetic, "populate_evaluation_vacation", no_work)
    monkeypatch.setattr(technical_analysis, "_photo_candidate", no_work)
    monkeypatch.setattr(technical_analysis, "_video_candidates", no_work)
    assert evaluate(work, "--seeds", "8", "--duration", "60") == exit_codes.OK
    again = json.loads((dataset_folder(work, SyntheticRequest()) / REPORT_NAME).read_text())
    assert again["metrics"] == report["metrics"]
    printed = capsys.readouterr().out
    assert "important_recall" in printed and "Report saved to" in printed


def test_a_second_instance_on_the_same_vacation_exits_5(work, report, capsys):
    with WorkdirLock(dataset_folder(work, SyntheticRequest()) / "work"):
        assert evaluate(work, "--seeds", "1") == exit_codes.WORKDIR_IN_USE
    assert "in use" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--seeds", "0"], ["--duration", "5"], ["--duration", "soon"]])
def test_invalid_seeds_or_duration_are_usage_errors(tmp_path, args):
    assert evaluate(tmp_path / "work", *args) == exit_codes.USAGE


def test_an_interrupted_generation_is_never_taken_for_a_whole_vacation(tmp_path):
    class StopAfter:
        def __init__(self, calls):
            self.calls = calls

        def check(self):
            self.calls -= 1
            if self.calls < 0:
                raise Interrupted(signal.SIGTERM)

    request = SyntheticRequest(days=1, places_per_day=1)
    media = tmp_path / "media"
    with pytest.raises(Interrupted):
        synthetic._dataset(media, request, StopAfter(4), Progress(None))
    assert not media.exists() and not (media.with_name("media.partial") / GROUND_TRUTH_NAME).exists()
    truth = synthetic._dataset(media, request, StopAfter(10_000), Progress(None))
    assert (media / GROUND_TRUTH_NAME).exists() and not media.with_name("media.partial").exists()
    assert len(truth.files) == len(list(media.rglob("*.jpg"))) + len(list(media.rglob("*.mp4")))
