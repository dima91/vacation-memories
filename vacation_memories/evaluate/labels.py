"""Persistent labels, history and anonymous ids of the evaluation of real material (`<workdir>/eval/`).

A flag belongs to a stable segment: its source (the path relative to `--input`) and its extent
`[start, end)` in seconds, None for a photo. When the segmentation changes, a segment is the same
as a labelled one of the same source when their temporal overlap (intersection over union) is at
least `MATCH_IOU`, so a later cycle only needs to review what is new. A plan shows a labelled
segment when one of its clips covers enough of it (the rule of the synthetic metrics).

`eval_history.json` keeps one entry per cycle. The anonymous ids of `eval_summary.json` are an
HMAC of the segment id with a secret salt of the workdir (`eval/.salt`, mode 0600): stable across
cycles, but they cannot be traced back to a file name without the salt.
"""

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from vacation_memories.core.atomic import atomic_write_json
from vacation_memories.core.config import ConfigError
from vacation_memories.core.workdir import PRIVATE_MODE
from vacation_memories.evaluate.metrics import INCLUDED_MIN_FRACTION, INCLUDED_MIN_SECONDS

EVAL_FOLDER = "eval"
LABELS_NAME = "labels.json"
HISTORY_NAME = "eval_history.json"
SALT_NAME = ".salt"
LABELS_VERSION = 1
HISTORY_VERSION = 1
MATCH_IOU = 0.5
SALT_BYTES = 32
ANONYMOUS_ID_CHARS = 16
OK, TOO_LONG, TOO_SHORT = "ok", "too_long", "too_short"
LENGTHS = (OK, TOO_LONG, TOO_SHORT)
FLAGS = ("important", "repetitive", "length")


class LabelsError(ConfigError):
    """`labels.json` or `eval_history.json` cannot be used (exit code 2)."""


def eval_folder(workdir: Path) -> Path:
    return Path(workdir) / EVAL_FOLDER


