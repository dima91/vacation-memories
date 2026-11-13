"""`review.html`: self-contained, blind, with the flags of earlier cycles filled in."""

import json
import re

from vacation_memories.evaluate.labels import Labels, Segment
from vacation_memories.evaluate.page import page_data, render_page

VARIANT_NAMES = ("full", "random", "no-ai", "technical-only")


def prepared():
    """A small run as `prepare.json` describes it: two versions, a left-out photo."""
    segments = {
        "s1": {"source": "day1/IMG_0001.jpg", "start": None, "end": None, "kind": "photo", "day": 1,
               "moment_type": "scenery", "thumb": "thumbs/s1.jpg"},
        "s2": {"source": "day1/VID_0002.mp4", "start": 2.0, "end": 5.5, "kind": "video", "day": 1,
               "moment_type": None, "thumb": "thumbs/s2.jpg"},
        "s3": {"source": "day2/IMG_0003</script><b>.jpg", "start": None, "end": None, "kind": "photo", "day": 2,
               "moment_type": "social", "thumb": None},
    }

    def variant(letter, keys):
        clips = [{"key": key, "start": 3.0 * number, "duration": 3.0, "section": "body",
                  "score": {"final": 0.5}, "nearest_similarity": 0.6} for number, key in enumerate(keys)]
        return {"video": f"variants/{letter}_preview.mp4", "structure": "compressed", "requested_duration": 15.0,
                "planned_duration": 15.0, "obtained_duration": 15.0, "skipped_segments": 0,
                "sections": [{"name": "opening", "budget": 0.0, "planned": 0.0, "clips": 0},
                             {"name": "body", "budget": 15.0, "planned": 15.0, "clips": len(keys)}],
                "clips": clips}

    return {"version": 1, "run_id": "run-1", "segments": segments,
            "variants": {"B": variant("B", ["s2", "s1"]), "A": variant("A", ["s1"])},
            "excluded": [{"key": "s3", "reason": "budget", "picked": "best", "score": {"final": 0.7}}]}


def embedded(page: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="review-data">(.*?)</script>', page, re.S).group(1))


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def test_the_page_is_self_contained_with_relative_paths_only():
    page = render_page(page_data(prepared(), Labels()))
    assert "http://" not in page and "https://" not in page
    assert not re.search(r"<(link|iframe|object|embed|img|video)\b", page)  # media come only from the data
    assert not re.search(r"\b(src|href)=", page) and "fetch(" not in page and "import(" not in page
    data = embedded(page)
    paths = [variant["video"] for variant in data["variants"]]
    paths += [clip["thumb"] for variant in data["variants"] for clip in variant["clips"] if clip["thumb"]]
    paths += [item["thumb"] for item in data["excluded"] if item["thumb"]]
    assert paths and all(re.fullmatch(r"(variants|thumbs)/[A-Za-z0-9_]+\.(mp4|jpg)", path) for path in paths)


def test_the_page_is_blind_no_variant_names_reasons_scores_or_file_names():
    page = render_page(page_data(prepared(), Labels()))
    data = embedded(page)
    assert not set(strings(data)) & set(VARIANT_NAMES)
    assert not re.search(r"\b(random|mapping|seed|reason|score|technical|budget)\b", page, re.I)
    assert "IMG_0001" not in page and "day1/" not in page
    assert [variant["label"] for variant in data["variants"]] == ["A", "B"]
    assert data["variants"][1]["sections"] == ["body"]  # only the parts that have clips get a rhythm


def test_data_cannot_close_the_script_element():
    model = page_data(prepared(), Labels())
    model["run_id"] = "</script><script>alert(1)</script>"
    page = render_page(model)
    assert page.count("</script>") == 2  # the data element and the page script
    assert embedded(page)["run_id"] == model["run_id"]


def test_flags_of_earlier_cycles_are_filled_in_and_new_moments_highlighted():
    labels = Labels()
    labels.set(Segment("day1/VID_0002.mp4", 2.4, 5.9), "run-0", repetitive=True, length="too_long")  # re-segmented
    labels.set(Segment("day2/IMG_0003</script><b>.jpg"), "run-0", important=True)
    data = page_data(prepared(), labels)
    assert data["labels"] == {
        "s2": {"important": None, "repetitive": True, "length": "too_long"},
        "s3": {"important": True, "repetitive": None, "length": None},
    }
    assert data["new"] == ["s1"]
