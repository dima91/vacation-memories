"""`evaluate prepare` and `evaluate report` end to end, on a synthetic vacation with known truth only."""

import json
import re
import signal
import socket
from datetime import datetime

import pytest

from vacation_memories import cli
from vacation_memories.analysis.media_probe import probe
from vacation_memories.core import exit_codes
from vacation_memories.core.config import resolve_settings
from vacation_memories.core.process import Interrupted, ToolRunner
from vacation_memories.core.workdir import WorkdirLock
from vacation_memories.evaluate import prepare, report
from vacation_memories.evaluate.ground_truth import NEAR_DUPLICATE
from vacation_memories.evaluate.labels import LABELS_NAME, Segment, load_history, load_labels
from vacation_memories.evaluate.page import PAGE_NAME
from vacation_memories.evaluate.prepare import (
    FULL,
    MAPPING_NAME,
    PREPARE_NAME,
    RANDOM,
    TECHNICAL_ONLY,
    VARIANTS,
    PrepareRequest,
    run_prepare,
)
from vacation_memories.evaluate.report import REPORT_MD_NAME, SUMMARY_NAME, ReportRequest, run_report
from vacation_memories.evaluate.synthetic_media import populate_evaluation_vacation

pytestmark = pytest.mark.usefixtures("isolate_process_state")
QUIET = {"out": lambda line: None}


@pytest.fixture(scope="module")
def vacation(tmp_path_factory):
    media = tmp_path_factory.mktemp("review") / "media"
    return media, populate_evaluation_vacation(media, days=2, places_per_day=1)


@pytest.fixture(scope="module")
def first(vacation, tmp_path_factory):
    """The first cycle: three variants of 20 s, without AI. Run outside the per-test isolation, so not via `cli`."""
    media, _ = vacation
    work = tmp_path_factory.mktemp("review-work")
    settings = resolve_settings({"input": media, "workdir": work, "no_ai": True}, {})
    request = PrepareRequest(duration=20, variants=(FULL, RANDOM, TECHNICAL_ONLY), seed=7, run_id="cycle1")
    assert run_prepare(settings, request, **QUIET) == exit_codes.OK
    return work, work / "eval" / "cycle1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def page_model(run_dir):
    page = (run_dir / PAGE_NAME).read_text(encoding="utf-8")
    return json.loads(re.search(r'id="review-data">(.*?)</script>', page, re.S).group(1))


def ratings_for(prepared, *, rating=lambda number: 5 - number, important=lambda number: number % 2 == 0):
    """What the page exports: every clip and every left-out moment, with some flags."""
    ratings = {"version": 1, "run_id": prepared["run_id"], "variants": {}, "excluded": {}}
    for number, (letter, variant) in enumerate(sorted(prepared["variants"].items())):
        ratings["variants"][letter] = {
            "rating": rating(number), "rank": number + 1,
            "rhythm": {section["name"]: "right" for section in variant["sections"] if section["clips"]},
            "clips": {clip["key"]: {"repetitive": position % 3 == 0, "length": "too_long" if position == 1 else "ok"}
                      for position, clip in enumerate(variant["clips"])},
        }
    for number, item in enumerate(prepared["excluded"]):
        ratings["excluded"][item["key"]] = {"important": important(number)}
    return ratings


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


@pytest.fixture(scope="module")
def reported(first):
    """The report of the first cycle, read as soon as it is written."""
    work, run_dir = first
    prepared = read(run_dir / PREPARE_NAME)
    (run_dir / "ratings.json").write_text(json.dumps(ratings_for(prepared)))
    settings = resolve_settings({"workdir": work, "no_ai": True}, {})
    assert run_report(settings, ReportRequest(run_id="cycle1"), **QUIET) == exit_codes.OK
    return {
        "prepared": prepared,
        "summary": read(run_dir / SUMMARY_NAME),
        "markdown": (run_dir / REPORT_MD_NAME).read_text(encoding="utf-8"),
        "labels": load_labels(work / "eval"),
        "history": load_history(work / "eval"),
        "mapping": read(run_dir / MAPPING_NAME),
    }


# --- prepare ---------------------------------------------------------------------------------------


