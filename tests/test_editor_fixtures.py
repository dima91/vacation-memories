"""The JSON fixtures the interface is checked against (`tests/fixtures/editor/`), and their agreement with the server.

The fixtures are real answers of the editor on a small synthetic vacation: the pipeline runs as a job, a few edits
are saved the way the interface will, and the responses are written down. `npm run check` and `npm test` use them
for the plan types and for the components rendered on fixture data. This test builds the same answers again and
compares their *shape* (keys and types, not values), so a change of the API that the interface does not know about
fails here.

Regenerate them, after a deliberate change of the API:
    docker compose run --rm -T dev python tests/test_editor_fixtures.py --write
"""

import json
import math
import struct
import sys
import time
import wave
from pathlib import Path

import pytest
from editor_fakes import RunningEditor, editor_settings

from vacation_memories.editor.server import EditorApp
from vacation_memories.evaluate.synthetic_media import populate_vacation

FIXTURES = Path(__file__).parent / "fixtures" / "editor"
TRACK = "brano.wav"
FOLDER = "vacation"
PLAN_FIXTURES = ("plan_document.json", "plan_v2.json")


def write_track(path: Path, seconds: float = 20.0) -> None:
    rate = 8000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * index / rate) * (0.4 + 0.6 * abs(math.sin(index / 4000)))))
            for index in range(int(seconds * rate))))


def wait_for_job(client, project: str, seconds: float = 180) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _, _, status = client.get(f"/api/projects/{project}/status")
        if status["job"] and status["job"]["state"] not in ("running", "stopping"):
            return status
        time.sleep(0.5)
    raise AssertionError("the job did not finish")


def live_documents(root: Path) -> dict[str, object]:
    """Every fixture, built from the live server on a synthetic vacation under `root`."""
    media, work, music = root / "media", root / "work", root / "music"
    for folder in (media, work, music):
        folder.mkdir()
    populate_vacation(media / FOLDER, photos=16, videos=2, days=2, video_scenes=(4.0, 4.0, 4.0), blurred=2,
                      size=(320, 240))
    write_track(music / TRACK)
    app = EditorApp(editor_settings(media, work, music))
    with RunningEditor(app) as editor:
        client = editor.client
        documents: dict[str, object] = {}
        documents["folders.json"] = client.get("/api/folders")[2]
        _, _, opened = client.post("/api/projects", {"folder": FOLDER})
        project = opened["id"]
        documents["project_new.json"] = client.get(f"/api/projects/{project}")[2]
        assert client.put(f"/api/projects/{project}/settings", {"duration": "0:30"})[0] == 200
        status, _, job = client.post(f"/api/projects/{project}/jobs", {"type": "plan", "ai": False})
        assert status == 202, job
        finished = wait_for_job(client, project)
        assert finished["job"]["state"] == "done", finished
        documents["status_done.json"] = finished

        _, _, first = client.get(f"/api/projects/{project}/plan")
        plan, revision = first["plan"], first["revision"]
        photo = next(element for element in plan["elements"] if element["type"] == "photo")
        photo.update(duration=4.5, duration_origin="user")
        clip = next(element for element in plan["elements"] if element["type"] == "video")
        clip["effects"] = [*clip["effects"], {"id": "user-dip-1", "type": "dip", "offset": 1.0, "duration": 0.6,
                                              "origin": "user"}]
        hidden = next(item for item in client.get(f"/api/projects/{project}/catalog")[2]["photos"]
                      if item["status"] in ("available", "discarded"))
        plan["excluded"] = [{"source": hidden["source"], "start": None, "end": None, "reason": "user"}]
        plan["music"] = {"tracks": [{"file": TRACK, "parts": [
            {"start": 0.0, "end": 12.0, "muted": False, "fade_in": 1.0, "fade_out": 0.0},
            {"start": 12.0, "end": 20.0, "muted": True, "fade_in": 0.0, "fade_out": 2.0}]}], "offset": 0.0}
        status, _, saved = client.put(f"/api/projects/{project}/plan", plan, headers={"If-Match": revision})
        assert status == 200, saved
        documents["plan_document.json"] = saved
        documents["plan_v2.json"] = saved["plan"]
        documents["project_planned.json"] = client.get(f"/api/projects/{project}")[2]
        documents["catalog.json"] = client.get(f"/api/projects/{project}/catalog")[2]
        track = documents["catalog.json"]["music"]["tracks"][0]["id"]
        documents["waveform.json"] = client.get(f"/api/projects/{project}/waveform/{track}")[2]

        # A project with its videos: rendered without the music, which the child process of the test has no folder for.
        silent_plan = {key: value for key, value in saved["plan"].items() if key != "music"}
        status, _, silent = client.put(f"/api/projects/{project}/plan", silent_plan, headers={"If-Match": saved["revision"]})
        assert status == 200, silent
        for kind in ("preview", "export"):
            assert client.post(f"/api/projects/{project}/jobs", {"type": kind, "name": "memories"})[0] == 202
            assert wait_for_job(client, project)["job"]["state"] == "done"
        documents["project_rendered.json"] = client.get(f"/api/projects/{project}")[2]
    return documents


