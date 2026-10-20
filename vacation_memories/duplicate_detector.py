"""Near-duplicate detection with a replaceable similarity backend (level 1: perceptual hash).

`find_duplicates` works on `DuplicateItem`s (a photo, or a video segment with one or more
representative frames) and on any `SimilarityBackend`. The backend turns an analysis frame into a
signature and compares signatures; everything else (grouping, choosing the best representative,
recording the reason) is independent of it, so a semantic backend based on visual embeddings can
be added later without touching the callers.

Level 1 (`HashBackend`) uses the perceptual hash of `imagehash` (pHash, with dHash as an optional
confirmation) and the Hamming distance. Known limits: it does not recognize the same scene with a
different framing, zoom or moment, nor a photo and a video frame of the same scene; it also looks
at luma only, so two scenes with the same structure and different colors may be confused.

Grouping happens only in memory and in the plan: no file is touched.
"""

from dataclasses import dataclass
from typing import Protocol

import imagehash
import numpy as np
from PIL import Image

DEFAULT_MAX_HASH_DISTANCE = 6  # out of 64 bits
DEFAULT_HASH_SIZE = 8
_FRAMES_PER_BLOCK = 64
_POPCOUNT = np.array([bin(value).count("1") for value in range(256)], dtype=np.uint8)


class SimilarityBackend(Protocol):
    """Turns analysis frames into signatures and compares them."""

    name: str
    threshold: float  # similarity (0-1) at or above which two items are the same group

    def signature(self, gray: np.ndarray) -> np.ndarray:
        """1-D signature of a grayscale uint8 analysis frame."""

    def similarity(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        """Similarity in [0, 1] between every row of `left` and every row of `right`."""


class HashBackend:
    """pHash signatures compared with the Hamming distance.

    With `confirm_with_dhash` a pair is similar only if both its pHash and its dHash are within
    the threshold, which trims false positives.
    """

    name = "hash"

    def __init__(
        self,
        max_distance: int = DEFAULT_MAX_HASH_DISTANCE,
        hash_size: int = DEFAULT_HASH_SIZE,
        confirm_with_dhash: bool = False,
    ):
        if hash_size < 4 or hash_size % 4:
            raise ValueError("hash_size must be a multiple of 4")
        self.bits = hash_size * hash_size
        if not 0 <= max_distance < self.bits:
            raise ValueError(f"max_distance must be between 0 and {self.bits - 1}")
        self.hash_size = hash_size
        self.max_distance = max_distance
        self.confirm_with_dhash = confirm_with_dhash
        self.threshold = 1.0 - max_distance / self.bits

    def signature(self, gray: np.ndarray) -> np.ndarray:
        image = Image.fromarray(gray, "L")
        parts = [imagehash.phash(image, hash_size=self.hash_size)]
        if self.confirm_with_dhash:
            parts.append(imagehash.dhash(image, hash_size=self.hash_size))
        return np.concatenate([np.packbits(part.hash.flatten()) for part in parts])

    def similarity(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        width = self.bits // 8
        distance = np.zeros((len(left), len(right)), dtype=np.uint16)
        for start in range(0, left.shape[1], width):
            part = _POPCOUNT[left[:, None, start:start + width] ^ right[None, :, start:start + width]]
            distance = np.maximum(distance, part.sum(axis=2, dtype=np.uint16))
        return (1.0 - distance / self.bits).astype(np.float32)


@dataclass(frozen=True)
class DuplicateItem:
    """A candidate to compare: `signatures` has one row per representative frame."""

    id: str
    signatures: np.ndarray
    technical_score: float
    narrative_score: float | None = None  # known only after the AI analysis; ties are broken by it


@dataclass(frozen=True)
class GroupMember:
    id: str
    similarity: float  # to the representative (1.0 for the representative itself)
    reason: str | None  # None for the representative


@dataclass(frozen=True)
class DuplicateGroup:
    """`members[0]` is the representative; the others are the duplicates it replaces."""

    members: tuple[GroupMember, ...]

    @property
    def representative(self) -> str:
        return self.members[0].id

    @property
    def duplicates(self) -> tuple[GroupMember, ...]:
        return self.members[1:]


@dataclass(frozen=True)
class DuplicateResult:
    """Every item belongs to exactly one group (singletons included)."""

    backend: str
    threshold: float
    ids: list[str]
    similarity: np.ndarray  # item by item, float32, symmetric, 1.0 on the diagonal
    groups: list[DuplicateGroup]

    @property
    def duplicate_groups(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.duplicates]

    def representatives(self) -> set[str]:
        return {group.representative for group in self.groups}


def find_duplicates(
    items: list[DuplicateItem], backend: SimilarityBackend, threshold: float | None = None
) -> DuplicateResult:
    """Group items whose similarity reaches `threshold` (default: the backend's), by connected components."""
    threshold = backend.threshold if threshold is None else threshold
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids must be unique")
    for item in items:
        if item.signatures.ndim != 2 or len(item.signatures) == 0:
            raise ValueError(f"{item.id}: signatures must be a non-empty 2-D array")

    matrix = _item_similarity(items, backend)
    parents = list(range(len(items)))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, j in np.argwhere(np.triu(matrix >= threshold, k=1)):
        parents[find(j)] = find(i)

    components: dict[int, list[int]] = {}
    for index in range(len(items)):
        components.setdefault(find(index), []).append(index)
    groups = [_build_group(items, indices, matrix, backend.name) for indices in components.values()]
    return DuplicateResult(backend.name, threshold, ids, matrix, groups)


def _item_similarity(items: list[DuplicateItem], backend: SimilarityBackend) -> np.ndarray:
    """Similarity between items: the best match among their frames, computed block by block."""
    count = len(items)
    matrix = np.ones((count, count), dtype=np.float32)
    if count == 0:
        return matrix
    frames = np.concatenate([item.signatures for item in items])
    sizes = np.array([len(item.signatures) for item in items])
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])

    first = 0
    while first < count:
        last, total = first, 0
        while last < count and (last == first or total + sizes[last] <= _FRAMES_PER_BLOCK):
            total += sizes[last]
            last += 1
        block = backend.similarity(frames[starts[first]:starts[first] + total], frames)
        by_item = np.maximum.reduceat(block, starts, axis=1)
        matrix[first:last] = np.maximum.reduceat(by_item, starts[first:last] - starts[first], axis=0)
        first = last
    np.fill_diagonal(matrix, 1.0)
    return matrix


def _build_group(items: list[DuplicateItem], indices: list[int], matrix: np.ndarray, backend_name: str) -> DuplicateGroup:
    best = max(indices, key=lambda i: (items[i].technical_score, items[i].narrative_score or 0.0))
    members = [GroupMember(items[best].id, 1.0, None)]
    for index in indices:
        if index == best:
            continue
        similarity = float(matrix[best, index])
        reason = f"near-duplicate of {items[best].id} ({backend_name} similarity {similarity:.2f})"
        members.append(GroupMember(items[index].id, similarity, reason))
    return DuplicateGroup(tuple(members))