def test_prepare_writes_a_preview_per_version_the_thumbnails_and_the_page(first):
    work, run_dir = first
    prepared, mapping = read(run_dir / PREPARE_NAME), read(run_dir / MAPPING_NAME)
    assert sorted(prepared["variants"]) == ["A", "B", "C"]
    assert sorted(mapping["letters"].values()) == sorted([FULL, RANDOM, TECHNICAL_ONLY])
    assert mapping["seeds"]["random"] == 7
    for letter, variant in prepared["variants"].items():
        video = probe(run_dir / variant["video"], ToolRunner())
        assert video.video.height == 540  # the preview
        assert video.format_duration == pytest.approx(20, rel=0.05)
        assert variant["obtained_duration"] == pytest.approx(20, rel=0.05) and variant["clips"]
    thumbs = [entry["thumb"] for entry in prepared["segments"].values()]
    assert all(thumbs) and all((run_dir / thumb).is_file() for thumb in thumbs)
    assert (run_dir / PAGE_NAME).is_file() and read(work / "status.json")["state"] == "done"
    # The render reports stay with the previews: the one of the last pipeline run is not overwritten.
    assert all((run_dir / "variants" / f"{letter}_render.json").is_file() for letter in prepared["variants"])
    assert not (work / "render_report.json").exists()


def test_prepare_json_and_the_page_do_not_tell_the_versions_apart(first):
    _, run_dir = first
    prepared, model = read(run_dir / PREPARE_NAME), page_model(run_dir)
    for data in (prepared, model):
        assert not set(strings(data)) & set(VARIANTS)
        assert not any("random baseline" in text or "seed" in text for text in strings(data))
    assert [variant["label"] for variant in model["variants"]] == ["A", "B", "C"]
    for variant in model["variants"]:
        assert (run_dir / variant["video"]).is_file()
    assert model["new"] and not model["labels"]  # the first cycle: everything is new


def test_the_technical_only_version_is_ranked_by_technical_score(first):
    _, run_dir = first
    prepared, letters = read(run_dir / PREPARE_NAME), read(run_dir / MAPPING_NAME)["letters"]
    letter = next(letter for letter, name in letters.items() if name == TECHNICAL_ONLY)
    for clip in prepared["variants"][letter]["clips"]:
        assert clip["score"]["narrative_score"] == clip["score"]["technical_score"] == clip["score"]["final"]


def test_the_gallery_shows_left_out_moments_with_their_reason(vacation, first):
    _, truth = vacation
    _, run_dir = first
    prepared, letters = read(run_dir / PREPARE_NAME), read(run_dir / MAPPING_NAME)["letters"]
    full = next(letter for letter, name in letters.items() if name == FULL)
    segments = prepared["segments"]
    shown = [Segment(segments[c["key"]]["source"], segments[c["key"]]["start"], segments[c["key"]]["end"])
             for c in prepared["variants"][full]["clips"]]
    by_reason = {}
    for item in prepared["excluded"]:
        entry = segments[item["key"]]
        segment = Segment(entry["source"], entry["start"], entry["end"])
        assert not any(clip.iou(segment) >= 0.99 for clip in shown)
        middle = 0.0 if entry["start"] is None else (entry["start"] + entry["end"]) / 2
        scene = next(s for s in truth.file(entry["source"]).scenes if s.start is None or s.start <= middle < s.end)
        by_reason.setdefault(item["reason"], []).append(scene)
    assert {"technical_threshold", "duplicate"} <= set(by_reason) <= set(prepare.EXCLUSION_REASONS)
    assert all(scene.defect for scene in by_reason["technical_threshold"])
    assert all(scene.group_kind == NEAR_DUPLICATE for scene in by_reason["duplicate"])
    assert len(by_reason["technical_threshold"]) == sum(1 for _, s in truth.scenes() if s.defect)  # all of them


# --- report ----------------------------------------------------------------------------------------


def test_the_report_answers_the_five_questions(reported):
    summary, markdown, mapping = reported["summary"], reported["markdown"], reported["mapping"]
    assert set(summary["variants"]) == {FULL, RANDOM, TECHNICAL_ONLY}
    full = summary["variants"][FULL]
    assert full["rating"] is not None and full["rank"] is not None and full["repetitive_share"] > 0
    assert full["too_long_share"] > 0 and full["similarity"]["repetitive"] and full["durations"]["by_section"]
    assert full["days"] and full["sections"] and full["moment_type_share"]
    important = summary["important"]
    assert important["flagged"] > 0 and important["most_frequent_reason"] in important["flagged_by_reason"]
    assert summary["blind"]["full_beats_random"] == (full["rating"] > summary["variants"][RANDOM]["rating"])
    assert summary["cycle"] == 1 and summary["previous_cycle"] is None and "indicative" in summary["note"]
    for name in (FULL, RANDOM, TECHNICAL_ONLY):
        assert name in markdown
    assert "## 5. Against the random selection" in markdown and "first cycle" in markdown
    assert set(mapping["letters"].values()) == set(summary["variants"])


