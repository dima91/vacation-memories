"""The editor's API on a synthetic vacation: projects, plan, catalog, media, jobs, videos.

The jobs here are the real CLI as child processes (no AI, a short video); the cases that need a job to
wait, fail or be stopped use `fake_job.py` in its place.
"""

import json
import os
import shutil
import sys
import time
import wave
from pathlib import Path

import pytest
from editor_fakes import RunningEditor, editor_settings, make_media

from vacation_memories.editor.ids import opaque_id
from vacation_memories.editor.projects import project_id
from vacation_memories.editor.server import EditorApp

FAKE_JOB = Path(__file__).parent / "fake_job.py"
QUICK_SETTINGS = {"duration": "15", "resolution": "480p", "ai": False}


def wait_for_job(client, project, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, body = client.get(f"/api/projects/{project}/status")
        assert status == 200
        if body["job"] and body["job"]["state"] not in ("running", "stopping"):
            return body
        time.sleep(0.3)
    raise AssertionError("the job did not finish")


def start_sleeping(client, work, project, job):
    """Start a stand-in job that waits, and return once it has installed its signal handler (`ready`)."""
    marker = work / "projects" / project / "ready"
    marker.unlink(missing_ok=True)
    status, _, started = client.post(f"/api/projects/{project}/jobs", job)
    assert status == 202, started
    deadline = time.monotonic() + 20
    while not marker.exists():
        assert time.monotonic() < deadline, "the stand-in job did not start"
        time.sleep(0.05)


def write_tone(path: Path, seconds=3.0, rate=8000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join((int(12000 * ((n // 20) % 2 * 2 - 1))).to_bytes(2, "little", signed=True)
                                    for n in range(int(seconds * rate))))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    media = make_media(tmp_path_factory.mktemp("api-media"))
    work = tmp_path_factory.mktemp("api-work")
    music = tmp_path_factory.mktemp("api-music")
    write_tone(music / "album" / "tone.wav")
    return media, work, music


@pytest.fixture(scope="module")
def editor(world):
    media, work, music = world
    with RunningEditor(EditorApp(editor_settings(media, work, music), ai_check=lambda: {
            "available": False, "reachable": False, "model": "test-model", "model_present": False,
            "reason": "ollama_unreachable"})) as running:
        yield running


@pytest.fixture(scope="module")
def trip(editor):
    """The project of `trip/`, analyzed and planned by a real job."""
    client = editor.client
    status, _, opened = client.post("/api/projects", {"folder": "trip"})
    assert status == 201
    project = opened["id"]
    assert client.put(f"/api/projects/{project}/settings", QUICK_SETTINGS)[0] == 200
    status, _, job = client.post(f"/api/projects/{project}/jobs", {"type": "plan"})
    assert status == 202 and job["type"] == "plan"
    done = wait_for_job(client, project)
    assert done["job"]["state"] == "done", done
    return project


class TestFoldersAndProjects:
    def test_the_folders_come_with_their_counts_and_state(self, editor):
        status, _, body = editor.client.get("/api/folders")
        folders = {folder["path"]: folder for folder in body["folders"]}
        assert status == 200 and folders["trip"]["photos"] == 10 and folders["other"]["photos"] == 2

    def test_a_folder_opens_a_project_and_again_the_same_one(self, editor):
        first = editor.client.post("/api/projects", {"folder": "other"})
        again = editor.client.post("/api/projects", {"folder": "other"})
        assert first[2]["id"] == again[2]["id"] == project_id("other")
        assert again[0] == 200

    @pytest.mark.parametrize("folder", ["../x", "/etc", "nowhere", 7, None])
    def test_a_bad_folder_is_refused(self, editor, folder):
        status, _, body = editor.client.post("/api/projects", {"folder": folder})
        assert status in (400, 404) and "error" in body

    def test_the_state_of_a_new_project(self, editor):
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        _, _, body = editor.client.get(f"/api/projects/{project}")
        assert body["plan"]["present"] is False and body["analysis"]["present"] is False
        assert body["analysis"]["files"]["total"] == 2
        assert body["job"] is None and body["busy"] is False and body["exports"] == []
        assert body["preview"] == {"present": False, "name": None, "modified": None, "outdated": False}
        assert body["settings"]["auto_preview"] is True

    def test_settings_errors_name_the_field(self, editor):
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        status, _, body = editor.client.put(f"/api/projects/{project}/settings", {"duration": "soon"})
        assert status == 400 and body["error"]["field"] == "duration"
        status, _, body = editor.client.put(f"/api/projects/{project}/settings", {"duration": "1:00", "nice": 3})
        assert status == 200 and body["settings"]["duration"] == "1:00"


class TestAnalysisAndPlanJob:
    def test_the_job_ends_with_the_plan_and_the_candidates(self, editor, trip):
        _, _, state = editor.client.get(f"/api/projects/{trip}")
        assert state["plan"]["present"] and state["plan"]["valid"] and state["plan"]["edited"] is False
        assert state["analysis"]["present"] and state["analysis"]["complete"]
        assert state["analysis"]["files"] == {"total": 11, "analyzed": 11, "new_or_changed": 0, "removed": 0}
        assert state["job"]["exit"]["code"] == 0 and state["job"]["exit"]["key"] == "done"

    def test_the_status_has_the_progress_without_host_details(self, editor, trip):
        _, _, status = editor.client.get(f"/api/projects/{trip}/status")
        assert status["progress"]["state"] == "done"
        assert "pid" not in status["progress"] and "hostname" not in status["progress"]

    def test_the_plan_comes_with_its_revision_and_limits(self, editor, trip):
        status, _, body = editor.client.get(f"/api/projects/{trip}/plan")
        plan = body["plan"]
        assert status == 200 and plan["schema_version"] == 2 and len(body["revision"]) == 16
        assert set(body["limits"]["elements"]) == {element["id"] for element in plan["elements"]}
        assert body["duration"] == pytest.approx(plan["planned_duration"], abs=0.1)

    def test_the_catalog_has_the_material_and_what_is_selected(self, editor, trip):
        _, _, plan = editor.client.get(f"/api/projects/{trip}/plan")
        status, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        assert status == 200 and catalog["revision"] == plan["revision"]
        selected = {item["id"] for item in catalog["photos"] if item["status"] == "selected"}
        selected |= {moment["id"] for video in catalog["videos"] for moment in video["moments"]
                     if moment["status"] == "selected"}
        assert selected == {element["id"] for element in plan["plan"]["elements"]}
        assert len(catalog["photos"]) == 10 and len(catalog["videos"]) == 1
        assert {item["type"] for item in catalog["transitions"]} == {"fade_in", "fade_out", "dissolve", "dip"}
        assert [track["file"] for track in catalog["music"]["tracks"]] == ["album/tone.wav"]


class TestSavingThePlan:
    def modified(self, plan, **changes):
        copy = json.loads(json.dumps(plan))
        photo = next(element for element in copy["elements"] if element["type"] == "photo")
        photo.update(changes)
        return copy, photo

    def test_it_needs_the_revision_that_was_read(self, editor, trip):
        _, _, body = editor.client.get(f"/api/projects/{trip}/plan")
        status, _, error = editor.client.put(f"/api/projects/{trip}/plan", body["plan"])
        assert status == 428 and error["error"]["code"] == "if_match_required"

    def test_a_valid_edit_is_saved_and_gets_a_new_revision(self, editor, trip):
        _, _, body = editor.client.get(f"/api/projects/{trip}/plan")
        plan, photo = self.modified(body["plan"], duration=4.0, duration_origin="user")
        status, _, saved = editor.client.put(f"/api/projects/{trip}/plan", plan, {"If-Match": body["revision"]})
        assert status == 200 and saved["revision"] != body["revision"]
        assert next(item for item in saved["plan"]["elements"] if item["id"] == photo["id"])["duration"] == 4.0
        _, _, again = editor.client.get(f"/api/projects/{trip}/plan")
        assert again["revision"] == saved["revision"]
        assert again["duration"] == pytest.approx(saved["duration"])
        _, _, state = editor.client.get(f"/api/projects/{trip}")
        assert state["plan"]["edited"] is True

    def test_an_outdated_revision_is_a_409_with_the_current_one(self, editor, trip):
        _, _, current = editor.client.get(f"/api/projects/{trip}/plan")
        plan, _ = self.modified(current["plan"], duration=5.0, duration_origin="user")
        status, _, error = editor.client.put(f"/api/projects/{trip}/plan", plan, {"If-Match": "0" * 16})
        assert status == 409 and error["error"]["code"] == "revision_conflict"
        assert error["error"]["revision"] == current["revision"]

    def test_an_invalid_plan_gets_a_message_and_the_path_of_the_field(self, editor, trip):
        _, _, current = editor.client.get(f"/api/projects/{trip}/plan")
        plan, photo = self.modified(current["plan"], duration=0.2, duration_origin="user")
        index = [element["id"] for element in plan["elements"]].index(photo["id"])
        status, _, error = editor.client.put(f"/api/projects/{trip}/plan", plan, {"If-Match": current["revision"]})
        assert status == 400 and error["error"]["code"] == "invalid_plan"
        assert error["error"]["field"].startswith(f"elements[{index}]")
        assert "1" in error["error"]["message"] and error["error"]["violations"]
        _, _, after = editor.client.get(f"/api/projects/{trip}/plan")
        assert after["revision"] == current["revision"]  # nothing was written

    def test_a_plan_with_an_unknown_key_is_rejected_with_its_place(self, editor, trip):
        _, _, current = editor.client.get(f"/api/projects/{trip}/plan")
        plan, _ = self.modified(current["plan"], colour="red")
        status, _, error = editor.client.put(f"/api/projects/{trip}/plan", plan, {"If-Match": current["revision"]})
        assert status == 400 and "colour" in error["error"]["message"] and error["error"]["field"]

    def test_overwriting_keeps_the_replaced_plan_in_the_history(self, editor, trip, world):
        _, work, _ = world
        _, _, current = editor.client.get(f"/api/projects/{trip}/plan")
        plan, photo = self.modified(current["plan"], duration=6.0, duration_origin="user")
        status, _, _ = editor.client.put(f"/api/projects/{trip}/plan", plan, {"If-Match": "*"})
        assert status == 200
        history = list((work / "projects" / trip / "plan_history").glob("edit_plan-*.json"))
        assert history and json.loads(history[-1].read_text())["elements"] != plan["elements"]

    def test_a_plan_that_does_not_exist_is_a_404(self, editor):
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        assert editor.client.get(f"/api/projects/{project}/plan")[2]["error"]["code"] == "no_plan"


class TestPlansOfTheCli:
    def test_a_schema_1_plan_is_read_as_schema_2_and_saved_as_such(self, editor, world):
        _, work, _ = world
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        plan = {"schema_version": 1, "requested_duration": 30, "planned_duration": 7.0,
                "output": {"width": 854, "height": 480, "fps": 30},
                "elements": [{"source": "IMG_0.jpg", "type": "photo", "duration": 4.0,
                              "transition_in": {"type": "cut", "duration": 0}},
                             {"source": "IMG_1.jpg", "type": "photo", "duration": 4.0,
                              "transition_in": {"type": "dissolve", "duration": 0.5}}]}
        path = work / "projects" / project / "edit_plan.json"
        path.write_text(json.dumps(plan))
        status, _, body = editor.client.get(f"/api/projects/{project}/plan")
        assert status == 200 and body["plan"]["schema_version"] == 2
        # the dissolve is stored in the incoming clip, and the global fade to black is an effect of the last one
        assert [effect["type"] for effect in body["plan"]["elements"][1]["effects"]] == ["dissolve", "fade_out"]
        assert body["duration"] == pytest.approx(7.5, abs=0.1)
        status, _, saved = editor.client.put(f"/api/projects/{project}/plan", body["plan"],
                                             {"If-Match": body["revision"]})
        assert status == 200 and json.loads(path.read_text())["schema_version"] == 2

    def test_a_plan_on_disk_that_is_not_valid_says_so(self, editor, world):
        _, work, _ = world
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        (work / "projects" / project / "edit_plan.json").write_text(json.dumps({"schema_version": 9}))
        status, _, body = editor.client.get(f"/api/projects/{project}/plan")
        assert status == 422 and body["error"]["code"] == "invalid_plan"
        assert editor.client.get(f"/api/projects/{project}")[2]["plan"]["valid"] is False
        (work / "projects" / project / "edit_plan.json").unlink()


class TestMedia:
    def test_a_thumbnail_of_a_photo_and_of_a_moment(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        photo = catalog["photos"][0]
        status, headers, data = editor.client.get(f"/api/projects/{trip}/thumb/{photo['media']}")
        assert status == 200 and headers["content-type"] == "image/jpeg" and data[:2] == b"\xff\xd8"
        moment = catalog["videos"][0]["moments"][0]
        status, headers, data = editor.client.get(
            f"/api/projects/{trip}/thumb/{moment['media']}?t={moment['thumb_time']}")
        assert status == 200 and data[:2] == b"\xff\xd8"

    @pytest.mark.parametrize("media", ["IMG_0001.jpg", "0" * 16, "../../etc/passwd", "G" * 16])
    def test_an_unknown_media_id_is_a_404(self, editor, trip, media):
        assert editor.client.get(f"/api/projects/{trip}/thumb/{media}")[0] == 404

    def test_the_time_of_a_thumbnail_must_be_a_number(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        video = catalog["videos"][0]["media"]
        assert editor.client.get(f"/api/projects/{trip}/thumb/{video}?t=soon")[0] == 400

    def test_the_waveform_of_a_track(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        track = catalog["music"]["tracks"][0]
        status, headers, body = editor.client.get(f"/api/projects/{trip}/waveform/{track['id']}")
        assert status == 200 and headers["content-type"].startswith("application/json")
        assert body["duration"] == pytest.approx(3.0, abs=0.1) and 0 < len(body["peaks"]) <= 800
        assert max(body["peaks"]) == pytest.approx(0.366, abs=0.02)  # 12000 / 32768
        assert editor.client.get(f"/api/projects/{trip}/waveform/{'0' * 16}")[0] == 404

    def test_the_preview_of_a_clip_streams_with_range(self, editor, trip):
        _, _, plan = editor.client.get(f"/api/projects/{trip}/plan")
        element = plan["plan"]["elements"][0]
        url = f"/api/projects/{trip}/clip/{opaque_id(element['id'])}"
        status, headers, data = editor.client.get(url)
        assert status == 200 and headers["content-type"] == "video/mp4" and data[4:8] == b"ftyp"
        assert headers["accept-ranges"] == "bytes"
        size = len(data)
        status, headers, part = editor.client.get(url, headers={"Range": "bytes=0-99"})
        assert status == 206 and len(part) == 100 and part == data[:100]
        assert headers["content-range"] == f"bytes 0-99/{size}"
        status, headers, part = editor.client.get(url, headers={"Range": "bytes=-50"})
        assert status == 206 and part == data[-50:] and headers["content-range"] == f"bytes {size - 50}-{size - 1}/{size}"
        status, headers, _ = editor.client.get(url, headers={"Range": f"bytes={size}-"})
        assert status == 416 and headers["content-range"] == f"bytes */{size}"

    def test_the_clip_of_an_element_that_is_not_in_the_plan_is_a_404(self, editor, trip):
        assert editor.client.get(f"/api/projects/{trip}/clip/{'0' * 16}")[0] == 404
        assert editor.client.get(f"/api/projects/{trip}/clip/IMG_0001")[0] == 404

    def test_a_candidate_moment_has_a_preview_whether_or_not_it_is_in_the_plan(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        moments = [moment for video in catalog["videos"] for moment in video["moments"]]
        by_status = {}
        for moment in moments:
            by_status.setdefault(moment["status"], moment)
        assert by_status, "the trip needs moments"
        for moment in by_status.values():  # one of each status the analysis gave them, in the plan or not
            status, headers, data = editor.client.get(f"/api/projects/{trip}/candidate/{moment['clip']}")
            assert status == 200 and headers["content-type"] == "video/mp4" and data[4:8] == b"ftyp"
            assert headers["accept-ranges"] == "bytes"

    def test_the_preview_of_a_photo_candidate_or_of_an_unknown_one_is_a_404(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        photo = catalog["photos"][0]
        assert editor.client.get(f"/api/projects/{trip}/candidate/{photo['clip']}")[0] == 404
        assert editor.client.get(f"/api/projects/{trip}/candidate/{'0' * 16}")[0] == 404
        assert editor.client.get(f"/api/projects/{trip}/candidate/{photo['id']}")[0] == 404

    def test_a_whole_video_has_a_preview(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        url = f"/api/projects/{trip}/source/{catalog['videos'][0]['media']}"
        status, headers, data = editor.client.get(url)
        assert status == 200 and headers["content-type"] == "video/mp4" and data[4:8] == b"ftyp"
        assert editor.client.get(url, headers={"Range": "bytes=0-99"})[0] == 206

    def test_the_preview_of_a_photo_or_of_an_unknown_file_is_a_404(self, editor, trip):
        _, _, catalog = editor.client.get(f"/api/projects/{trip}/catalog")
        assert editor.client.get(f"/api/projects/{trip}/source/{catalog['photos'][0]['media']}")[0] == 404
        assert editor.client.get(f"/api/projects/{trip}/source/{'0' * 16}")[0] == 404
        assert editor.client.get(f"/api/projects/{trip}/source/IMG_0001.jpg")[0] == 404


class TestPreviewAndExport:
    def test_the_preview_job_makes_a_video_and_it_streams(self, editor, trip):
        client = editor.client
        status, _, job = client.post(f"/api/projects/{trip}/jobs", {"type": "preview"})
        assert status == 202
        done = wait_for_job(client, trip)
        assert done["job"]["state"] == "done", done
        _, _, state = client.get(f"/api/projects/{trip}")
        assert (state["preview"]["present"], state["preview"]["name"], state["preview"]["outdated"]) == (True, "memories_preview", False)
        assert state["preview"]["modified"]
        assert job["plan_revision"] == state["plan"]["revision"] == done["job"]["plan_revision"]
        url = f"/api/projects/{trip}/video/memories_preview"
        status, headers, data = client.get(url, headers={"Range": "bytes=0-1023"})
        assert status == 206 and len(data) == 1024 and data[4:8] == b"ftyp"
        assert headers["content-range"].startswith("bytes 0-1023/")
        assert "content-disposition" not in headers

    def test_a_new_plan_makes_the_preview_outdated(self, editor, trip, world):
        _, work, _ = world
        plan = work / "projects" / trip / "edit_plan.json"
        later = time.time() + 5
        os.utime(plan, (later, later))
        assert editor.client.get(f"/api/projects/{trip}")[2]["preview"]["outdated"] is True

    def test_a_plan_saved_while_the_preview_rendered_makes_it_outdated(self, editor, trip, world):
        _, work, _ = world
        client = editor.client
        plan = work / "projects" / trip / "edit_plan.json"
        past = time.time() - 60
        os.utime(plan, (past, past))  # an earlier test pushed its date into the future
        assert client.post(f"/api/projects/{trip}/jobs", {"type": "preview"})[0] == 202
        assert wait_for_job(client, trip)["job"]["state"] == "done"
        assert client.get(f"/api/projects/{trip}")[2]["preview"]["outdated"] is False
        document = json.loads(plan.read_text())
        document["elements"][0]["duration"] = round(document["elements"][0]["duration"] + 1, 1)
        plan.write_text(json.dumps(document))
        earlier = (work / "projects" / trip / "memories_preview.mp4").stat().st_mtime - 30
        os.utime(plan, (earlier, earlier))  # saved before the video was finished, as a save during the render is
        assert editor.client.get(f"/api/projects/{trip}")[2]["preview"]["outdated"] is True

    def test_the_export_is_downloaded_with_its_name(self, editor, trip):
        client = editor.client
        assert client.post(f"/api/projects/{trip}/jobs", {"type": "export", "name": "summer trip"})[0] == 202
        done = wait_for_job(client, trip)
        assert done["job"]["state"] == "done" and done["job"]["name"] == "summer trip", done
        _, _, state = client.get(f"/api/projects/{trip}")
        assert [entry["name"] for entry in state["exports"]] == ["summer trip"]
        assert state["exports"][0]["path"] == f"projects/{trip}/summer trip.mp4"  # relative to the work folder
        status, headers, data = client.get(f"/api/projects/{trip}/video/summer%20trip?download=1")
        assert status == 200 and data[4:8] == b"ftyp"
        assert headers["content-disposition"] == 'attachment; filename="summer trip.mp4"'

    @pytest.mark.parametrize("name", ["../x", "a/b", "x_preview", "", ".hidden", "a" * 80, "x.partial", 3])
    def test_a_bad_video_name_is_refused(self, editor, trip, name):
        status, _, body = editor.client.post(f"/api/projects/{trip}/jobs", {"type": "export", "name": name})
        assert status == 400 and body["error"]["field"] == "name"

    @pytest.mark.parametrize("name", ["../plan", "nothing", "edit_plan", "%2e%2e"])
    def test_a_video_that_is_not_there_is_a_404(self, editor, trip, name):
        assert editor.client.get(f"/api/projects/{trip}/video/{name}")[0] == 404

    def test_rendering_needs_a_plan(self, editor):
        project = editor.client.post("/api/projects", {"folder": "other"})[2]["id"]
        status, _, body = editor.client.post(f"/api/projects/{project}/jobs", {"type": "preview"})
        assert status == 409 and body["error"]["code"] == "no_plan"

    def test_an_unknown_job_type_is_refused(self, editor, trip):
        status, _, body = editor.client.post(f"/api/projects/{trip}/jobs", {"type": "fly"})
        assert status == 400 and body["error"]["field"] == "type"


class TestAI:
    def test_the_status_of_ollama(self, editor):
        status, _, body = editor.client.get("/api/ai")
        assert status == 200 and body["available"] is False and body["reason"] == "ollama_unreachable"

    def test_a_job_with_ai_needs_ollama_now_and_the_user_chooses(self, editor, trip):
        client = editor.client
        assert client.put(f"/api/projects/{trip}/settings", {"ai": True})[0] == 200
        status, _, body = client.post(f"/api/projects/{trip}/jobs", {"type": "plan"})
        assert status == 409 and body["error"]["code"] == "ai_unavailable"
        assert body["error"]["reason"] == "ollama_unreachable"
        assert client.put(f"/api/projects/{trip}/settings", {"ai": False})[0] == 200


class TestWithStandInJobs:
    """Waiting, failing and stopped jobs, with `fake_job.py` in place of the CLI."""

    @pytest.fixture
    def stand_in(self, world, trip):
        media, work, music = world

        def start(mode):
            app = EditorApp(editor_settings(media, work, music), job_command=[sys.executable, str(FAKE_JOB)],
                            job_env={**os.environ, "FAKE_JOB": mode}, ai_check=lambda: {
                                "available": True, "reachable": True, "model": "m", "model_present": True,
                                "reason": None})
            return RunningEditor(app)

        return start

    def arguments(self, work, project):
        return json.loads((work / "projects" / project / "argv.json").read_text())

    def test_the_plan_job_gets_the_settings_as_cli_options(self, stand_in, world, trip):
        _, work, _ = world
        with stand_in("ok") as running:
            client = running.client
            client.put(f"/api/projects/{trip}/settings", {"ai": True, "ai_frames": 40, "duration": "2:00",
                                                          "structure": "chronological", "assume_tz": "Europe/Rome"})
            assert client.post(f"/api/projects/{trip}/jobs", {"type": "plan"})[0] == 202
            wait_for_job(client, trip)
            arguments = self.arguments(work, trip)
            assert arguments[0] == "--dry-run" and "--require-ai" in arguments and "--no-ai" not in arguments
            assert arguments[arguments.index("--ai-frames") + 1] == "40" and "--ai-time-budget" not in arguments
            assert arguments[arguments.index("--duration") + 1] == "2:00"
            assert arguments[arguments.index("--structure") + 1] == "chronological"
            assert arguments[arguments.index("--assume-tz") + 1] == "Europe/Rome"
            assert arguments[arguments.index("--workdir") + 1] == str(work / "projects" / trip)
            client.put(f"/api/projects/{trip}/settings", {"ai": False, "ai_time_budget": 600, "duration": "15",
                                                          "structure": "narrative", "assume_tz": None})

    def test_the_analysis_without_ai_gets_no_ai(self, stand_in, world, trip):
        _, work, _ = world
        with stand_in("ok") as running:
            running.client.post(f"/api/projects/{trip}/jobs", {"type": "plan", "ai": False})
            wait_for_job(running.client, trip)
            arguments = self.arguments(work, trip)
            assert "--no-ai" in arguments and "--require-ai" not in arguments

    def test_the_render_jobs_get_the_name_and_preview(self, stand_in, world, trip):
        _, work, _ = world
        with stand_in("ok") as running:
            running.client.post(f"/api/projects/{trip}/jobs", {"type": "preview", "name": "trial"})
            wait_for_job(running.client, trip)
            arguments = self.arguments(work, trip)
            assert arguments[0] == "--render-only" and "--preview" in arguments
            assert arguments[arguments.index("--output") + 1] == "trial.mp4"
            running.client.post(f"/api/projects/{trip}/jobs", {"type": "export"})
            wait_for_job(running.client, trip)
            arguments = self.arguments(work, trip)
            assert "--preview" not in arguments and arguments[arguments.index("--output") + 1] == "memories.mp4"

    @pytest.mark.parametrize(("code", "key"), [(2, "invalid_input"), (3, "check_failed"), (4, "missing_requirements"),
                                               (5, "project_busy"), (1, "failed"), (7, "failed")])
    def test_exit_codes_become_messages(self, stand_in, trip, code, key):
        with stand_in(f"exit:{code}") as running:
            running.client.post(f"/api/projects/{trip}/jobs", {"type": "plan", "ai": False})
            body = wait_for_job(running.client, trip)
            exit_info = body["job"]["exit"]
            assert body["job"]["state"] == "failed"
            assert exit_info["code"] == code and exit_info["key"] == key and exit_info["message"]
            assert exit_info["detail"] == "the reason of the failure"

    def test_one_job_at_a_time_then_stop_with_143_and_run_it_again(self, stand_in, world, trip):
        _, work, _ = world
        with stand_in("sleep") as running:
            client = running.client
            start_sleeping(client, work, trip, {"type": "plan", "ai": False})
            other = client.post("/api/projects", {"folder": "other"})[2]["id"]
            for project in (trip, other):
                status, _, body = client.post(f"/api/projects/{project}/jobs", {"type": "plan", "ai": False})
                assert status == 409 and body["error"]["code"] == "job_running"
            assert client.get(f"/api/projects/{trip}/status")[2]["job"]["state"] == "running"
            assert client.post(f"/api/projects/{other}/jobs/cancel")[2] == {"stopping": False}
            assert client.post(f"/api/projects/{trip}/jobs/cancel")[2] == {"stopping": True}
            body = wait_for_job(client, trip)
            assert body["job"]["state"] == "interrupted" and body["job"]["exit"]["code"] == 143
            assert body["job"]["exit"]["key"] == "interrupted"
            assert client.post(f"/api/projects/{trip}/jobs", {"type": "plan", "ai": False})[0] == 202
            client.post(f"/api/projects/{trip}/jobs/cancel")
            wait_for_job(client, trip)

    def test_the_plan_is_read_only_during_the_analysis_and_plan_job(self, stand_in, world, trip):
        with stand_in("sleep") as running:
            client = running.client
            _, _, current = client.get(f"/api/projects/{trip}/plan")
            start_sleeping(client, world[1], trip, {"type": "plan", "ai": False})
            status, _, body = client.put(f"/api/projects/{trip}/plan", current["plan"],
                                         {"If-Match": current["revision"]})
            assert status == 409 and body["error"]["code"] == "plan_locked"
            client.post(f"/api/projects/{trip}/jobs/cancel")
            wait_for_job(client, trip)

    def test_during_a_render_the_plan_can_still_be_saved(self, stand_in, world, trip):
        with stand_in("sleep") as running:
            client = running.client
            _, _, current = client.get(f"/api/projects/{trip}/plan")
            start_sleeping(client, world[1], trip, {"type": "export"})
            status, _, _ = client.put(f"/api/projects/{trip}/plan", current["plan"], {"If-Match": current["revision"]})
            assert status == 200
            client.post(f"/api/projects/{trip}/jobs/cancel")
            wait_for_job(client, trip)

    def test_the_server_stops_the_job_when_it_shuts_down(self, stand_in, world, trip):
        running = stand_in("sleep")
        with running:
            start_sleeping(running.client, world[1], trip, {"type": "plan", "ai": False})
            job = running.app.jobs._job
            started = time.monotonic()
        assert job.exit_code == 143 and time.monotonic() - started < 5

    def test_a_project_used_by_a_cli_job_is_read_only(self, stand_in, world, trip):
        import fcntl

        _, work, _ = world
        descriptor = os.open(work / "projects" / trip / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            with stand_in("ok") as running:
                client = running.client
                assert client.get(f"/api/projects/{trip}")[2]["busy"] is True
                _, _, current = client.get(f"/api/projects/{trip}/plan")
                status, _, body = client.put(f"/api/projects/{trip}/plan", current["plan"],
                                             {"If-Match": current["revision"]})
                assert status == 409 and body["error"]["code"] == "project_busy"
                status, _, body = client.post(f"/api/projects/{trip}/jobs", {"type": "plan", "ai": False})
                assert status == 409 and body["error"]["code"] == "project_busy"
        finally:
            os.close(descriptor)


def test_a_job_with_ai_and_no_ollama_exits_4_and_makes_no_plan(world):
    """The real CLI, `--require-ai` with Ollama unreachable: code 4, and nothing is produced without AI."""
    media, _, _ = world
    work = Path(world[1]).parent / "api-no-ollama-work"
    environment = {**os.environ, "OLLAMA_HOST": "http://127.0.0.1:9", "VACATION_MEMORIES_ALLOW_REMOTE_OLLAMA": "1"}
    app = EditorApp(editor_settings(media, work), job_env=environment,
                    ai_check=lambda: {"available": True, "reachable": True, "model": "m", "model_present": True,
                                      "reason": None})
    with RunningEditor(app) as running:
        client = running.client
        project = client.post("/api/projects", {"folder": "other"})[2]["id"]
        client.put(f"/api/projects/{project}/settings", {"ai": True, "duration": "15"})
        assert client.post(f"/api/projects/{project}/jobs", {"type": "plan"})[0] == 202
        body = wait_for_job(client, project)
        assert body["job"]["exit"]["code"] == 4 and body["job"]["exit"]["key"] == "missing_requirements"
        assert not (work / "projects" / project / "edit_plan.json").exists()
        shutil.rmtree(work, ignore_errors=True)


def test_no_response_shows_an_absolute_path(editor, world, trip):
    """Everything the API told in this module's tests: no host path, in any body (section 5)."""
    media, work, music = world
    client = editor.client
    bodies = [client.get("/api/folders")[2], client.get(f"/api/projects/{trip}")[2],
              client.get(f"/api/projects/{trip}/plan")[2], client.get(f"/api/projects/{trip}/catalog")[2],
              client.get(f"/api/projects/{trip}/status")[2]]
    text = json.dumps(bodies)
    for forbidden in (str(media), str(work), str(music), "/media", "/work", "/tmp"):
        assert forbidden not in text
    ids = [item["media"] for item in client.get(f"/api/projects/{trip}/catalog")[2]["photos"]]
    assert all(len(value) == 16 and "." not in value for value in ids)


def test_the_progress_of_an_earlier_job_is_not_shown_for_the_one_that_just_started(world, trip):
    """`status.json` still holds the last run until the new child writes its own."""
    media, work, music = world
    app = EditorApp(editor_settings(media, work, music), job_command=[sys.executable, str(FAKE_JOB)],
                    job_env={**os.environ, "FAKE_JOB": "sleep"})
    with RunningEditor(app) as running:
        client = running.client
        (work / "projects" / trip / "status.json").write_text(json.dumps({
            "state": "done", "phase": "rendering", "done": 3, "total": 3, "eta_seconds": 0,
            "started_at": "2000-01-01T00:00:00+00:00", "updated_at": "2000-01-01T00:00:09+00:00"}))
        start_sleeping(client, work, trip, {"type": "export"})
        assert client.get(f"/api/projects/{trip}/status")[2]["progress"] is None
        client.post(f"/api/projects/{trip}/jobs/cancel")
        wait_for_job(client, trip)
