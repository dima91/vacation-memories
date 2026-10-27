import base64
import io
import logging
from pathlib import Path

import numpy as np
import pytest
from fake_ollama import VALID_ANSWER, Reply
from PIL import Image
from system_fakes import MODEL

from vacation_memories import synthetic_media as sm
from vacation_memories.cache_manager import CacheManager
from vacation_memories.media_scanner import scan_media
from vacation_memories.ollama_client import OllamaClient
from vacation_memories.quality_analyzer import SegmentQuality
from vacation_memories.technical_analysis import Candidate
from vacation_memories.vision_analyzer import (
    MAX_CONSECUTIVE_FAILURES,
    VisionAnalyzer,
    analyze_candidates,
    prepare_image,
)
from vacation_memories.vision_schema import VisionResult


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_analyzer(server, cache=None, *, resolution=512, unload=True, model=MODEL, timeout=5.0) -> VisionAnalyzer:
    client = OllamaClient(server.url, model, timeout=timeout)
    return VisionAnalyzer(client, model=model, resolution=resolution, cache=cache, unload_model=unload)


def decode_sent_image(server, index=0) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(server.images_sent()[index])))


def candidate(rel: str, score: float, *, start=None, end=None) -> Candidate:
    quality = SegmentQuality(sharpness=score, exposure=score, contrast=score, black_fraction=0.0,
                             technical_score=score, frames=1)
    kind = "photo" if start is None else "video"
    cid = rel if start is None else f"{rel}#{start:.3f}-{end:.3f}"
    return Candidate(cid, Path(rel), kind, start, end, quality, None, np.zeros((1, 8), dtype=np.uint8))


class TestPrepareImage:
    def test_a_large_image_is_reduced_to_the_resolution_keeping_the_aspect_ratio(self):
        encoded = prepare_image(Image.new("RGB", (2000, 1000), (200, 30, 30)), 512)
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        assert image.size == (512, 256)
        assert image.format == "JPEG"

    def test_a_vertical_image_is_limited_on_its_long_side(self):
        image = Image.open(io.BytesIO(base64.b64decode(prepare_image(Image.new("RGB", (900, 1600)), 768))))
        assert image.size == (432, 768)

    def test_a_small_image_is_not_enlarged(self):
        image = Image.open(io.BytesIO(base64.b64decode(prepare_image(Image.new("RGB", (300, 200)), 512))))
        assert image.size == (300, 200)


class TestAnalyzeImage:
    def test_a_valid_answer_becomes_a_result(self, fake_ollama):
        result = make_analyzer(fake_ollama).analyze_image(Image.new("RGB", (800, 600)))
        assert result == VisionResult.from_response(VALID_ANSWER)

    def test_the_image_sent_respects_the_configured_resolution(self, fake_ollama):
        make_analyzer(fake_ollama, resolution=256).analyze_image(Image.new("RGB", (800, 600)))
        assert decode_sent_image(fake_ollama).size == (256, 192)

    def test_one_retry_after_a_failure(self, fake_ollama):
        fake_ollama.script.append(Reply(status=500))
        result = make_analyzer(fake_ollama).analyze_image(Image.new("RGB", (64, 64)))
        assert result is not None
        assert len(fake_ollama.chat_requests) == 2

    def test_no_more_than_one_retry(self, fake_ollama):
        fake_ollama.script.extend([Reply(status=500)] * 3)
        assert make_analyzer(fake_ollama).analyze_image(Image.new("RGB", (64, 64))) is None
        assert len(fake_ollama.chat_requests) == 2

    @pytest.mark.parametrize("content", ["not json {", '"just text"', "[1, 2]", ""])
    def test_an_unusable_answer_is_retried(self, fake_ollama, content):
        fake_ollama.script.append(Reply(content))
        result = make_analyzer(fake_ollama).analyze_image(Image.new("RGB", (64, 64)))
        assert result == VisionResult.from_response(VALID_ANSWER)
        assert len(fake_ollama.chat_requests) == 2

    def test_a_timeout_is_retried(self, fake_ollama):
        fake_ollama.script.append(Reply(VALID_ANSWER, delay=0.5))
        result = make_analyzer(fake_ollama, timeout=0.2).analyze_image(Image.new("RGB", (64, 64)))
        assert result is not None
        assert len(fake_ollama.chat_requests) == 2

    def test_an_invalid_field_is_null_and_does_not_cost_a_retry(self, fake_ollama):
        fake_ollama.script.append(Reply({**VALID_ANSWER, "moment_type": "party", "narrative_value": 9}))
        result = make_analyzer(fake_ollama).analyze_image(Image.new("RGB", (64, 64)))
        assert (result.moment_type, result.narrative_value) == (None, None)
        assert result.category == "sea"
        assert len(fake_ollama.chat_requests) == 1

    def test_the_cache_parameters_name_model_prompt_and_resolution(self, fake_ollama):
        params = make_analyzer(fake_ollama, resolution=640).params
        assert MODEL in params and "640" in params
        assert params != make_analyzer(fake_ollama, resolution=512).params
        assert params != make_analyzer(fake_ollama, resolution=640, model="other:latest").params


