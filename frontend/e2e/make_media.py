"""Makes the synthetic vacation the browser tests work on: `dev-media/e2e-vacation` (16 photos, 2 videos, 2 days).

    docker compose run --rm -T dev python frontend/e2e/make_media.py

Synthetic media only (section "Privacy and development data" of SPEC.md). Running it again leaves an existing folder alone.
"""

from pathlib import Path

from vacation_memories.evaluate.synthetic_media import populate_vacation

FOLDER = Path("dev-media") / "e2e-vacation"

if FOLDER.exists():
    print(f"{FOLDER} exists: left as it is")
else:
    files = populate_vacation(FOLDER, photos=16, videos=2, days=2, video_scenes=(4.0, 4.0, 4.0), blurred=2, size=(320, 240))
    print(f"{len(files)} synthetic files written in {FOLDER}")
