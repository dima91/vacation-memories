# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-13

First release.

### Added

- Scanning of a media folder (JPEG, PNG, HEIC/HEIF, MP4, MOV, MKV) with metadata extraction, capture dates with a
  documented fallback, and grouping by day independent of the server time zone.
- Analysis of every shot: scene detection, frame sampling, technical quality (blur, exposure, motion), perceptual
  duplicate detection, and a persistent cache so a second run only redoes selection, plan and rendering.
- Optional local vision analysis through Ollama (default model `qwen3-vl:4b-instruct-q4_K_M`) with a time budget,
  `--no-ai` and `--require-ai`, and the `bench vision` subcommand.
- Selection and ranking of scenes, and a soft narrative structure (opening, beginning, development, finale,
  closing) or a purely chronological one, for any target `--duration`.
- A validated `edit_plan.json` that can be edited by hand and rendered again with `--render-only`.
- Rendering with FFmpeg: dissolves and cuts, every resolution and aspect ratio (`--resolution`, `--aspect`),
  background music with fades and loops, quick `--preview`, parallel jobs, and checked, atomically written output.
- Headless operation: workdir lock, `status.json`, orderly shutdown on SIGINT/SIGTERM/SIGHUP, `--resume`, structured
  logs, and the `doctor` subcommand.
- The `evaluate` subcommand: a synthetic evaluation with known truth, and a blind review page to compare edits of a
  real vacation.
- Docker-only setup: a multi-stage image (`dev` and a minimal, non-root `runtime`), a hardened `app` service, Ollama
  on an internal network, optional Intel GPU and SELinux overrides, and the image version in its label.
- `scripts/test-docker.sh`, which tests the images, the compose files and a full job on synthetic media.
- A GitHub Actions workflow that runs the tests on every push and pull request, and the container tests on pull
  requests and version tags.

### Known limits

See "Known limits" in the README: duplicates are perceptual only, places are not known, and the scores are tuned on
synthetic media only.

[0.1.0]: https://github.com/dima91/vacation-memories/releases/tag/v0.1.0
