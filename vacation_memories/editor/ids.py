"""Opaque ids: media, clips and tracks are named in URLs by a hash, so file names never reach the URLs or nginx's logs."""

import hashlib
import re

OPAQUE_LENGTH = 16
OPAQUE_PATTERN = re.compile(rf"[0-9a-f]{{{OPAQUE_LENGTH}}}\Z")


def opaque_id(value: str) -> str:
    """A stable id for a relative path or an element id."""
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()[:OPAQUE_LENGTH]


def is_opaque_id(value: str) -> bool:
    return OPAQUE_PATTERN.match(value) is not None