class TestUnload:
    def test_leaving_the_context_unloads_the_model(self, fake_ollama):
        with make_analyzer(fake_ollama) as analyzer:
            analyzer.analyze_image(Image.new("RGB", (64, 64)))
            assert fake_ollama.loaded
        assert fake_ollama.unload_requests == 1

    def test_unloading_can_be_turned_off(self, fake_ollama):
        with make_analyzer(fake_ollama, unload=False) as analyzer:
            analyzer.analyze_image(Image.new("RGB", (64, 64)))
        assert fake_ollama.unload_requests == 0

    def test_a_failed_unload_is_only_a_warning(self, fake_ollama, caplog):
        analyzer = make_analyzer(fake_ollama)
        analyzer.client.url = "http://127.0.0.1:1"
        with caplog.at_level(logging.WARNING):
            analyzer.close()
        assert "unload" in caplog.text


@pytest.fixture
def photos(tmp_path):
    """Ten photos; `scores` ranks them so that the best ones are not the first in file order."""
    root = tmp_path / "media"
    root.mkdir()
    for index in range(10):
        sm.make_photo(root / f"p{index}.jpg", index + 1, 160, 120)
    return root


@pytest.fixture
def media(photos):
    return {file.rel_path: file for file in scan_media(photos)}


SCORES = {"p3": 0.95, "p7": 0.9, "p0": 0.85, "p9": 0.8, "p5": 0.75, "p1": 0.7, "p8": 0.65, "p2": 0.6, "p6": 0.55,
          "p4": 0.5}
BY_PRIORITY = [f"{name}.jpg" for name in SCORES]


@pytest.fixture
def candidates():
    return [candidate(f"{name}.jpg", score) for name, score in sorted(SCORES.items())]


class TestPhase:
    def test_candidates_are_analyzed_best_score_first(self, fake_ollama, media, candidates):
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=4)
        assert set(outcome.results) == set(BY_PRIORITY[:4])

    def test_a_fixed_number_of_frames_gives_the_same_frames_on_a_fast_and_a_slow_machine(self, fake_ollama, media,
                                                                                         candidates):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(30.0), Reply(VALID_ANSWER))[1]
        slow = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=5, clock=clock)
        fast = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=5, clock=FakeClock())
        assert set(slow.results) == set(fast.results) == set(BY_PRIORITY[:5])
        assert slow.coverage.seconds_per_frame == pytest.approx(30.0)

    def test_the_time_budget_gives_as_many_frames_as_the_measured_speed_allows(self, fake_ollama, media, candidates):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(2.0), Reply(VALID_ANSWER))[1]
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), time_budget=10.0, clock=clock)
        assert outcome.coverage.seconds_per_frame == pytest.approx(2.0)
        assert set(outcome.results) == set(BY_PRIORITY[:5])  # 10 s / 2 s per frame
        assert outcome.coverage.skipped_by_budget == 5
        assert len(fake_ollama.chat_requests) == 5

    def test_a_faster_machine_analyzes_more_frames_with_the_same_budget(self, fake_ollama, media, candidates):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(1.0), Reply(VALID_ANSWER))[1]
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), time_budget=7.0, clock=clock)
        assert len(outcome.results) == 7

    def test_a_budget_that_covers_everything_analyzes_every_candidate(self, fake_ollama, media, candidates):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(1.0), Reply(VALID_ANSWER))[1]
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), time_budget=1000.0, clock=clock)
        assert len(outcome.results) == 10 and outcome.coverage.skipped_by_budget == 0

    def test_the_default_budget_applies_when_neither_option_is_given(self, fake_ollama, media, candidates):
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama))
        assert len(outcome.results) == 10
        assert outcome.coverage.mode == "time_budget"

    def test_frames_and_time_budget_exclude_each_other(self, fake_ollama, media, candidates):
        with pytest.raises(ValueError, match="exclusive"):
            analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=3, time_budget=60.0)

    def test_the_warm_up_is_short_and_measures_the_speed_before_the_budget_is_spent(self, fake_ollama, media,
                                                                                    candidates):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(100.0), Reply(VALID_ANSWER))[1]
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), time_budget=1.0, clock=clock)
        assert len(fake_ollama.chat_requests) == 3  # only the warm-up frames
        assert len(outcome.results) == 3

    def test_the_model_placement_is_read_once_after_the_warm_up(self, fake_ollama, media, candidates):
        seen = []

        def probe():
            seen.append(len(fake_ollama.chat_requests))
            return "gpu"

        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=8, probe_placement=probe)
        assert seen == [3]
        assert outcome.coverage.placement == "gpu"

    def test_the_placement_stays_unknown_when_nothing_could_be_analyzed(self, fake_ollama, media, candidates):
        fake_ollama.on_chat = lambda body: Reply(status=500)
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), probe_placement=lambda: "gpu")
        assert outcome.coverage.placement == "unknown"

    def test_a_probe_that_fails_leaves_the_placement_unknown(self, fake_ollama, media, candidates):
        def probe():
            raise OSError("no route")

        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=4, probe_placement=probe)
        assert outcome.coverage.placement == "unknown"
        assert len(outcome.results) == 4

    def test_one_request_at_a_time_by_default(self, fake_ollama, media, candidates):
        fake_ollama.delay = 0.03
        analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=8)
        assert fake_ollama.max_in_flight == 1

    def test_more_concurrency_only_when_asked_for(self, fake_ollama, media, candidates):
        fake_ollama.delay = 0.1
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=10, max_concurrency=3)
        assert 1 < fake_ollama.max_in_flight <= 3
        assert len(outcome.results) == 10

    def test_progress_is_reported_up_to_the_total(self, fake_ollama, media, candidates):
        reports = []
        analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=6,
                           progress=lambda done, total: reports.append((done, total)))
        assert reports[-1] == (6, 6)
        assert [done for done, _ in reports] == sorted(done for done, _ in reports)


