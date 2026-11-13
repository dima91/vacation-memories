"""`ground_truth.json`: what a synthetic vacation contains, scene by scene.

The generator of the evaluation vacation (`synthetic_media.populate_evaluation_vacation`) writes it
next to the media; the metrics read it. For every file: day, place, capture date and its scenes, a
photo being a single scene and a clip a run of scenes `[start, end)` in seconds from its first frame.
A scene may belong to a repetition group, be an important moment or have a defect:

- `near_duplicate` groups: the same shot with another brightness, compression or size, which the
  perceptual hash recognizes;
- `similar_scene` groups: the same scene framed differently, which the hash does not recognize
  (Milestone 2 embeddings should);
- important moments: `motion` (a fast pan) or `unique` (a scene unlike the rest of its day);
- defects: `blurred` or `black`, which the technical threshold must discard.
"""

import json
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path

from vacation_memories.core.atomic import atomic_write_json

GROUND_TRUTH_NAME = "ground_truth.json"
SCHEMA_VERSION = 1
NEAR_DUPLICATE, SIMILAR_SCENE = "near_duplicate", "similar_scene"
GROUP_KINDS = (NEAR_DUPLICATE, SIMILAR_SCENE)
MOTION, UNIQUE = "motion", "unique"
IMPORTANT_REASONS = (MOTION, UNIQUE)
BLURRED, BLACK = "blurred", "black"
DEFECTS = (BLURRED, BLACK)


class GroundTruthError(ValueError):
    """A `ground_truth.json` that is missing, of another version or inconsistent."""


@dataclass(frozen=True)
class TruthScene:
    scene_id: int
    start: float | None  # None for a photo
    end: float | None
    group: str | None = None
    group_kind: str | None = None
    important: bool = False
    important_reason: str | None = None
    defect: str | None = None


@dataclass(frozen=True)
class TruthFile:
    path: str  # relative to the dataset folder, with forward slashes
    kind: str  # "photo" or "video"
    day: int  # 1 for the first day
    place: str
    captured_at: str  # naive ISO date and time, as written in the metadata
    scenes: tuple[TruthScene, ...]


@dataclass(frozen=True)
class GroundTruth:
    seed: int
    params: dict  # the generator's parameters: the same seed and parameters give the same vacation
    days: int
    places: tuple[str, ...]
    files: tuple[TruthFile, ...]
    version: int = SCHEMA_VERSION

    @cached_property
    def _by_path(self) -> dict[str, TruthFile]:
        return {item.path: item for item in self.files}

    def file(self, path: str) -> TruthFile:
        return self._by_path[path]

    def scenes(self):
        """Every (file, scene) pair."""
        return [(item, scene) for item in self.files for scene in item.scenes]

    def to_dict(self) -> dict:
        return {
            "version": self.version, "seed": self.seed, "params": self.params, "days": self.days,
            "places": list(self.places),
            "files": [{**asdict(item), "scenes": [asdict(scene) for scene in item.scenes]} for item in self.files],
        }

    def save(self, folder: Path) -> Path:
        path = Path(folder) / GROUND_TRUTH_NAME
        atomic_write_json(path, self.to_dict())
        return path


def load_ground_truth(folder: Path) -> GroundTruth:
    path = Path(folder) / GROUND_TRUTH_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GroundTruthError(f"{path} not found") from None
    except (OSError, ValueError) as error:
        raise GroundTruthError(f"{path} is not readable: {error}") from None
    return ground_truth_from_dict(data)


def ground_truth_from_dict(data: object) -> GroundTruth:
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        raise GroundTruthError(f"unsupported ground truth, version {SCHEMA_VERSION} is expected")
    try:
        files = tuple(
            TruthFile(**{**item, "scenes": tuple(TruthScene(**scene) for scene in item["scenes"])})
            for item in data["files"]
        )
        truth = GroundTruth(int(data["seed"]), dict(data["params"]), int(data["days"]), tuple(data["places"]), files)
    except (KeyError, TypeError) as error:
        raise GroundTruthError(f"inconsistent ground truth: {error}") from None
    _check(truth)
    return truth


def _check(truth: GroundTruth) -> None:
    paths = [item.path for item in truth.files]
    if len(set(paths)) != len(paths):
        raise GroundTruthError("a file is listed twice")
    for item in truth.files:
        if item.kind not in ("photo", "video") or not 1 <= item.day <= truth.days or item.place not in truth.places:
            raise GroundTruthError(f"{item.path}: kind, day or place out of range")
        if not item.scenes or (item.kind == "photo" and len(item.scenes) != 1):
            raise GroundTruthError(f"{item.path}: a photo has one scene, a clip at least one")
        for scene in item.scenes:
            if (scene.group is None) != (scene.group_kind is None) or scene.group_kind not in (None, *GROUP_KINDS):
                raise GroundTruthError(f"{item.path}: inconsistent repetition group")
            if scene.important != (scene.important_reason in IMPORTANT_REASONS) or scene.defect not in (None, *DEFECTS):
                raise GroundTruthError(f"{item.path}: inconsistent important moment or defect")
