"""Persistent labels of the evaluation: stable segments, matching after a new segmentation, history, salt."""

import json
import stat

import pytest

from vacation_memories.evaluate.labels import (
    MATCH_IOU,
    TOO_LONG,
    Label,
    Labels,
    LabelsError,
    Segment,
    anonymous_id,
    load_history,
    load_labels,
    record_cycle,
    save_labels,
    workdir_salt,
)

CLIP = "2024-07-02_day2/VID_0003.mp4"
PHOTO = "2024-07-01_day1/IMG_0001.jpg"


def test_a_segment_has_the_id_of_a_candidate_and_overlaps_only_the_same_source():
    assert Segment(PHOTO).id == PHOTO
    assert Segment(CLIP, 4.0, 8.0).id == f"{CLIP}#4.000-8.000"
    assert Segment(CLIP, 4.0, 8.0).iou(Segment(CLIP, 6.0, 10.0)) == pytest.approx(2 / 6)
    assert Segment(PHOTO).iou(Segment(PHOTO)) == 1.0
    assert Segment(CLIP, 0.0, 4.0).iou(Segment("other.mp4", 0.0, 4.0)) == 0.0
    assert Segment(CLIP, 0.0, 4.0).iou(Segment(CLIP, 4.0, 8.0)) == 0.0


def test_a_clip_shows_a_labelled_segment_when_it_covers_a_second_or_half_of_it():
    labelled = Segment(CLIP, 0.0, 8.0)
    assert Segment(CLIP, 2.0, 5.5).shows(labelled)  # 3.5 s of 8: below the identity threshold, but shown
    assert Segment(CLIP, 2.0, 5.5).iou(labelled) < MATCH_IOU
    assert not Segment(CLIP, 7.5, 12.0).shows(labelled)  # half a second
    assert Segment(CLIP, 1.0, 2.0).shows(Segment(CLIP, 1.2, 2.0))  # all of a short stretch
    assert Segment(PHOTO).shows(Segment(PHOTO)) and not Segment(PHOTO).shows(Segment(CLIP, 0.0, 4.0))


def test_labels_survive_a_new_segmentation_with_shifted_boundaries():
    labels = Labels()
    labels.set(Segment(CLIP, 4.0, 8.0), "cycle1", repetitive=True, length=TOO_LONG)
    labels.set(Segment(CLIP, 12.0, 16.0), "cycle1", important=True)
    labels.set(Segment(PHOTO), "cycle1", important=True)
    # The next analysis cuts the clip elsewhere: same stretches, boundaries moved by a second.
    assert labels.match(Segment(CLIP, 4.6, 8.9)).repetitive is True
    assert labels.match(Segment(CLIP, 11.2, 15.4)).important is True
    assert labels.match(Segment(PHOTO)).important is True
    assert labels.match(Segment(CLIP, 8.0, 12.0)) is None  # between the two: a new segment
    assert labels.match(Segment(CLIP, 6.5, 13.5)) is None  # overlaps both, but neither enough


def test_a_new_cycle_moves_the_label_to_the_new_extent_and_keeps_the_other_flags():
    labels = Labels()
    labels.set(Segment(CLIP, 4.0, 8.0), "cycle1", repetitive=True, length=TOO_LONG)
    label = labels.set(Segment(CLIP, 4.5, 8.5), "cycle2", length="ok")
    assert len(labels.labels) == 1 and label.segment == Segment(CLIP, 4.5, 8.5)
    assert (label.repetitive, label.length, label.cycle) == (True, "ok", "cycle2")
    # A stretch split in two within one cycle keeps two labels.
    second = labels.set(Segment(CLIP, 4.0, 7.0), "cycle2", repetitive=False)
    assert len(labels.labels) == 2 and second is not label


def test_labels_are_saved_and_read_back_and_a_broken_file_is_refused(tmp_path):
    labels = Labels([Label(Segment(CLIP, 4.0, 8.0), repetitive=True, cycle="c1"), Label(Segment(PHOTO), True)])
    save_labels(tmp_path, labels)
    again = load_labels(tmp_path)
    assert sorted(label.to_dict()["source"] for label in again.labels) == sorted([CLIP, PHOTO])
    assert again.match(Segment(CLIP, 4.0, 8.0)).repetitive is True
    assert load_labels(tmp_path / "nothing").labels == []
    for broken in ('{"version": 9, "labels": []}', '{"version": 1, "labels": [{"source": "a.mp4", "start": 3}]}',
                   '{"version": 1, "labels": [{"source": "a.jpg", "length": "longish"}]}', "not json"):
        (tmp_path / "labels.json").write_text(broken)
        with pytest.raises(LabelsError):
            load_labels(tmp_path)


def test_the_history_keeps_one_entry_per_run_in_the_order_of_preparation(tmp_path):
    record_cycle(tmp_path, {"run_id": "b", "created_at": "2026-02-01T10:00:00+00:00", "value": 1})
    record_cycle(tmp_path, {"run_id": "a", "created_at": "2026-01-01T10:00:00+00:00", "value": 1})
    cycles = record_cycle(tmp_path, {"run_id": "b", "created_at": "2026-02-01T10:00:00+00:00", "value": 2})
    assert [(cycle["run_id"], cycle["cycle"], cycle["value"]) for cycle in cycles] == [("a", 1, 1), ("b", 2, 2)]
    assert load_history(tmp_path) == cycles
    (tmp_path / "eval_history.json").write_text(json.dumps({"version": 1, "cycles": "x"}))
    with pytest.raises(LabelsError):
        load_history(tmp_path)


def test_anonymous_ids_are_stable_with_the_salt_and_private_without_it(tmp_path):
    salt = workdir_salt(tmp_path)
    assert stat.S_IMODE((tmp_path / ".salt").stat().st_mode) == 0o600
    assert workdir_salt(tmp_path) == salt
    segment = Segment(CLIP, 4.0, 8.0)
    assert anonymous_id(salt, segment) == anonymous_id(workdir_salt(tmp_path), segment)
    assert anonymous_id(salt, segment) != anonymous_id(b"x" * 32, segment)
    assert anonymous_id(salt, segment) != anonymous_id(salt, Segment(CLIP, 4.0, 8.5))
    assert "VID" not in anonymous_id(salt, segment) and len(anonymous_id(salt, segment)) == 16
    assert anonymous_id(salt, Segment("caf\udce9.jpg"))  # a name that is not valid UTF-8