def test_the_summary_is_anonymous(vacation, reported):
    _, truth = vacation
    texts = list(strings(reported["summary"]))
    names = {part for item in truth.files for part in item.path.split("/")}
    names |= {part.rsplit(".", 1)[0] for part in names}
    dates = {datetime.fromisoformat(item.captured_at).date().isoformat() for item in truth.files}
    for text in texts:
        assert not any(name in text for name in names), text
        assert not any(day in text for day in dates), text
        assert "/" not in text and not text.endswith((".jpg", ".mp4")), text
    assert "cycle1" not in texts
    segments = [clip["segment"] for variant in reported["summary"]["variants"].values() for clip in variant["clips"]]
    assert segments and all(re.fullmatch(r"[0-9a-f]{16}", segment) for segment in segments)


def test_the_report_records_the_labels_and_the_cycle(reported):
    labels, history, prepared = reported["labels"], reported["history"], reported["prepared"]
    flagged = [item for number, item in enumerate(prepared["excluded"]) if number % 2 == 0]
    assert len(labels.flagged("important")) == len(flagged)
    assert labels.flagged("repetitive") and labels.flagged("length", "too_long")
    assert [cycle["run_id"] for cycle in history] == ["cycle1"]
    assert history[0]["config_hash"] == prepared["config_hash"] and history[0]["variants"][FULL]["rating"] == 5 - sorted(
        prepared["variants"]).index(next(l for l, n in reported["mapping"]["letters"].items() if n == FULL))


def test_a_second_cycle_starts_from_the_labels_and_is_compared_with_the_first(vacation, first, reported):
    media, _ = vacation
    work, _ = first
    settings = resolve_settings({"input": media, "workdir": work, "no_ai": True}, {})
    request = PrepareRequest(duration=20, variants=(FULL, RANDOM), seed=8, run_id="cycle2")
    assert run_prepare(settings, request, **QUIET) == exit_codes.OK
    run_dir = work / "eval" / "cycle2"
    prepared, model = read(run_dir / PREPARE_NAME), page_model(run_dir)
    known = set(model["labels"])
    assert known and set(model["new"]).isdisjoint(known)
    assert set(model["new"]) | known == set(prepared["segments"])
    ratings = ratings_for(prepared)
    for letter, entry in ratings["variants"].items():  # the page starts from the earlier flags
        for key, flags in entry["clips"].items():
            if key in model["labels"] and model["labels"][key]["repetitive"] is not None:
                flags["repetitive"] = model["labels"][key]["repetitive"]
    (run_dir / "ratings.json").write_text(json.dumps(ratings))
    assert run_report(resolve_settings({"workdir": work, "no_ai": True}, {}), ReportRequest(), **QUIET) == 0
    summary = read(run_dir / SUMMARY_NAME)  # the latest run, without --run-id
    assert summary["cycle"] == 2 and summary["previous_cycle"]["cycle"] == 1
    assert summary["previous_cycle"]["same_configuration"] is True
    assert summary["blind"]["history"]["cycles"] == 2
    assert [cycle["run_id"] for cycle in load_history(work / "eval")] == ["cycle1", "cycle2"]
    assert summary["important"]["labelled"] >= reported["summary"]["important"]["labelled"]


def test_a_report_without_ratings_exits_2_and_never_reads_the_mapping(first, monkeypatch, capsys):
    work, run_dir = first

    def no_mapping(*args, **kwargs):
        raise AssertionError("the mapping was read before the ratings")

    monkeypatch.setattr(report, "load_mapping", no_mapping)
    base = ["--workdir", str(work), "evaluate", "report", "--run-id", "cycle1", "--ratings"]
    assert cli.main([*base, str(run_dir / "missing.json")], environ={}) == exit_codes.USAGE
    assert "no ratings" in capsys.readouterr().err
    prepared = read(run_dir / PREPARE_NAME)
    out_of_range = ratings_for(prepared)
    out_of_range["variants"]["A"]["rating"] = 9
    for bad, message in ((out_of_range, "not valid"), ({**ratings_for(prepared), "run_id": "other"}, "of the run"),
                         ({**ratings_for(prepared), "variants": {"Z": {}}}, "not valid")):
        (run_dir / "bad.json").write_text(json.dumps(bad))
        assert cli.main([*base, "eval/cycle1/bad.json"], environ={}) == exit_codes.USAGE  # relative: in the workdir
        assert message in capsys.readouterr().err
    assert cli.main(["--workdir", str(work), "evaluate", "report", "--run-id", "nothing"], environ={}) == 2


