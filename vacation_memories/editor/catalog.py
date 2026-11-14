"""The catalog of a project: the material the interface shows next to the timeline (section 11 of `UI-SPEC.md`).

It is built from `candidates.json`, which the pipeline writes after every analysis, and from the plan:
the server never reruns the analysis or the ranking and never opens the cache. Files are named in the
URLs by opaque ids (a hash of the relative path), never by their names.
"""

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from vacation_memories.editor.ids import opaque_id
from vacation_memories.planning.edit_plan import DIP, DISSOLVE, FADE_IN, FADE_OUT, EditPlan
from vacation_memories.planning.edit_planner import EXCLUSION_OVERLAP_SECONDS

AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"})
MAX_TRACKS = 2000

SELECTED, EXCLUDED, DISCARDED, AVAILABLE = "selected", "excluded", "discarded", "available"
# Where each effect can go (section 8): the interface needs it to offer them and to snap them to the clip.
_POSITIONS = {FADE_IN: "start", FADE_OUT: "end", DISSOLVE: "start", DIP: "anywhere"}


def file_id(source: str) -> str:
    return opaque_id(source)


def music_tracks(music_root: Path | None) -> list[dict]:
    """The audio files under the music root, with an opaque id each (the waveform is asked by it)."""
    if music_root is None or not music_root.is_dir():
        return []
    found = []
    for folder, _dirs, names in os.walk(music_root):
        for name in names:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                path = Path(folder) / name
                relative = path.relative_to(music_root).as_posix()
                found.append({"id": opaque_id(relative), "file": relative})
    found.sort(key=lambda track: track["file"])
    return found[:MAX_TRACKS]


def transition_library(defaults: dict[str, float], minimum: float) -> list[dict]:
    return [{"type": kind, "position": position, "default_seconds": defaults[kind], "min_seconds": minimum}
            for kind, position in _POSITIONS.items()]


def build_catalog(document: dict | None, plan: EditPlan | None, tracks: Iterable[dict],
                  library: Sequence[dict]) -> dict:
    """Photos, videos with their moments, the transition library and the tracks.

    `status` of every candidate: `selected` (in the plan), `excluded` (the user hid or deleted it),
    `discarded` (the analysis left it out, with its reason) or `available`.
    """
    catalog: dict = {"analysis": None, "photos": [], "videos": [], "transitions": list(library),
                     "music": {"tracks": list(tracks)}}
    if document is None:
        return catalog
    in_plan = set() if plan is None else {element.id for element in plan.elements}
    excluded = [] if plan is None else list(plan.excluded)
    catalog["analysis"] = {"generated_at": document.get("generated_at"), "counts": document.get("counts"),
                           "ai": document.get("ai")}

    files = {entry["path"]: entry for entry in document.get("files", [])}
    moments: dict[str, list[dict]] = {}
    for candidate in document.get("candidates", []):
        item = _item(candidate, in_plan, excluded)
        if candidate["type"] == "photo":
            catalog["photos"].append(item)
        else:
            moments.setdefault(candidate["source"], []).append(item)

    for path, entry in files.items():
        if entry["type"] == "video":
            catalog["videos"].append({
                "media": file_id(path), "source": path, "duration": entry.get("duration"),
                "readable": entry.get("readable", True),
                "moments": sorted(moments.get(path, []), key=lambda moment: moment["start"]),
            })
    return catalog


def _item(candidate: dict, in_plan: set[str], excluded: Sequence) -> dict:
    source, start, end = candidate["source"], candidate.get("start"), candidate.get("end")
    if candidate["id"] in in_plan:
        status = SELECTED
    elif _is_excluded(source, start, end, excluded):
        status = EXCLUDED
    elif candidate["status"] == "discarded":
        status = DISCARDED
    else:
        status = AVAILABLE
    media = file_id(source)
    item = {
        "id": candidate["id"], "source": source, "media": media, "status": status,
        "discard": candidate.get("discard"), "score": candidate["score"], "moment_type": candidate.get("moment_type"),
        "category": candidate.get("category"), "narrative_value": candidate.get("narrative_value"),
        "description": candidate.get("description"), "captured_at": candidate.get("captured_at"),
        "day": candidate.get("day"), "group": candidate.get("group"),
    }
    if start is not None:
        item.update(start=start, end=end, thumb_time=round((start + end) / 2, 1))
    return item


def _is_excluded(source: str, start: float | None, end: float | None, excluded: Sequence) -> bool:
    """The rule the planner applies when it regenerates the plan (`candidate_is_excluded`)."""
    for exclusion in excluded:
        if exclusion.source != source:
            continue
        if exclusion.start is None:
            return True
        if start is not None and min(end, exclusion.end) - max(start, exclusion.start) > EXCLUSION_OVERLAP_SECONDS:
            return True
    return False