class TestCache:
    def test_a_second_run_asks_the_model_nothing(self, fake_ollama, media, candidates, tmp_path):
        with CacheManager(tmp_path / "work") as cache:
            first = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=6)
            asked = len(fake_ollama.chat_requests)
            second = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=6)
        assert asked == 6
        assert len(fake_ollama.chat_requests) == 6
        assert second.results == first.results
        assert second.coverage.from_cache == 6

    def test_cached_frames_cost_no_budget(self, fake_ollama, media, candidates, tmp_path):
        clock = FakeClock()
        fake_ollama.on_chat = lambda body: (clock.advance(2.0), Reply(VALID_ANSWER))[1]
        with CacheManager(tmp_path / "work") as cache:
            analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=3, clock=clock)
            outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), time_budget=10.0,
                                         clock=clock)
        assert outcome.coverage.from_cache == 3
        assert len(outcome.results) == 3 + 5  # three free, then as many as 10 s allow

    def test_a_different_resolution_or_model_asks_again(self, fake_ollama, media, candidates, tmp_path):
        with CacheManager(tmp_path / "work") as cache:
            analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=3)
            analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache, resolution=768), frames=3)
            analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache, model="other:latest"), frames=3)
        assert len(fake_ollama.chat_requests) == 9

    def test_failures_are_not_cached(self, fake_ollama, media, candidates, tmp_path):
        with CacheManager(tmp_path / "work") as cache:
            fake_ollama.script.extend([Reply(status=500)] * 2)
            first = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=1)
            assert first.results == {}
            second = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=1)
        assert len(second.results) == 1

    def test_an_answer_with_null_fields_is_cached_as_it_is(self, fake_ollama, media, candidates, tmp_path):
        fake_ollama.script.append(Reply({**VALID_ANSWER, "moment_type": "party"}))
        with CacheManager(tmp_path / "work") as cache:
            first = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=1)
            second = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=1)
        assert first.results == second.results
        assert next(iter(second.results.values())).moment_type is None
        assert len(fake_ollama.chat_requests) == 1

    def test_a_clip_frame_is_cached_by_its_timestamp(self, fake_ollama, tmp_path):
        root = tmp_path / "media"
        root.mkdir()
        sm.make_video(root / "clip.mp4", [(1, 2.0), (2, 2.0)], 160, 120, fps=10)
        files = {file.rel_path: file for file in scan_media(root)}
        clip = [candidate("clip.mp4", 0.9, start=0.0, end=2.0), candidate("clip.mp4", 0.8, start=2.0, end=4.0)]
        with CacheManager(tmp_path / "work") as cache:
            analyzer = make_analyzer(fake_ollama, cache)
            outcome = analyze_candidates(clip, files, analyzer, frames=2)
            media_file = files[Path("clip.mp4")]
            assert cache.get_vision(media_file, 1.0, analyzer.params) is not None  # middle of the segment
            assert cache.get_vision(media_file, 3.0, analyzer.params) is not None
        assert len(outcome.results) == 2
        first, second = (decode_sent_image(fake_ollama, i) for i in range(2))
        assert first.size == (160, 120)
        assert np.abs(np.asarray(first, float) - np.asarray(second, float)).mean() > 10  # two different scenes