def test_the_report_runs_without_ollama_even_with_require_ai(first, reported):
    work, _ = first
    code = cli.main(["--workdir", str(work), "--require-ai", "evaluate", "report", "--run-id", "cycle1"],
                    environ={"OLLAMA_HOST": "http://ollama:11434"})
    assert code == exit_codes.OK


def test_the_report_needs_the_eval_lock(first, reported, capsys):
    work, _ = first
    with WorkdirLock(work / "eval"):
        assert cli.main(["--workdir", str(work), "evaluate", "report", "--run-id", "cycle1"], environ={}) == 5
    assert "in use" in capsys.readouterr().err


# --- prepare: usage, lock, signals, AI -------------------------------------------------------------


@pytest.mark.parametrize("args", [
    ["--variants", "full,fancy"], ["--variants", "random"], ["--variants", "full,full"], ["--run-id", "../x"],
    ["--duration", "5"], ["--seed", "-1"], ["--no-ai", "--variants", "full,no-ai"],
])
def test_invalid_options_of_prepare_are_usage_errors(vacation, tmp_path, args):
    media, _ = vacation
    assert cli.main(["--input", str(media), "--workdir", str(tmp_path / "work"), "evaluate", "prepare", *args],
                    environ={}) == exit_codes.USAGE


def test_a_prepared_run_is_not_overwritten_and_the_workdir_lock_is_respected(vacation, first, capsys):
    media, _ = vacation
    work, _ = first
    base = ["--input", str(media), "--workdir", str(work), "--no-ai", "evaluate", "prepare"]
    assert cli.main([*base, "--run-id", "cycle1"], environ={}) == exit_codes.USAGE
    assert "already prepared" in capsys.readouterr().err
    with WorkdirLock(work):
        assert cli.main([*base, "--run-id", "cycle9"], environ={}) == exit_codes.WORKDIR_IN_USE


def test_sigterm_during_prepare_exits_143_and_the_run_can_be_prepared_again(vacation, tmp_path, monkeypatch):
    media, _ = vacation
    work = tmp_path / "work"
    args = ["--input", str(media), "--workdir", str(work), "--no-ai", "evaluate", "prepare", "--duration", "15",
            "--run-id", "stopped"]

    def stop(*args, **kwargs):
        raise Interrupted(signal.SIGTERM)

    with monkeypatch.context() as patch:
        patch.setattr(prepare, "render_plan", stop)
        assert cli.main(args, environ={}) == 128 + signal.SIGTERM
    assert read(work / "status.json")["state"] == "interrupted"
    assert not (work / "eval" / "stopped" / PREPARE_NAME).exists()
    assert cli.main(args, environ={}) == exit_codes.OK
    assert (work / "eval" / "stopped" / PREPARE_NAME).exists()


def _closed_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_prepare_uses_the_vision_model_and_requires_it_when_asked(vacation, tmp_path, fake_ollama):
    media, _ = vacation
    base = ["--input", str(media), "--workdir", str(tmp_path / "work"), "evaluate", "prepare", "--duration", "15"]
    assert cli.main([*base, "--ollama-url", fake_ollama.url, "--variants", "full,no-ai", "--run-id", "ai"],
                    environ={}) == exit_codes.OK
    run_dir = tmp_path / "work" / "eval" / "ai"
    assert read(run_dir / PREPARE_NAME)["ai"]["used"] is True and fake_ollama.chat_requests
    moments = {clip["moment_type"] for variant in page_model(run_dir)["variants"] for clip in variant["clips"]}
    assert "scenery" in moments  # the answer of the simulated model, the same in both versions
    # Another workdir: in this one the answers are cached, and Ollama would not be needed.
    other = ["--input", str(media), "--workdir", str(tmp_path / "other"), "evaluate", "prepare", "--duration", "15"]
    code = cli.main([*other, "--ollama-url", _closed_url(), "--require-ai", "--run-id", "no-model"], environ={})
    assert code == exit_codes.REQUIREMENTS
    assert not (tmp_path / "other" / "eval" / "no-model" / PREPARE_NAME).exists()
