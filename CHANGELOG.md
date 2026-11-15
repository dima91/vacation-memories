# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-15

### Added

- The interactive editor, a local web page served by the new `serve` subcommand behind an `nginx` proxy
  (`docker compose up -d editor nginx`, `http://127.0.0.1:8080`). The interface is in Italian, in React and TypeScript
  (`frontend/`), and the page makes no request outside the editor.
    - Projects: every folder under `MEDIA_DIR` gets its own working folder, with settings, analysis and plan job with
    progress and stop, and a resumable job after the editor stops.
    - Editing: add, reorder and remove clips, photo durations, trims of the moments, hide photos and delete moments (the
    planner never chooses them again), undo and redo, saving with a revision and a conflict dialog, and regenerating the
    plan with a copy in `plan_history/`.
    - Transitions and music: a library of transitions applied by drag or by buttons, several music tracks cut into parts
    with mute, fades and a shift against the video.
    - Preview and export: the preview of the whole video after every plan, an "outdated" mark, per-clip previews, and
    "Export video" with a name, progress, stop, download and error states.
    - Browser tests with Playwright (`e2e` service) on a synthetic vacation.
- Edit plan `schema_version: 2`: a stable `id` and an `origin` for every clip, `effects` (`fade_in`, `fade_out`,
  `dissolve`, `dip`), `excluded` moments and a `music` block. A schema 1 plan is still read and rendered. `--music` and
  the tracks of the plan are relative to `music_dir`, and `--preview` writes `render_report_preview.json`.
- `candidates.json`, written after every analysis, with every candidate, its scores and the reason it was left out.
- A full run or `--dry-run` over a plan with edits made by hand first copies it into `plan_history/` and keeps its
  excluded moments out of the new plan.
- Single-file launcher (`dist/vacation-memories`, built with PyInstaller by the `launcher-build` service): a Google Chrome
  `--app` window asks the folders on the first start, pulls the image from Docker Hub, starts the editor and stops the
  containers when the window is closed. `--install-desktop` adds the applications menu entry, `--reconfigure` asks
  the folders again, `--stop` stops it. Without Chrome it opens the default browser with `xdg-open` and waits for `--stop`.
- `VM_IMAGE` variable: image of `app` and `editor` (default `vacation-memories:local`).
- CI: on a release tag `vX.Y.Z`, once the tests pass, the `publish-image` job pushes the `runtime` image to Docker Hub as
  `<IMAGE_NAME>:X.Y.Z` (plus `latest` for a final version). `IMAGE_NAME` is a repository variable; the login uses the
  secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
- CI: the `release` job then builds the launcher and creates the GitHub release with it
  (`vacation-memories-X.Y.Z-linux-x86_64` and its `.sha256`). The notes are the `## [X.Y.Z]` section of this file; a tag
  without it is refused before anything is published.

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

See "Known limits" in the README: duplicates are perceptual only, places are not known, and the scores are tuned on synthetic media only.

- [0.1.0]: https://github.com/dima91/vacation-memories/releases/tag/v0.1.0
- [0.2.0]: https://github.com/dima91/vacation-memories/releases/tag/v0.2.0