class TestFailures:
    def test_a_frame_the_model_cannot_answer_is_left_out_and_the_others_continue(self, fake_ollama, media, candidates):
        fake_ollama.script.extend([Reply(status=500)] * 2)  # both attempts on the best frame
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=4)
        assert set(outcome.results) == set(BY_PRIORITY[1:4])
        assert outcome.coverage.failed == 1
        assert not outcome.coverage.aborted

    def test_an_unreadable_file_is_skipped_without_asking_the_model(self, fake_ollama, photos, candidates):
        (photos / "p3.jpg").write_bytes(b"not an image")
        files = {file.rel_path: file for file in scan_media(photos)}
        outcome = analyze_candidates(candidates, files, make_analyzer(fake_ollama), frames=4)
        assert "p3.jpg" not in outcome.results
        assert len(outcome.results) == 3
        assert len(fake_ollama.chat_requests) == 3
        assert outcome.coverage.failed == 1

    def test_unreadable_files_do_not_count_as_a_model_outage(self, fake_ollama, photos, candidates):
        for name in BY_PRIORITY[:MAX_CONSECUTIVE_FAILURES + 1]:
            (photos / name).write_bytes(b"not an image")
        files = {file.rel_path: file for file in scan_media(photos)}
        outcome = analyze_candidates(candidates, files, make_analyzer(fake_ollama), frames=10)
        assert not outcome.coverage.aborted
        assert len(outcome.results) == 10 - (MAX_CONSECUTIVE_FAILURES + 1)

    def test_a_model_that_stops_answering_ends_the_phase_instead_of_waiting_for_every_frame(self, fake_ollama, media,
                                                                                           candidates, caplog):
        fake_ollama.on_chat = lambda body: Reply(status=500)
        with caplog.at_level(logging.WARNING):
            outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=10)
        assert outcome.results == {}
        assert outcome.coverage.aborted
        assert len(fake_ollama.chat_requests) == MAX_CONSECUTIVE_FAILURES * 2  # two attempts per frame
        assert outcome.coverage.failed == MAX_CONSECUTIVE_FAILURES
        assert outcome.coverage.skipped_after_failures == 10 - MAX_CONSECUTIVE_FAILURES
        assert "stopping" in caplog.text

    def test_a_success_resets_the_count_of_consecutive_failures(self, fake_ollama, media, candidates):
        pattern = iter([500, 500, 200, 500, 500, 200, 500, 500])  # frames 1, 3 and 5 fail on both attempts
        fake_ollama.on_chat = lambda body: Reply(VALID_ANSWER, status=next(pattern, 200))
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=5)
        assert not outcome.coverage.aborted

    def test_nothing_to_analyze_is_not_an_error(self, fake_ollama, media):
        outcome = analyze_candidates([], media, make_analyzer(fake_ollama), frames=5)
        assert outcome.results == {} and outcome.coverage.candidates == 0
        assert fake_ollama.chat_requests == []


class TestCoverage:
    def test_the_counts_and_the_record_for_the_plan(self, fake_ollama, media, candidates, tmp_path):
        with CacheManager(tmp_path / "work") as cache:
            analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=2)
            outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama, cache), frames=6,
                                         probe_placement=lambda: "mixed")
        record = outcome.coverage.to_dict()
        assert record["model"] == MODEL
        assert record["mode"] == "frames"
        assert record["candidates"] == 10
        assert record["selected"] == 6
        assert record["analyzed"] == 6
        assert record["from_cache"] == 2
        assert record["failed"] == 0
        assert record["placement"] == "mixed"
        assert record["aborted"] is False


class TestStop:
    def test_no_frame_is_sent_once_the_job_is_stopping(self, fake_ollama, media, candidates):
        sent = []
        fake_ollama.on_chat = lambda body: (sent.append(1), Reply(VALID_ANSWER))[1]
        outcome = analyze_candidates(candidates, media, make_analyzer(fake_ollama), frames=10,
                                     stop=lambda: len(sent) >= 4)
        assert len(fake_ollama.chat_requests) == 4
        assert len(outcome.results) == 4 and outcome.coverage.failed == 0

    def test_an_aborted_request_is_not_retried_nor_counted_as_a_failure(self, fake_ollama, media, candidates):
        analyzer = make_analyzer(fake_ollama)
        analyzer.client.abort()
        outcome = analyze_candidates(candidates, media, analyzer, frames=3)
        assert fake_ollama.chat_requests == [] and outcome.results == {}
        assert outcome.coverage.failed == 0 and not outcome.coverage.aborted
        analyzer.close()
        assert fake_ollama.unload_requests == 0  # nothing to unload from a job that is stopping
