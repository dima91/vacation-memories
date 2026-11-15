"""Makes the synthetic material the browser tests work on, in `dev-media`:

- `e2e-vacation`: 16 photos, 2 videos, 2 days;
- `e2e-music`: two synthetic tracks (sine tones), the music root of the tests (step U6). Run the tests with
  `MUSIC_DIR=./dev-media/e2e-music`, so that the editor lists these and never the folder of the real music.

    docker compose run --rm -T dev python frontend/e2e/make_media.py

Synthetic media only (section "Privacy and development data" of SPEC.md). Running it again leaves what exists alone.
"""

import math
import struct
import wave
from pathlib import Path

from vacation_memories.evaluate.synthetic_media import populate_vacation

FOLDER = Path("dev-media") / "e2e-vacation"
MUSIC = Path("dev-media") / "e2e-music"
TRACKS = {"tone-a.wav": (220.0, 40.0), "tone-b.wav": (330.0, 25.0)}  # name: (hertz, seconds)


def write_tone(path: Path, hertz: float, seconds: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * hertz * index / rate) * (0.4 + 0.6 * abs(math.sin(index / 4000)))))
            for index in range(int(seconds * rate))))


if FOLDER.exists():
    print(f"{FOLDER} exists: left as it is")
else:
    files = populate_vacation(FOLDER, photos=16, videos=2, days=2, video_scenes=(4.0, 4.0, 4.0), blurred=2, size=(320, 240))
    print(f"{len(files)} synthetic files written in {FOLDER}")

MUSIC.mkdir(parents=True, exist_ok=True)
for name, (hertz, seconds) in TRACKS.items():
    if (MUSIC / name).exists():
        print(f"{MUSIC / name} exists: left as it is")
    else:
        write_tone(MUSIC / name, hertz, seconds)
        print(f"{MUSIC / name} written")
