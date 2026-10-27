"""Tests with the real model. Run them with `docker compose up -d ollama`, then
`docker compose run --rm -T dev pytest -m ollama`. Synthetic images only; skipped when Ollama does not
answer or the model is missing."""

import json
import os

import pytest

from vacation_memories import cli, exit_codes
from vacation_memories import synthetic_media as sm
from vacation_memories.config import DEFAULT_MODEL, normalize_ollama_host
from vacation_memories.media_scanner import scan_media
from vacation_memories.ollama_client import OllamaClient
from vacation_memories.quality_analyzer import SegmentQuality
from vacation_memories.runtime_profile import DetectionEnv, detect_ollama, loaded_model_placement
from vacation_memories.technical_analysis import Candidate
from vacation_memories.vision_analyzer import NUM_PREDICT, VisionAnalyzer, analyze_candidates
from vacation_memories.vision_schema import CATEGORIES, MOMENT_TYPES, PROMPT, RESPONSE_SCHEMA, VisionResult

# main() changes process-wide state
pytestmark = [pytest.mark.ollama, pytest.mark.usefixtures("isolate_process_state")]

REAL_TIMEOUT_SECONDS = 600.0  # the first request loads the model, which on a CPU takes minutes
PLACEMENTS = {"gpu", "cpu", "mixed"}


@pytest.fixture(scope="module")
def ollama():
    url = normalize_ollama_host(os.environ.get("OLLAMA_HOST", "http://ollama:11434"))
    model = os.environ.get("VACATION_MEMORIES_MODEL") or DEFAULT_MODEL
    problems = []
    info = detect_ollama(DetectionEnv(), url, model, problems.append)
    if not info.reachable:
        pytest.skip(f"Ollama does not answer at {url}: start it with `docker compose up -d ollama`")
    if not info.model_present:
        pytest.skip(f"the model {model} is not installed in Ollama: run `docker compose up ollama-pull`")
    return url, model


@pytest.fixture(scope="module")
def synthetic_beach(tmp_path_factory):
    folder = tmp_path_factory.mktemp("synthetic")
    for scene in range(1, 4):
        sm.make_photo(folder / f"scene{scene}.jpg", scene, 640, 480)
    return folder


def test_the_answer_respects_the_schema(ollama, synthetic_beach):
    from vacation_memories.frame_sampler import photo_color_frame
    from vacation_memories.vision_analyzer import prepare_image

    url, model = ollama
    client = OllamaClient(url, model, timeout=REAL_TIMEOUT_SECONDS)
    encoded = prepare_image(photo_color_frame(synthetic_beach / "scene1.jpg"), 512)
    answer = json.loads(client.chat(prompt=PROMPT, image_b64=encoded, schema=RESPONSE_SCHEMA, num_predict=NUM_PREDICT))

    assert set(answer) == set(RESPONSE_SCHEMA["required"])
    assert isinstance(answer["description"], str) and answer["description"].strip()
    assert answer["category"] in CATEGORIES
    assert answer["moment_type"] in MOMENT_TYPES
    assert isinstance(answer["narrative_value"], int) and 1 <= answer["narrative_value"] <= 5
    assert isinstance(answer["people"], bool)


def test_the_phase_gets_answers_and_records_where_the_model_runs(ollama, synthetic_beach, tmp_path):
    url, model = ollama
    files = {file.rel_path: file for file in scan_media(synthetic_beach)}
    candidates = [
        Candidate(rel.as_posix(), rel, "photo", None, None,
                  SegmentQuality(0.5, 0.5, 0.5, 0.0, 0.8 - 0.1 * index, 1), None, None)
        for index, rel in enumerate(sorted(files))
    ]
    client = OllamaClient(url, model, timeout=REAL_TIMEOUT_SECONDS)
    with VisionAnalyzer(client, model=model, resolution=512) as analyzer:
        outcome = analyze_candidates(
            candidates, files, analyzer, frames=3, warmup_frames=2,
            probe_placement=lambda: loaded_model_placement(DetectionEnv(), url, model),
        )

    # A frame the model fails on is allowed (the phase goes on without it); the integrated GPU can be flaky.
    coverage = outcome.coverage
    assert outcome.results, f"no frame was analyzed: {coverage.to_dict()}"
    assert all(isinstance(result, VisionResult) for result in outcome.results.values())
    assert coverage.analyzed + coverage.failed == coverage.selected == 3
    assert coverage.seconds_per_frame > 0
    assert coverage.placement in PLACEMENTS


def test_bench_vision_reports_the_speed_and_the_placement(ollama, tmp_path, capsys):
    url, model = ollama
    code = cli.main(
        ["bench", "vision", "--workdir", str(tmp_path / "work"), "--ollama-url", url, "--allow-remote-ollama",
         "--model", model, "--count", "2"],
        environ={"VACATION_MEMORIES_AI_TIMEOUT": str(REAL_TIMEOUT_SECONDS)},
    )
    out = capsys.readouterr().out
    assert code == exit_codes.OK, out
    assert "first frame" in out
    profile = json.loads((tmp_path / "work" / "runtime_profile.json").read_text())
    assert profile["ollama"]["placement"] in PLACEMENTS
