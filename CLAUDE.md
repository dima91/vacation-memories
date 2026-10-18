# Vacation Memories

Python application for Linux, fully local and run only in Docker containers (in development and on the server), that creates a memory video (configurable length, 5 minutes by default) from hundreds of photos and clips of a vacation: it selects the best moments, discards blurry and repeated scenes, orders them into a narrative with variety, and adds background music.

## Specification and workflow

- The specification is in SPEC.md. Do not import it with `@`: it is too long.
- Before every step, read "How to work", "Privacy and development data" and "Constraints", plus the sections listed for that step in "Sections to read for each step" (inside "Development plan"). Do not read the rest.
- Run one step at a time, then stop and report (tested / run but not tested / not verified).

## Non-negotiable rules

- Synthetic media only: MEDIA_DIR stays ./dev-media. Do not open, list or read folders with real media or `eval/` folders from real evaluations.
- Do not use `docker run` (only `scripts/test-docker.sh` may), nor `-v`/`--volume`/`--mount` with folders outside the repository, nor sudo, `sudo docker`, `--privileged` or the Docker socket.
- On the host, use only git, the repository files and the Docker CLI. Do not install Python, packages or FFmpeg on the host and do not create virtual environments on the host.
- After Step 1, do not modify `.env`: new variables go in `.env.example` (with a default value in the compose file) and in the report.
- Everything must work without a display or an interactive terminal (section "Headless execution").

## Commands

Code and tests run only in containers. Your shell has no terminal: always add `-T`.

- Build the images: `docker compose --profile dev build`
- Tests: `docker compose run --rm -T dev pytest`
- Tests with the real model (`-m ollama`): `docker compose up -d ollama`, then `docker compose run --rm -T dev pytest -m ollama`
- CLI trial (from Step 6, on the synthetic media in ./dev-media only): `docker compose build app`, then `docker compose run --rm -T app --duration 30 --output prova.mp4`
- `dev` sees the mounted repository immediately; `app` contains a copy of the code and must be rebuilt with `docker compose build app` after every change.