# Dictionaries whose keys are ids or free names: their entries are compared by the first one. `runtime` is the
# planner's own report: it is only checked to be a dictionary.
KEYED_BY_ID = ("limits.elements", ".effects", "refs", ".components")
OPAQUE = ("plan.runtime", ".runtime")


def assert_same_shape(live, stored, where=""):
    if isinstance(stored, dict) and isinstance(live, dict):
        if where.endswith(OPAQUE):
            return
        if where.endswith(KEYED_BY_ID) and not where.endswith("plan.elements[].effects"):
            assert bool(live) == bool(stored), where
            if live and stored:
                assert_same_shape(next(iter(live.values())), next(iter(stored.values())), f"{where}.<id>")
            return
        assert live.keys() == stored.keys(), f"{where}: {sorted(live)} != {sorted(stored)}"
        for key in live:
            assert_same_shape(live[key], stored[key], f"{where}.{key}")
    elif isinstance(stored, list) and isinstance(live, list):
        if stored and live:
            assert_same_shape(live[0], stored[0], f"{where}[]")
    elif stored is not None and live is not None:
        assert type(live) is type(stored) or {type(live), type(stored)} <= {int, float}, f"{where}: {live!r} vs {stored!r}"
    else:
        assert (live is None) == (stored is None) or where.endswith(NULLABLE), f"{where}: {live!r} vs {stored!r}"


# Fields that are null in some answers and filled in others, depending on the synthetic material.
NULLABLE = ("duration_origin", "detail", "group", "discard", "final", "narrative_score", "components", "moment_type",
            "category", "narrative_value", "description", "end_max", "ai", "coverage", "job", "exit", "name",
            "finished_at", "revision", "generated_at", "error", "start", "end", "percent", "phase", "done", "total",
            "eta_seconds", "started_at", "updated_at", "requested_duration", "planned_duration", "assumed_tz",
            "assume_tz", "ai_frames", "music", "duration", "trim", "modified", "plan_revision")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    return live_documents(tmp_path_factory.mktemp("fixtures"))


@pytest.mark.parametrize("name", ["folders.json", "project_new.json", "project_planned.json", "status_done.json",
                                  "plan_document.json", "plan_v2.json", "catalog.json", "waveform.json",
                                  "project_rendered.json"])
def test_the_stored_fixture_has_the_shape_of_the_live_answer(live, name):
    stored = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert_same_shape(live[name], stored, name.removesuffix(".json"))


def test_the_fixtures_name_no_host_path():
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "/tmp" not in text and "/work/" not in text and "/media/" not in text, path.name


def test_the_plan_fixture_is_a_valid_v2_plan():
    from vacation_memories.planning.edit_plan import plan_from_dict

    plan = plan_from_dict(json.loads((FIXTURES / "plan_v2.json").read_text(encoding="utf-8")))
    assert plan.schema_version == 2 and plan.music is not None and len(plan.excluded) == 1
    assert any(effect.origin == "user" for element in plan.elements for effect in element.effects)


if __name__ == "__main__":
    if "--write" not in sys.argv:
        sys.exit(__doc__)
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        documents = live_documents(Path(scratch))
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        (FIXTURES / name).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote", FIXTURES / name)