@dataclass(frozen=True)
class Segment:
    """A photo (no extent) or the stretch `[start, end)` of a clip."""

    source: str
    start: float | None = None
    end: float | None = None

    @property
    def id(self) -> str:
        """The same form as the id of a candidate."""
        return self.source if self.start is None else f"{self.source}#{self.start:.3f}-{self.end:.3f}"

    @property
    def length(self) -> float:
        return 0.0 if self.start is None else self.end - self.start

    def _overlap(self, other: "Segment") -> float | None:
        """Seconds in common; None when the two are not stretches of the same clip."""
        if other.source != self.source or (self.start is None) != (other.start is None):
            return None
        if self.start is None:
            return 0.0
        return max(0.0, min(self.end, other.end) - max(self.start, other.start))

    def iou(self, other: "Segment") -> float:
        """1 for the same photo, the intersection over union for two stretches of the same clip, else 0."""
        common = self._overlap(other)
        if common is None:
            return 0.0
        if self.start is None:
            return 1.0
        union = max(self.end, other.end) - min(self.start, other.start)
        return common / union if union > 0 else 0.0

    def shows(self, other: "Segment") -> bool:
        """Whether this (a clip of a plan) shows enough of `other`: all of a photo, 1 s or half of a stretch."""
        common = self._overlap(other)
        if common is None:
            return False
        if self.start is None:
            return True
        return common >= min(INCLUDED_MIN_SECONDS, INCLUDED_MIN_FRACTION * other.length) and common > 0

    def to_dict(self) -> dict:
        return {"source": self.source, "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        source, start, end = data["source"], data.get("start"), data.get("end")
        if not isinstance(source, str) or (start is None) != (end is None):
            raise ValueError("a segment has a source and either both start and end or neither")
        if start is None:
            return cls(source)
        start, end = float(start), float(end)
        if not 0 <= start < end:
            raise ValueError(f"invalid extent {start}-{end}")
        return cls(source, round(start, 3), round(end, 3))


@dataclass
class Label:
    segment: Segment
    important: bool | None = None  # an excluded moment the user wants in the video
    repetitive: bool | None = None  # a clip that repeats what the edit already shows
    length: str | None = None  # OK, TOO_LONG or TOO_SHORT
    cycle: str = ""  # run id of the cycle that set the flags last

    def flags(self) -> dict:
        return {name: getattr(self, name) for name in FLAGS}

    def to_dict(self) -> dict:
        return {**self.segment.to_dict(), **self.flags(), "cycle": self.cycle}

    @classmethod
    def from_dict(cls, data: dict) -> "Label":
        segment = Segment.from_dict(data)
        important, repetitive, length = data.get("important"), data.get("repetitive"), data.get("length")
        if important not in (None, True, False) or repetitive not in (None, True, False):
            raise ValueError("important and repetitive are true, false or null")
        if length not in (None, *LENGTHS):
            raise ValueError(f"length is one of {', '.join(LENGTHS)} or null")
        return cls(segment, important, repetitive, length, str(data.get("cycle", "")))


@dataclass
class Labels:
    labels: list[Label] = field(default_factory=list)

    def match(self, segment: Segment) -> Label | None:
        """The labelled segment that is the same as `segment` (the largest overlap, at least `MATCH_IOU`)."""
        best, best_iou = None, MATCH_IOU
        for label in self.labels:
            overlap = label.segment.iou(segment)
            if overlap >= best_iou:
                best, best_iou = label, overlap
        return best

    def set(self, segment: Segment, cycle: str, **flags) -> Label:
        """Record the flags of `segment` given in `cycle`; a matching label follows the new extent.

        Two segments of this cycle that both match one old label (a stretch split in two) keep
        two labels: the second one starts a new label rather than taking the first one's place.
        """
        label = self.match(segment)
        if label is not None and label.cycle == cycle and label.segment != segment:
            label = None
        if label is None:
            label = Label(segment, cycle=cycle)
            self.labels.append(label)
        label.segment, label.cycle = segment, cycle
        for name, value in flags.items():
            if name not in FLAGS:
                raise ValueError(f"unknown flag {name}")
            if value is not None:
                setattr(label, name, value)
        return label

    def flagged(self, name: str, value: object = True) -> list[Label]:
        return [label for label in self.labels if getattr(label, name) == value]

    def to_dict(self) -> dict:
        ordered = sorted(self.labels, key=lambda label: (label.segment.source, label.segment.start or 0.0))
        return {"version": LABELS_VERSION, "labels": [label.to_dict() for label in ordered]}


def load_labels(folder: Path) -> Labels:
    """The labels of `<workdir>/eval`; none yet before the first report."""
    data = _read(Path(folder) / LABELS_NAME)
    if data is None:
        return Labels()
    try:
        if data.get("version") != LABELS_VERSION:
            raise ValueError(f"version {data.get('version')!r}, expected {LABELS_VERSION}")
        return Labels([Label.from_dict(item) for item in data["labels"]])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise LabelsError(f"{Path(folder) / LABELS_NAME} is not usable: {error}") from None


def save_labels(folder: Path, labels: Labels) -> None:
    atomic_write_json(Path(folder) / LABELS_NAME, labels.to_dict())


def load_history(folder: Path) -> list[dict]:
    data = _read(Path(folder) / HISTORY_NAME)
    if data is None:
        return []
    if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION or not isinstance(data.get("cycles"), list):
        raise LabelsError(f"{Path(folder) / HISTORY_NAME} is not usable: unknown format")
    return data["cycles"]


def record_cycle(folder: Path, entry: dict) -> list[dict]:
    """Add the cycle of `entry` to the history, or replace it when its run was reported before."""
    cycles = [cycle for cycle in load_history(folder) if cycle.get("run_id") != entry["run_id"]]
    position = next((number for number, cycle in enumerate(cycles)
                     if cycle.get("created_at", "") > entry.get("created_at", "")), len(cycles))
    cycles.insert(position, entry)
    for number, cycle in enumerate(cycles, start=1):
        cycle["cycle"] = number
    atomic_write_json(Path(folder) / HISTORY_NAME, {"version": HISTORY_VERSION, "cycles": cycles})
    return cycles


def workdir_salt(folder: Path) -> bytes:
    """The secret of the anonymous ids, created on first use and readable only by the owner."""
    path = Path(folder) / SALT_NAME
    try:
        salt = path.read_bytes()
        if len(salt) == SALT_BYTES:
            return salt
    except FileNotFoundError:
        pass
    Path(folder).mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    salt = secrets.token_bytes(SALT_BYTES)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(salt)
    return salt


def anonymous_id(salt: bytes, segment: Segment) -> str:
    digest = hmac.new(salt, segment.id.encode("utf-8", "surrogateescape"), hashlib.sha256)
    return digest.hexdigest()[:ANONYMOUS_ID_CHARS]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise LabelsError(f"cannot read {path}: {error}") from None
