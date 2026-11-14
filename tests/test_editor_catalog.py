"""The catalog of a project, from a hand-written `candidates.json` document and a plan."""

from vacation_memories.editor.catalog import build_catalog, file_id, music_tracks, transition_library
from vacation_memories.planning.edit_plan import EditPlan, Exclusion, OutputSpec, PlanElement


def candidate(identifier, source, kind, status="eligible", start=None, end=None, discard=None):
    return {"id": identifier, "type": kind, "source": source, "start": start, "end": end, "status": status,
            "discard": discard, "score": {"final": 0.5, "technical_score": 0.6, "narrative_score": 0.4,
                                          "components": None},
            "moment_type": None, "category": None, "narrative_value": None, "description": None,
            "captured_at": "2024-05-01T10:00:00.000", "day": "2024-05-01", "group": None}


def document():
    return {"version": 1, "generated_at": "now", "counts": {}, "ai": None, "files": [
        {"path": "day1/a.jpg", "type": "photo", "readable": True, "duration": None},
        {"path": "day1/b.jpg", "type": "photo", "readable": True, "duration": None},
        {"path": "day1/c.jpg", "type": "photo", "readable": True, "duration": None},
        {"path": "day1/d.jpg", "type": "photo", "readable": True, "duration": None},
        {"path": "day1/v.mp4", "type": "video", "readable": True, "duration": 30.0},
        {"path": "day1/broken.mp4", "type": "video", "readable": False, "duration": None},
    ], "candidates": [
        candidate("day1/a.jpg", "day1/a.jpg", "photo"),
        candidate("day1/b.jpg", "day1/b.jpg", "photo"),
        candidate("day1/c.jpg", "day1/c.jpg", "photo", status="discarded",
                  discard={"reason": "blurry", "detail": None}),
        candidate("day1/d.jpg", "day1/d.jpg", "photo"),
        candidate("day1/v.mp4#0.000-8.000", "day1/v.mp4", "video", start=0.0, end=8.0),
        candidate("day1/v.mp4#8.000-16.000", "day1/v.mp4", "video", start=8.0, end=16.0),
        candidate("day1/v.mp4#16.000-24.000", "day1/v.mp4", "video", start=16.0, end=24.0),
    ]}


def plan(*elements, excluded=()):
    return EditPlan(OutputSpec(1920, 1080, 30.0), list(elements), excluded=tuple(excluded))


LIBRARY = transition_library({"fade_in": 1.0, "fade_out": 3.0, "dissolve": 0.8, "dip": 0.8}, 0.2)


def statuses(catalog):
    photos = {item["id"]: item["status"] for item in catalog["photos"]}
    moments = {moment["id"]: moment["status"] for video in catalog["videos"] for moment in video["moments"]}
    return photos, moments


def test_without_an_analysis_the_catalog_is_empty_but_has_the_library():
    catalog = build_catalog(None, None, [], LIBRARY)
    assert catalog["photos"] == [] and catalog["videos"] == [] and catalog["analysis"] is None
    assert [item["type"] for item in catalog["transitions"]] == ["fade_in", "fade_out", "dissolve", "dip"]
    assert {item["type"]: item["position"] for item in catalog["transitions"]} == {
        "fade_in": "start", "fade_out": "end", "dissolve": "start", "dip": "anywhere"}


def test_every_candidate_is_listed_with_what_it_is():
    catalog = build_catalog(document(), None, [], LIBRARY)
    photos, moments = statuses(catalog)
    assert photos == {"day1/a.jpg": "available", "day1/b.jpg": "available", "day1/c.jpg": "discarded",
                      "day1/d.jpg": "available"}
    assert len(moments) == 3
    discarded = next(item for item in catalog["photos"] if item["id"] == "day1/c.jpg")
    assert discarded["discard"] == {"reason": "blurry", "detail": None}


def test_the_videos_have_their_moments_in_order_and_unreadable_ones_are_marked():
    catalog = build_catalog(document(), None, [], LIBRARY)
    videos = {video["source"]: video for video in catalog["videos"]}
    assert [moment["start"] for moment in videos["day1/v.mp4"]["moments"]] == [0.0, 8.0, 16.0]
    assert videos["day1/v.mp4"]["duration"] == 30.0 and videos["day1/v.mp4"]["media"] == file_id("day1/v.mp4")
    assert videos["day1/broken.mp4"]["readable"] is False and videos["day1/broken.mp4"]["moments"] == []
    assert videos["day1/v.mp4"]["moments"][0]["thumb_time"] == 4.0


def test_the_ids_in_urls_are_opaque_not_file_names():
    catalog = build_catalog(document(), None, [], LIBRARY)
    for item in catalog["photos"]:
        assert item["media"] == file_id(item["source"]) and len(item["media"]) == 16 and "jpg" not in item["media"]


def test_what_is_in_the_plan_is_selected():
    selected = plan(PlanElement("day1/a.jpg", "photo", 4.0),
                    PlanElement("day1/v.mp4", "video", 5.0, start=8.0, end=13.0, id="day1/v.mp4#8.000-16.000"))
    photos, moments = statuses(build_catalog(document(), selected, [], LIBRARY))
    assert photos["day1/a.jpg"] == "selected" and photos["day1/b.jpg"] == "available"
    assert moments["day1/v.mp4#8.000-16.000"] == "selected" and moments["day1/v.mp4#0.000-8.000"] == "available"


def test_a_hidden_photo_and_a_deleted_moment_are_excluded_as_the_planner_reads_them():
    deleted = plan(excluded=[Exclusion("day1/b.jpg"), Exclusion("day1/v.mp4", 9.0, 12.0)])
    photos, moments = statuses(build_catalog(document(), deleted, [], LIBRARY))
    assert photos["day1/b.jpg"] == "excluded" and photos["day1/a.jpg"] == "available"
    assert moments["day1/v.mp4#8.000-16.000"] == "excluded"  # the deleted moment is inside it
    assert moments["day1/v.mp4#0.000-8.000"] == "available" and moments["day1/v.mp4#16.000-24.000"] == "available"


def test_a_moment_that_only_touches_a_deleted_one_is_not_excluded():
    touching = plan(excluded=[Exclusion("day1/v.mp4", 8.0, 16.0)])
    _, moments = statuses(build_catalog(document(), touching, [], LIBRARY))
    assert moments["day1/v.mp4#0.000-8.000"] == "available" and moments["day1/v.mp4#16.000-24.000"] == "available"
    assert moments["day1/v.mp4#8.000-16.000"] == "excluded"


def test_a_selected_candidate_is_selected_even_if_the_plan_also_lists_it_excluded():
    both = plan(PlanElement("day1/a.jpg", "photo", 4.0), excluded=[Exclusion("day1/a.jpg")])
    assert statuses(build_catalog(document(), both, [], LIBRARY))[0]["day1/a.jpg"] == "selected"


def test_the_tracks_of_the_music_root_have_opaque_ids(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "two.mp3").write_bytes(b"x")
    (tmp_path / "one.wav").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    tracks = music_tracks(tmp_path)
    assert [track["file"] for track in tracks] == ["b/two.mp3", "one.wav"]
    assert all(len(track["id"]) == 16 and "." not in track["id"] for track in tracks)
    assert music_tracks(None) == [] and music_tracks(tmp_path / "missing") == []
