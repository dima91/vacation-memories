# Vacation Memories — Specification

Act as a senior Python developer specialized in computer vision, video processing and local AI architectures.

## Goal

Python application for Linux, fully local and run **only in Docker containers** (in development and on the server), that creates a memory video (configurable length, 5 minutes by default) from hundreds of photos and clips of a vacation: it selects the best moments, discards blurry and repeated scenes, orders them into a narrative with variety, and adds background music.

**Priorities:** selection quality, reliability, local execution, untouched originals.

## How to work (rules for Claude Code)

- Implement **only the step the user indicates** in the message. When the step is done, stop and report. Do not anticipate later steps.
- Before starting, examine the current directory and reuse what already exists.
- Write tests together with the code, run them and fix the errors actually found.
- In the final report distinguish: **tested** / **run but not tested** / **not verified**. Do not declare working what you have not run.
- **A step is done only when it is tested and working**: all the checks listed for the step were actually run in the containers, the whole test suite passes (not only the new tests), and what the step delivers runs as described. Do not close a step with failing, skipped-without-reason or deleted tests, and do not weaken a test to make it pass. If something cannot be fixed or verified (e.g. a Docker command that cannot be run), say so explicitly in the report instead of presenting the step as complete.
- **All code runs in containers** (see "Docker image and Docker Compose"): tests, the synthetic media generator and CLI trials in the development container (`docker compose run --rm dev ...`); the production image is tried with the `app` service and with `scripts/test-docker.sh`, always on synthetic media. Python, dependencies and FFmpeg are not installed on the host, and the application is not run there. Dependencies have pinned versions; if they change, rebuild the images (`docker compose --profile dev build`). No privileged command without authorization.
- **Branches:** each step is developed on its own branch created by the user from `develop` before the step starts; work on the current branch and do not create or switch branches. Names: `feature/step-1-docker-env`, `feature/step-2a-scanner-metadata`, `feature/step-2b-runtime-profile-doctor`, `feature/step-3-technical-analysis`, `feature/step-4-plan-rendering`, `feature/step-5-ai-analysis`, `feature/step-6-selection-cli`, `feature/step-7-container-tests`. When the step is verified, the user merges it into `develop`.
- Do not add frameworks, databases or services without a concrete reason. For the analysis cache use the standard library `sqlite3` (see section 10); the plan, `status.json` and the reports stay JSON written atomically.

## Privacy and development data

- During development use **synthetic media only** (e.g. FFmpeg `testsrc`/`sine` sources, images generated with Pillow/NumPy) or small fixtures in `tests/fixtures/`. On the development PC `MEDIA_DIR` points to the synthetic media folder (`./dev-media`), never to real folders.
- **Do not open, list or read the folders with the user's real photos and videos.** What Claude Code reads in a session is sent to Anthropic. Runs on real data are launched by the user.
- The application makes no network connection except the one to Ollama, on the compose internal network, which in any case prevents it from reaching the Internet. A non-local Ollama host requires an explicit option (see "Headless execution").
- Evaluating perceived quality on real material (Milestone 2) is up to the user. Claude Code does not open the evaluation page, the thumbnails, the previews or the files in the `eval/` folder: it only sees the anonymized `eval_summary.json`, if the user decides to share it. On the development PC, the `eval/` folder copied from the server must be kept outside the repository.

## Constraints

- Linux, with Docker Engine and the Compose plugin as the only host dependencies, both on the development PC and on the server: the application runs only in containers (see "Docker image and Docker Compose"). No fixed hardware requirement: the machine's resources and capabilities are detected at runtime (see "Runtime profile") and the program must also work on CPU alone. It must also work in **headless** mode (server without a display, without an interactive terminal and without a GPU): see "Headless execution".
- Ollama is the `ollama` service of the Docker Compose file, reached by the app as `http://ollama:11434` (URL configurable with `--ollama-url` or in the configuration). For the vision model, one request at a time is assumed: the compose file starts Ollama with `OLLAMA_NUM_PARALLEL=1`.
- Originals strictly read-only. No deletion or modification, ever, not even optional.
- Output, cache and temporary files in a separate working folder (`--workdir`, default `./work`). No writes outside it, with a single exception: `--output` and `--log-file` given by the user as an absolute path. A relative `--output` is resolved inside the workdir. Nothing is ever written inside `--input`: an output path in there is an error (code 2). At startup it is also checked that the folders of `--output` and `--log-file` exist and are writable; otherwise code 2 immediately, not at the end of rendering.
- Concurrency toward the vision model: one request at a time by default (`ai.max_concurrency = 1`); a higher value only by explicit user choice, and the user must also raise `OLLAMA_NUM_PARALLEL` in the compose file accordingly.
- On error, preserve the useful state already produced (cache, partial plan).

## Runtime profile (adapting at runtime)

**Principle.** No hardware specification is hard-wired in the code, in the specification or in the defaults. At startup the program detects the machine's resources, derives a profile from them, shows it and records it. Every derived value can be overridden by configuration or CLI. The profile changes **speed and amount of work**, not the quality or selection criteria.

**Detection** (at startup, cheap; standard library and dependencies already in the stack only, `psutil` only if really needed):
- effectively usable CPU cores: the minimum of `os.sched_getaffinity` (respects affinity but not the CPU quotas of containers) and the cgroup quota (`cpu.max` in cgroup v2, `cpu.cfs_quota_us` and `cpu.cfs_period_us` in v1);
- available RAM: the minimum of `MemAvailable` from `/proc/meminfo` (in a container it shows the host's RAM) and the cgroup headroom, computed on the working set: `memory.max` minus (`memory.current` − `inactive_file` from `memory.stat`) in v2, equivalents in v1 (`memory.usage_in_bytes` − `total_inactive_file`). `memory.current` alone includes the page cache, which after reading many media files fills the limit even though it is reclaimable; if `memory.max` is `max` there is no limit; free space in `--workdir` and in `<workdir>/tmp` (`shutil.disk_usage`);
- FFmpeg version, encoders and available accelerations (`ffmpeg -encoders`, `-hwaccels`);
- Ollama reachable, installed models (e.g. `/api/tags`) and state of loaded models (e.g. `/api/ps`);
- in Milestone 2, providers available for ONNX Runtime or PyTorch if embeddings are used;
- with AI active and the model not yet loaded, the memory it will occupy (estimated from the model size in `/api/tags`) is subtracted from the host's free RAM (`MemAvailable`), because Ollama will load it on the same machine. If it is already loaded (`/api/ps`), that memory is already out of `MemAvailable` and is not subtracted again. It does not weigh on the app's cgroup limit, because Ollama runs in another container. This also applies when the model runs on the integrated GPU, which uses system RAM;
- where the model runs in Ollama (GPU, CPU or mixed, from the ratio between `size_vram` and `size` in `/api/ps`, as `ollama ps` does). It is only known when the model is loaded: the program records it in `runtime_profile.json` after the calibration warm-up, and `doctor` reports it if the model is already loaded. It is informative only: calibration measures the real speed anyway.

If a detection fails: conservative values, a warning in the log, never a crash.

**Calibration: measure, don't assume.**
- **AI:** at the start of the AI phase a short warm-up on a few frames (e.g. 3-5) measures seconds per frame on the machine in use. With a time budget (`--ai-time-budget`, configurable default) the program computes how many frames to analyze: `n = min(candidates, budget / seconds_per_frame)`. Candidates are ordered by priority (best non-semantic scores first), so partial coverage is still useful and the frames not analyzed fall back to the non-semantic score alone. A faster machine analyzes more frames with no changes.
- **Reproducible alternative:** `--ai-frames N` fixes the number of frames, so the result is the same on different machines. The two options are mutually exclusive.
- **Rendering:** the encoding time of a test clip feeds the estimate shown by `--dry-run` and the warnings about long renders.

**Derived parameters** (default `auto` unless stated otherwise, overridable):
- `--jobs`: number of FFmpeg and analysis processes in parallel, depending on cores and RAM. Threads per process are chosen so that jobs × threads does not exceed the cores (no oversubscription). The AI has a separate concurrency (`ai.max_concurrency`, default 1).
- Size of the assembly blocks (section 8) and memory limits for decoding and in-memory cache, reduced if available RAM is low.
- `--hwaccel auto|none|<name>` (default `none`): with `auto` a hardware encoder is used only if a short test encode succeeds and produces a valid file; otherwise fall back to software with a warning.
- Disk space: estimate of what is needed (segments, thumbnails, previews) **before** starting. If it is insufficient, a clear error immediately and not halfway through the work.
- **Not derived from hardware**, for reproducibility: model, prompt, image resolution for the AI, thresholds and weights.

**Reproducibility and cache**
- The parameters that change the analysis result (model, prompt, resolution, thresholds) go into the cache keys. Performance-only parameters (`jobs`, threads, blocks, `hwaccel`) do **not**, so changing profile does not invalidate the cache. On another PC the cache stays valid if the media have the same relative paths, sizes and mtime (see section 10).
- A `runtime` block in `edit_plan.json` records the profile used and `ai_coverage` (frames analyzed out of candidates).
- A plan can be rendered on another machine with `--render-only`: the renderer adapts to the new machine without modifying the plan. The source paths in the plan are relative to `--input`, so on the new PC it is enough to say where the media folder is.

**Transparency.** At startup the detected profile and the chosen values are printed and saved in `<workdir>/runtime_profile.json`. `--dry-run` shows them together with the planned plan, without rendering (see "Execution modes and subcommands").

**Tests.** The detection functions sit behind a mockable interface (fake contents of `/proc/meminfo` and of the cgroup files, number of cores, output of `ffmpeg -encoders`, Ollama responses), so no test depends on real hardware:
- synthetic profiles: small machine (few cores and little RAM), large machine, without Ollama, without the required model, FFmpeg without accelerations, acceleration whose test fails (fallback to software);
- derived parameters respect the constraints (jobs × threads ≤ cores, reduced blocks with little RAM);
- expected AI budget with a fake backend of known speed; `--ai-frames` produces the same frame selection on different profiles;
- changing profile does not invalidate the cache;
- cgroup limits (RAM and CPU) lower than the host values: the profile uses the lowest value;
- simulated `/api/ps` with the model on GPU, on CPU and mixed: the profile records the right value; with no model loaded it reports "unknown".

**Phased implementation:** `runtime_profile` with detection and override in Step 2; `--jobs` and encoder choice in Step 4; calibration and AI budget in Step 5. `--hwaccel auto` may slip to a later milestone if it complicates rendering.

## Headless execution (server without a display)

**Principle.** The program must be able to run in an SSH session, in `tmux` or from `cron` (always as `docker compose run`, with `-T` when there is no terminal), on a machine without a display, without an interactive terminal and without a GPU. No requirement for a graphical environment, a TTY or human intervention during execution.

**No graphical dependencies**
- OpenCV in its **headless** variant (`opencv-python-headless`): the GUI variant requires graphics libraries (e.g. `libGL`) that are missing on servers. PySceneDetect must be installed with its headless extra if the chosen version offers it; otherwise install headless OpenCV separately. The non-headless `opencv-python` package must not appear among the dependencies.
- No `cv2.imshow`, windows, `webbrowser.open` or graphical backends (if matplotlib is ever needed: `Agg` backend). The program does not depend on `DISPLAY` or `WAYLAND_DISPLAY`.
- Fonts: synthetic media and tests do not use system fonts (missing on minimal servers). Use a font included in the repository with a compatible license, or Pillow's built-in one. FFmpeg's `drawtext` only with an explicit `fontfile`.

**Non-interactive execution**
- No `input()` and no confirmation prompts. In case of ambiguity a documented default is applied and logged, or the program exits with a clear error. With stdin closed or `/dev/null` the behavior is identical.
- Every FFmpeg/FFprobe subprocess starts with `-nostdin` (otherwise, in the background or from `cron`, it can hang or consume stdin), `-hide_banner`, reduced logging and `stdin=subprocess.DEVNULL`.
- No progress bars with carriage returns or ANSI sequences if stderr is not a terminal: timestamped log lines, at intervals (e.g. every 10-30 s and at every phase change).
- `--log-file` (default `<workdir>/run.log`), `--log-format text|json`, `--quiet`. The single configuration can also be loaded from a file (`--config`, TOML; if the option is missing, the file named by the `VACATION_MEMORIES_CONFIG` variable is used, if set) as well as from the CLI, so a scheduled job does not depend on a long command line. Order of precedence: CLI, then the program's own variables (`VACATION_MEMORIES_*`), then the configuration file, finally the defaults. Variables of other programs, such as `OLLAMA_HOST`, count only as a fallback after the configuration. An empty variable counts as unset.

**Monitoring and exit codes**
- `<workdir>/status.json`, updated atomically every few seconds: PID and hostname (in the compose file, the container ID), phase, items done and total, estimated remaining time, start time and last update, state (`running`, `done`, `failed`, `interrupted`).
- Exit codes: `0` completed and verified; `1` generic error; `2` invalid usage or configuration; `3` final verification failed; `4` missing requirements (FFmpeg, space; Ollama or model only with `--require-ai`); `5` workdir already in use; `128 + signal number` for an orderly interruption (130 SIGINT, 143 SIGTERM, 129 SIGHUP), resumable with `--resume`. Insufficient material is not an error: code 0 with a warning.

**Signals, SSH sessions and resumes**
- SIGINT, SIGTERM and SIGHUP cause an orderly shutdown: terminate child processes (process group), complete or discard the unit of work in progress (the shutdown must finish within a few seconds, target 5 s, discarding the segment in progress if needed: it must stay comfortably under the 10 s that `docker stop` grants by default before SIGKILL, even though the compose file grants 30), save cache and state, write `status.json`, exit with the code above. No signal must leave orphan FFmpeg processes or corrupt files.
- All state writes to files (plan, `status.json`, reports) are atomic: temporary file in the same folder and `os.replace`; the analysis cache uses `sqlite3` transactions. Even a `kill -9` or a power cut leaves no truncated files.
- The final video is written as `<name>.partial.mp4` and renamed to the final name only after the FFprobe checks. If they fail the `.partial.mp4` file remains, with exit code 3. The final extension stays `.mp4` because FFmpeg infers the format from the extension.
- **Workdir lock:** `fcntl.flock` on `<workdir>/.lock`. The lock releases itself if the process dies, so no orphan locks remain. A second instance on the same workdir exits immediately with code 5 and names the active one (hostname, PID and start time, from `status.json`): every `docker compose run` is a different container, so the PID alone is not enough to recognize it. Different workdirs are independent.
- The README explains how to launch long jobs with `tmux` or `nohup`, instead of a bare SSH session.

**Resources on shared machines**
- Detection respects the cgroup limits (see "Runtime profile").
- `--nice N` (default 0) lowers the priority of the process and its children.
- **Temporary files under `<workdir>/tmp`:** `TMPDIR` set for the process and its children and `tempfile` configured accordingly. On many servers `/tmp` is a small in-RAM tmpfs, and FFmpeg would fill it. Free space is checked before starting.
- `--input` may be on NFS/SMB or on a read-only filesystem: nothing is ever written to `--input`.

**Ollama on the server**
- The program neither starts nor stops Ollama: Docker Compose starts it (see "Docker image and Docker Compose").
- If Ollama is not reachable or the model is missing, fall back to `--no-ai` with a warning, **except** with `--require-ai`, which exits with code 4. It is meant for unattended runs, where a silently AI-less result would be a surprise. `--require-ai` applies only to the phases that use the AI: `--render-only`, `evaluate report`, `doctor --no-ai` and `--dry-run` with the analysis already fully cached do not contact Ollama and do not fail if it is missing. `--require-ai` and `--no-ai` together on the command line are contradictory (code 2); with `require_ai = true` in the configuration, `--no-ai` on the command line takes precedence.
- **Non-local URL** (other than `localhost`, `127.0.0.0/8`, `::1`): requires an explicit `--allow-remote-ollama` (or `allow_remote_ollama = true` in the configuration, as the compose file does for its internal network) and produces a warning in the log, because the images travel in base64 over unencrypted HTTP. URL precedence: `--ollama-url`, then the configuration, then `OLLAMA_HOST`, finally `http://localhost:11434`. `OLLAMA_HOST` is normalized as Ollama does (without a scheme `http://` is added, `0.0.0.0` counts as `localhost`) and the same rule on non-local hosts applies.

**Environment, locale and time zone**
- Works with `LANG=C`: text I/O is forced to UTF-8 (log, JSON, console, with `errors="backslashreplace"`). File names that cannot be decoded as UTF-8 neither crash the program nor get lost (`os.fsdecode`, `surrogateescape`, lossless serialization in the JSON files).
- **Grouping by day does not depend on the server's time zone:** "naive" date and time are used as written in the metadata (EXIF, container tags). The `mtime` fallback uses `--assume-tz` (default: system time zone). The plan records `date_source` and the assumed time zone for each file. Moving the job to a UTC server does not change the division into days.
- **Private permissions:** `umask 077` at startup and `<workdir>` with `0700` permissions. It contains thumbnails, previews and metadata of private photos, and the server may be multi-user.

**Installation check: `doctor`**
`doctor` (`docker compose run --rm app doctor`) runs checks without touching the media or doing heavy work, prints a report and exits with code 4 if anything blocking is missing:
- Python version (3.11 or higher, with a clear message otherwise);
- `ffmpeg` and `ffprobe` present, version, `libx264` and AAC encoders (some minimal builds do not include them) and all the filters used by the renderer, listed in a single place in the code and shared with the build check (`xfade`, `zoompan`, `fps`, `scale`, `boxblur`, `overlay`, `format`, `setsar` and, for audio, `afade`, `acrossfade`, `anullsrc`); `zscale` and `tonemap`, used for the HDR → SDR conversion, are optional, with documented degradation (section 8);
- `pillow-heif` imports and registers its decoder (if it fails, HEIC files are skipped with a warning) and the other optional libraries, with documented degradation;
- writable workdir, free space, `<workdir>/tmp`, detected cgroup limits;
- Ollama reachable and model present: warning if missing, blocking (code 4) only with `--require-ai`, not checked with `--no-ai`. It is the same rule as normal execution.

**Reproducible and offline installation.** Pinned Python dependencies (`requirements.txt` with versions, preferably with hashes too), installed only in the image. On the host no package besides Docker: FFmpeg is in the image, and the `pillow-heif` wheels already include libheif. The app downloads nothing at runtime: the model is downloaded by the `ollama-pull` service on first start (or by Step 0), and the embedding weights are in `<workdir>/models/`. For a server without Internet, images and the models volume are transferred (see "Docker image and Docker Compose").

**Phased implementation:** `doctor`, logging, lock, atomic writes, `TMPDIR` in the workdir and the bundled font in Step 2; `-nostdin`, signal handling and `.partial.mp4` files in Step 4; headless tests and the deployment README in Step 6.

## Docker image and Docker Compose

**Principle.** Docker is the only way to develop and run the application, on the development PC and on the server alike. The host needs only Docker Engine and the Compose plugin (plus git and Claude Code on the development PC). Python, FFmpeg, the dependencies and Ollama live in the containers, and the application is neither intended nor supported outside the container. A single `compose.yaml` serves both machines: only the `.env` variables change.

**What to produce**
- In Step 1: multi-stage `Dockerfile` with `dev` and `runtime` targets, `.dockerignore`, `compose.yaml` with the `dev`, `app`, `ollama` and `ollama-pull` services, `compose.intel-gpu.yaml` (Intel GPU for Ollama: `/dev/dri` and `OLLAMA_IGPU_ENABLE=1`), `compose.selinux.yaml` (only for hosts with SELinux), `docker/compose.toml` (app configuration in the compose file), `.env.example` to copy to `.env` (not versioned).
- In Step 7: `compose.test.yaml` (override for tests with a simulated Ollama), `scripts/test-docker.sh` (container tests, run on the host with the Docker CLI alone) and the "Docker" section of the README.

**Building the image**
- Debian slim base with Python 3.11 or higher, with a pinned tag (never `latest`) and the digest noted for releases. Avoid Alpine (musl): there are fewer precompiled wheels for NumPy and OpenCV.
- FFmpeg from the base distribution's package (`--no-install-recommends`, apt lists cleaned). The build verifies the presence of `libx264`, the AAC encoder and the same filters checked by `doctor` (list in "Installation check: `doctor`"), except `zscale` and `tonemap`, and fails if they are missing.
- `base` stage: the starting image with FFmpeg, the native libraries needed at runtime (few and without graphical dependencies), the non-root user and the environment variables described here. The other stages derive from `base`, so `dev` and `runtime` have the same FFmpeg and the same libraries.
- `builder` stage (from `base`): installs the runtime dependencies from `requirements.txt` into a venv inside the image (with `--require-hashes` if hashes are present). The dependencies layer is separate from the code layer, to take advantage of the cache. Only `opencv-python-headless`.
- `dev` stage (from `builder`): adds `requirements-dev.txt` (pytest and development tools). It does not contain the code: in development the repository is mounted in the container. It is used for development and tests.
- `runtime` stage (from `base`): the venv copied from `builder` and the code, nothing else. No compilers, tests, fixtures or `.git`.
- Non-root user with a fixed UID. The image creates `/work` owned by that user and leaves the current directory at `/`, so the default workdir `./work` coincides with `/work` and `doctor` works even without a volume. Environment variables: `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, `LANG=C.UTF-8`, `HOME=/work`, `XDG_CACHE_HOME=/work/.cache`, `USER=app`, `LOGNAME=app` (with a UID that has no entry in `/etc/passwd`, `getpass.getuser()` fails, and libraries such as torch, which Milestone 2 will use, call it). The image must also work with an arbitrary UID, as in the compose file, where `/work` is the mounted folder: nothing is written outside `/work`.
- `runtime` target: `ENTRYPOINT ["python", "-m", "vacation_memories"]` in exec form, with no `CMD` that starts work. The arguments passed to the container are those of the CLI, including the `doctor`, `bench` and `evaluate` subcommands.
- OCI label with the version (from a build arg). `doctor` prints the image version and the FFmpeg version.
- Architecture equal to the machine's (amd64 or arm64); for another one, `docker buildx build --platform`. If a dependency has no wheel for the architecture, the README says so.
- `.dockerignore` excludes `.git`, `.env`, caches, `work/`, `dev-media/`, `music/` and every media file outside `tests/fixtures/`, so no media and no local configuration end up in the build context.
- `tzdata` (Python package) among the dependencies, so that `--assume-tz` works even where the system time zone data is missing.

**Services**
- **`dev`** (profile `dev`, development PC only): `dev` image with the repository mounted at `/src`, which is also the container's current directory (`PYTHONPATH=/src`). It runs as `${HOST_UID}:${HOST_GID}`, so the files created in the repository belong to the user. `/tmp` is a tmpfs and `PYTHONDONTWRITEBYTECODE=1` avoids dirtying the repository. It has `HOME=/tmp` and `XDG_CACHE_HOME=/tmp/.cache`, because with the host's UID the image's HOME (`/work`) may not be writable, and, for the integration tests, `OLLAMA_HOST=http://ollama:11434` and `VACATION_MEMORIES_MODEL=${OLLAMA_MODEL}`, so they use the same model as `app`. It does not use `docker/compose.toml`, which sets `input = "/media"`. It sits only on the internal `backend` network: it reaches Ollama for the integration tests but not the Internet, so dependencies are changed only by rebuilding the image (`docker compose build dev`). **It never mounts `MEDIA_DIR`**: it works on the synthetic media, generated in the tests' temporary files or in the repository's `./dev-media` folder (ignored by git). It does not depend on `ollama`, so the normal tests do not start it: for the `-m ollama` tests, first start `docker compose up -d ollama`, and if Ollama does not respond or the model is not present those tests are skipped with a clear message. Usage: `docker compose run --rm dev pytest`, `docker compose run --rm dev python -m vacation_memories ...`.
- **`app`:** the `runtime` image, used with `docker compose run --rm app <CLI arguments>`. It depends on `ollama` (`condition: service_healthy`) and on `ollama-pull` (`condition: service_completed_successfully`): Compose starts Ollama, waits for it to respond and for the model to be present, then launches the app.
- **`ollama`:** official `ollama/ollama` image with a pinned tag (never `latest`), the same one the user chose for Step 0 and gave in the Step 1 request, version 0.30 or later: Qwen3-VL requires at least 0.12.7, and from 0.30 the Vulkan backend, which the integrated Intel GPU needs, is enabled by default. `OLLAMA_NUM_PARALLEL=1`, plus `OLLAMA_VULKAN` only if set in `.env`. The integrated GPU is enabled with `OLLAMA_IGPU_ENABLE=1`, which the `compose.intel-gpu.yaml` override sets (see "Integrated Intel GPU for Ollama"), not the base file. Models in the **external** volume `vacation-ollama-models` (`external: true`, mounted at `/root/.ollama`), created once with `docker volume create vacation-ollama-models`. Being external, it can already be populated by Step 0 and not even `docker compose down -v` deletes it. If it is missing, Compose gives a clear error. No published ports. Healthcheck that verifies the server responds (e.g. `ollama list`), with `interval: 5s`, `timeout: 5s`, `retries: 12` and `start_period: 10s` (with the default values the first check comes after 30 s). Log with rotation (`json-file`, `max-size: 10m`, `max-file: 3`). It stays active until `docker compose stop` or `down` is run.
- **`ollama-pull`:** startup service with the same image, which talks to the server through `OLLAMA_HOST=http://ollama:11434` and depends on `ollama` (`condition: service_healthy`). It downloads the configured model (`OLLAMA_MODEL`; the default `qwen3-vl:4b-instruct-q4_K_M` is defined in a single place in the compose file, for example in an `x-` extension field, and applies to `ollama-pull`, `app` and `dev`) **only if it is missing** (check with `ollama show` before `ollama pull`), then exits. With the model already present no Internet is needed. The first download is about 3.3 GB.

Networks:
- **`backend`, internal** (`internal: true`): the only network of `app` and `dev`. They talk only to Ollama and **have no Internet access**, so not even a bug in the code can make images leave the machine.
- **`egress`:** only for `ollama`, which sits on both networks and must be able to download models. `ollama-pull` sits only on `backend`: it asks the `ollama` server to download, and the server does it.
- No published ports: Ollama is not reachable from the network. From the host it is reachable only by knowing the container's IP, because `egress` is an ordinary bridge network.

Configuration:
- **`docker/compose.toml`**, mounted read-only and given to the app with `VACATION_MEMORIES_CONFIG`: `input = "/media"`, `workdir = "/work"`, Ollama URL `http://ollama:11434`, `allow_remote_ollama = true`, `require_ai = true`. The permission for a non-local host is explicit and versioned; traffic stays in the internal network anyway. `require_ai` makes a job fail immediately if Ollama does not respond; `--no-ai` on the command line takes precedence over the configuration. The `dev` service does not use this file: the integration tests explicitly pass the permission for the non-local host, and the normal tests do not depend on these variables.
- **`.env`**, copied from `.env.example`: `MEDIA_DIR`, `MUSIC_DIR` (default `./music`), `WORK_DIR` (default `./work`), `HOST_UID`, `HOST_GID`, `OLLAMA_MODEL`, `COMPOSE_FILE`, the optional `OLLAMA_VULKAN` (see "Integrated Intel GPU for Ollama"), the optional `ASSUME_TZ` (e.g. `Europe/Rome`), `APP_VERSION` (build arg for the image label, optional). `MEDIA_DIR`, `HOST_UID` and `HOST_GID` are required (`${VAR:?message}`), so a missing variable gives a clear error. `.env.example` values, meant for the development PC: `MEDIA_DIR=./dev-media` (synthetic media), `HOST_UID=1000` and `HOST_GID=1000` (example, to be replaced with `id -u` and `id -g`) and `COMPOSE_FILE=compose.yaml:compose.intel-gpu.yaml` (GPU active). On the server, change `MEDIA_DIR`, with the real folder, and `HOST_UID` and `HOST_GID` if the user has different values there.
- **`app` service:** `user: "${HOST_UID}:${HOST_GID}"`, `init: true`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `stop_grace_period: 30s`; `MEDIA_DIR` and `MUSIC_DIR` read-only. It receives `VACATION_MEMORIES_CONFIG`, `VACATION_MEMORIES_MODEL=${OLLAMA_MODEL}` (so app and `ollama-pull` always use the same model) and `VACATION_MEMORIES_ASSUME_TZ=${ASSUME_TZ:-}`.

**Same commands on the development PC and the server**

| Operation | Command | Where |
|---|---|---|
| Tests | `docker compose run --rm dev pytest` | Development PC |
| Integration tests with the real model (synthetic images only) | `docker compose up -d ollama`, then `docker compose run --rm dev pytest -m ollama` | Development PC |
| Container tests (from Step 7) | `scripts/test-docker.sh` | Development PC |
| Installation check | `docker compose run --rm app doctor --require-ai` | both |
| Model benchmark | `docker compose run --rm app bench vision` | both |
| Full job | `docker compose run --rm app --music /music/<track> --duration 300 --output memories.mp4` | both |
| Resume | same command with `--resume` | both |
| Render only from a plan (without Ollama) | `docker compose run --rm --no-deps app --render-only --output memories.mp4` | both |
| Evaluation (Milestone 2) | `docker compose run --rm app evaluate prepare ...` | server |
| Evaluation report (Milestone 2, without Ollama) | `docker compose run --rm --no-deps app evaluate report ...` | server |

- `--input` and `--workdir` come from the compose configuration; passing them is still possible.
- Without `--no-deps` Compose starts `ollama` and `ollama-pull` anyway (and downloads the model if it is missing): for commands that do not use the AI add `--no-deps`. From `cron`, with `nohup` or from Claude Code's shell, which have no terminal, add `-T`.
- `app` contains the code copied into the image: after every code change or update, rebuild (`docker compose build app`). `dev`, instead, sees the mounted repository immediately.
- On the development PC `app` works on the synthetic media of `MEDIA_DIR=./dev-media`, on the server on the real ones.
- At the end of a job the app asks Ollama to unload the model (section 5), so the model's RAM is freed on its own. `docker compose stop ollama` also stops the server; `docker compose down` stops everything and the external models volume remains.
- **Changing model:** set `OLLAMA_MODEL` in `.env`. `ollama-pull` downloads it if it is missing, and the AI analysis is redone because the cache depends on the model.
- **Resources:** Ollama and the app share the machine. Optional per-service limits (`cpus`, `mem_limit`) are documented in the README.
- **Server without Internet:** the README explains how to transfer the images (`docker save` of the app and of `ollama/ollama`) and the models volume (`tar` archive of the `vacation-ollama-models` volume prepared on a machine with Internet). On that server the image is not built, because the build downloads the base image and the packages: the transferred one is used, with `docker load`.

**Common rules for container execution**
- **`app` volumes:** media read-only at `/media`, music read-only at `/music`, writable working folder at `/work` (it also contains temporary files and output). An absolute `--output` or `--log-file` must be under `/work`, the only writable folder. Paths in the plan are relative to `--input`, so a plan stays valid between the development PC and the server.
- **Host folders created beforehand:** if a mounted path does not exist, Docker creates it owned by root and the non-root container cannot write to it. In the compose file the bind mounts use the long syntax with `create_host_path: false`, which turns the case into a clear error.
- **Hosts with SELinux enabled in the Docker daemon** (`docker info` shows `selinux` among the Security Options; typical of Fedora and RHEL): bind mounts are not accessible to containers without a label. `MEDIA_DIR` is not relabeled with `:z`, because that would change the SELinux labels of the originals: the `compose.selinux.yaml` override, to be added to `COMPOSE_FILE` only on these hosts, sets `security_opt: [label=disable]` for `app` and `dev`.
- **User:** `app` and `dev` run with the UID and GID of the host user, so the files in `/work` and in the repository belong to them.
- **`app` hardening:** read-only filesystem (everything lives in `/work`, see `TMPDIR` in the workdir), `cap_drop: ALL`, `no-new-privileges`. Never `--privileged`, never the Docker socket mounted, in any service.
- **Resource limits:** the container's are read by the runtime profile's cgroup detection.
- **Time zone:** in the container the system time zone is usually UTC. Grouping by day does not depend on it (naive dates from the metadata), but the `mtime` fallback does: in that case set `ASSUME_TZ` in `.env` or pass `--assume-tz` (e.g. `Europe/Rome`).
- **Signals:** `init: true` to clean up child processes. Ctrl-C or `docker stop` cause an orderly shutdown (code 130 or 143) and resuming with `--resume`. Shutdown time granted: 30 s (`stop_grace_period`).
- Video acceleration (FFmpeg on `/dev/dri`) is not included in the app image: the encoder stays software and `--hwaccel auto` falls back to software. The integrated Intel GPU is used by Ollama (see below).

**Integrated Intel GPU for Ollama**

Ollama uses the integrated Intel GPU through Vulkan, **both on the development PC and on the server**. The Vulkan backend is already included in the official `ollama/ollama` image and turns on by itself when the container sees the GPU devices; nothing needs to be installed in the container. For integrated GPUs `OLLAMA_IGPU_ENABLE=1` is also needed, which the override sets (see below). The kernel driver (`i915` or `xe`) stays the host's.

- **`compose.intel-gpu.yaml` override:** adds to the `ollama` service only `devices: ["/dev/dri:/dev/dri"]` and `OLLAMA_IGPU_ENABLE=1` in the environment. Only `/dev/dri`: `/dev/kfd` belongs to AMD/ROCm, does not exist on Intel iGPUs and would make startup fail.
- **`OLLAMA_IGPU_ENABLE=1` forces the use of the integrated GPU.** In Ollama's source, during detection, integrated GPUs are dropped unless the variable is set (CUDA devices and a single AMD APU are exceptions): an Intel iGPU, which goes through Vulkan, would stay unused and the model would run on the CPU. It is a fixed value of the override, not a `.env` variable, so removing the override from `COMPOSE_FILE` on machines without a GPU makes `/dev/dri` and the variable disappear together. `OLLAMA_VULKAN=0` remains stronger: without Vulkan an Intel iGPU is not used. When Ollama drops an iGPU it writes it to the log (message like `dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1`); it has not been verified from which Ollama version the filter exists, and with a version that lacks it the variable is harmless.
- **Active by default:** `.env.example` contains `COMPOSE_FILE=compose.yaml:compose.intel-gpu.yaml`, and Compose reads this variable by itself, so the commands stay identical. The override stays a separate file because `devices` makes startup fail where `/dev/dri` does not exist: on a machine without a GPU, just remove the override from `COMPOSE_FILE`.
- **Permissions:** in the official Ollama image it runs as root and accesses `/dev/dri` with nothing else. If one day it ran as a non-root user, `group_add` with the numeric GID of the device's group (usually `render`) would be needed. On hosts with SELinux (Fedora, RHEL and derivatives) `container_use_devices` must be enabled (`sudo setsebool -P container_use_devices=1`).
- **Automatic fallback:** if Vulkan does not find the GPU, Ollama uses the CPU and the program works anyway. `OLLAMA_VULKAN=0` in `.env` disables Vulkan without removing the override; after changing it, `docker compose up -d ollama` recreates the container with the new setting. It is useful for CPU-versus-GPU benchmarks and on iGPUs where Vulkan is unstable.
- **Memory:** the iGPU has no memory of its own and uses system RAM. The runtime profile reserves the model's memory even when it runs on the GPU.
- **Check:** with the model loaded (during a job's AI phase: at the end the app unloads it), `docker compose exec ollama ollama ps` must show GPU (or a mixed percentage) in the PROCESSOR column, and the `ollama` logs the Vulkan device found; if `ollama ps` says CPU, the `docker compose logs ollama` log says whether the iGPU was dropped or not found. The program records the same information in `runtime_profile.json`.
- **The gain must be measured:** on an iGPU, with a quantized 4B model, it is not guaranteed. Step 0 measures it on the server, with and without the GPU; the Step 5 calibration then adapts by itself to the real speed.

**Constraints for Claude Code**
- On the host Claude Code uses only git, the repository files and the Docker CLI. It runs code, tests, the synthetic media generator and CLI trials **only** in containers: as a rule in `dev` (`docker compose run --rm dev ...`), and with `app` or `scripts/test-docker.sh` to try the production image, on the synthetic media of `./dev-media`. It does not install Python, packages or FFmpeg on the host and does not create virtual environments on the host.
- Synthetic media only: no volume and no `.env` pointing to the user's real folders. On the development PC `MEDIA_DIR` stays `./dev-media`, and Claude Code does not change it.
- No `--privileged`, no mounted Docker socket, no `sudo docker` (if Docker requires privileges, ask the user), no push to a registry without authorization. No `docker run` and no `-v`/`--volume`/`--mount` with host folders outside the repository: containers are started with the compose services, and `scripts/test-docker.sh` is the only script that may use `docker run`, always without mounting folders outside the repository. In `docker compose run` commands use `-T`, because the shell has no terminal. After Step 1 `.env` is not modified: a new variable goes in `.env.example`, with a default in the compose file, and in the report.
- Models: normal tests use a simulated Ollama. The `-m ollama` integration tests use the real model the user already downloaded in Step 0, with synthetic images only; no other model downloads.
- No secrets, media or models in the image.
- If a Docker command cannot be run, the report marks it as **not verified**: an image or compose file that was never started is not declared working.

**Docker tests** (`scripts/test-docker.sh`, Step 7; synthetic media only):
- `docker compose --profile dev build` succeeds, and `docker compose run --rm dev pytest` passes;
- `docker compose run --rm --no-deps app doctor --no-ai` exits with code 0 without starting Ollama, and FFmpeg has `libx264`, AAC and all the filters checked by `doctor`;
- the `runtime` image runs as non-root and contains no media files, `.git`, `.env`, fixtures or non-headless `opencv-python`;
- full app cycle on synthetic media with the test compose file: produces a video that passes the FFprobe checks, the media remain unchanged and nothing is written outside `/work`;
- `docker stop` during rendering: exit with 143 within 10 s (even if the compose timeout is 30 s), no orphan FFmpeg; `--resume` completes;
- limits `cpus: 1` and `mem_limit: 1g` on the `app` service (in the test compose file): `runtime_profile.json` reports at most 1 core and 1 GiB;
- `dev`: has no `MEDIA_DIR` mounted, does not reach the Internet, reaches `http://ollama:11434` (with the test compose file, where Ollama is simulated), and the files it creates in `/src` belong to the host user;
- `docker compose config` is valid with `.env.example`; without `MEDIA_DIR` or `HOST_UID` it gives a clear error; with a nonexistent mounted folder it fails without creating it;
- `docker compose config` with the `COMPOSE_FILE` of `.env.example`: only `ollama` has a device, `/dev/dri` (no `/dev/kfd`), and `OLLAMA_IGPU_ENABLE=1` appears in its environment; without the override no service has devices and the variable does not appear; `OLLAMA_VULKAN` appears in `ollama`'s environment only if set in `.env`; with `compose.selinux.yaml` added, `label=disable` appears only on `app` and `dev`;
- with `compose.test.yaml` (Ollama and `ollama-pull` replaced by a simulated server, test models volume in place of the external one, no model downloaded; Compose merges volumes by destination path, so it is enough to redeclare `/root/.ollama`, and where a field must be replaced entirely, such as the `external` declaration, `!override` or `!reset` is used, which require Compose 2.24.4 or later; `docker compose config` with the test file confirms it): `docker compose run --rm app doctor --require-ai` exits with 0; the app reaches `http://ollama:11434` but not the Internet (an outbound connection attempt fails); no Ollama port is published on the host.

Manual user check on the first real start: the model is downloaded only once, and on the second start `ollama-pull` exits immediately. On both machines `ollama ps` reports GPU; on the server `bench vision`, with and without `OLLAMA_VULKAN=0`, shows how much is gained.

## Stack

In the image: Python 3.11+, FFmpeg/FFprobe, OpenCV (headless variant), PySceneDetect, Pillow (+ `pillow-heif` for HEIC), imagehash, NumPy, requests or httpx, pydantic or jsonschema, `tzdata`; pytest only in the `dev` image. `librosa` only from Milestone 3. Visual embeddings (Milestone 2) are not in the MVP stack and are added only after the benchmark described in section 4. On the host: only Docker Engine and Compose.

## Structure

Package `vacation_memories/` with independent modules: `media_scanner`, `metadata_extractor`, `scene_detector`, `frame_sampler`, `quality_analyzer`, `duplicate_detector`, `vision_analyzer`, `scene_ranker`, `edit_planner`, `video_renderer`, `cache_manager`, `runtime_profile`, `cli` and, from Milestone 2, `evaluate`. Starting with fewer files and splitting later is allowed. At the repository root: `Dockerfile`, `compose*.yaml`, `.env.example`, `docker/`, `scripts/`.

## Pipeline

### 1. Scanning and metadata
- Recursive reading. Formats: JPG, PNG, HEIC (if `pillow-heif` is available), MP4, MOV, MKV.
- Dimensions, duration, FPS, orientation (EXIF/rotation), capture date (EXIF or container tag) with a documented fallback to `mtime`.
- Corrupt or unreadable files: log and skip, never crash.

### 2. Scenes and sampling
- PySceneDetect for obvious cuts; temporal sampling at intervals for continuous videos.
- Candidate segments with precise timestamps. Do not decode the same content more than once.

### 3. Technical quality (cheap, on all candidates)
- Sharpness (variance of the Laplacian, normalized to the resolution), exposure, contrast, black frames, motion/instability if measurable at low cost.
- Normalize the scores so as not to systematically penalize night scenes.
- From these measures `technical_score` (0-1) is derived, which describes only how well the shot turned out technically and is kept separate from the narrative value (see section 6).

### 4. Duplicates and repetitions
Two levels, with different purpose and cost. The perceptual hash finds nearly identical photos, but does not recognize semantically similar scenes (the same beach from different angles, ten dishes from the same lunch, similar panoramas): level 2 is needed for those.

**Level 1 — near-duplicates with `imagehash` (MVP, cheap):**
- Perceptual hash (pHash, with dHash as an optional check) of every photo and of one or more representative frames for every video segment. Comparison with Hamming distance on all pairs (vectorized NumPy), configurable threshold, grouping by connected components.
- The best representative of the group is kept (by `technical_score`, ties broken by `narrative_score`). The other members stay in the group with the reason recorded in the plan. Grouping happens only in the plan: no file is touched.
- Known limits, to be documented in the README: it does not recognize the same scene with a different framing, zoom or moment, nor a photo and a video frame of the same scene.
- `duplicate_detector` exposes an interface with a replaceable backend (returns groups and pairwise similarities). In the MVP only the `hash` backend exists, so level 2 can be added without modifying the callers.

**Level 2 — semantic similarity with visual embeddings (Milestone 2, only after a benchmark):**
- It is not part of the MVP or of the stack until the benchmark is passed.
- **Benchmark on the machine in use** (`bench embeddings` subcommand, launched by the user on the server, see "Privacy and development data"): for the small variants of CLIP or SigLIP (e.g. ViT-B/32), with ONNX Runtime and, if possible, PyTorch, in a variant of the app image built only for the benchmark (a dedicated target and a service with its own profile and the same volumes and networks as `app`, e.g. `docker compose run --rm app-embeddings bench embeddings`; these dependencies enter `runtime` only if the criteria are met), on CPU because the GPU is assigned to Ollama, measure load time, images per second, peak RAM and weight of the dependencies, on the machine in use and with Ollama active.
- **Adoption criteria:**
  1. cost per image at least one order of magnitude lower than that of the vision model measured with `bench vision`;
  2. RAM compatible with Ollama: load the embedder, compute the vectors and free it before the AI phase;
  3. improvement verified by the user with the Milestone 2 evaluation system: comparing the plans produced with the hash alone and with the embeddings, the share of clips flagged as repetitive decreases without increasing the important moments excluded.

  If a criterion is not met, stay on level 1 and document the reason.
- **Use of similarity:** two configurable thresholds.
  - Above the high threshold: same repetition group, with the best representative (and a minimum share so as not to empty a narrative section).
  - Between the medium and high threshold: no elimination, but a redundancy penalty in the ranking (section 6, for example with MMR) proportional to the similarity with the clips already chosen. The penalty weighs more between candidates close in time and less between different days: the same beach on two distinct days can be a different moment.
  - Embeddings also feed the variety component of `narrative_score`.
- **Cache and privacy:** vectors cached per (file, timestamp, model, input resolution). No automatic download at runtime: the weights are downloaded with an explicit step, documented in the README and authorized by the user, and then used offline from `<workdir>/models/`. The download happens on the host (e.g. `curl` with checksum verification, into the `models/` folder of `WORK_DIR`), because the `app` container has no Internet access. If the weights are missing, automatic fallback to level 1 with a warning in the log.
- **Tests:** with a fake, deterministic embedder (no real weights in the tests) on synthetic images: same scene with variations versus different scenes; check of thresholds, penalties and fallback.

### 5. AI analysis (optional, survivors only)
The pipeline must also work **without AI** (`--no-ai`), using only quality, duplicates, motion and temporal distribution.

When the AI is active, send to the model **only the candidates that passed filters 3 and 4**, limited by a budget computed at runtime (see "Runtime profile") and in any case to about 2-3 times the number of clips needed for the edit.

- Model configurable with `--model`, with the `VACATION_MEMORIES_MODEL` variable or with the configuration, in that order of precedence. The compose file sets the variable from `OLLAMA_MODEL`, so it takes precedence over any model in the configuration file and always matches the one downloaded by `ollama-pull`. Default: `qwen3-vl:4b-instruct-q4_K_M` (**instruct non-thinking** variant, 4-bit, about 3.3 GB; tag verified on the model page at ollama.com). Warning: the `qwen3-vl:4b` tag is the *thinking* variant and `qwen3-vl:latest` is the 8B, so neither must be used as default. The installed tag is checked with `ollama list`. If the model is not present, a clear error message and fallback to `--no-ai`, except with `--require-ai` (code 4).
- `/api/chat` endpoint with the image in base64, `stream: false`, **structured output through the `format` parameter with a JSON schema**, `temperature: 0`, a contained `num_predict`, a long `keep_alive` for the duration of the job. At the end of the job the app asks Ollama to unload the model (`keep_alive: 0`), so the RAM is freed (configurable).
- Image resized to **512 px** on the long side (configurable parameter, maximum 768).
- Timeout and at most 1 retry. An AI error on a frame does not block the pipeline: the frame keeps only the non-semantic scores, like the frames excluded by the AI budget.
- Prompt to the model in English (more reliable with small models).
- Required fields, only these:
  - `description`: at most 15 words;
  - `category`: **subject or environment**, enum (`nature`, `sea`, `city`, `monument`, `food`, `animals`, `indoor`, `other`). It serves to avoid thematic pile-ups (e.g. twenty food photos);
  - `moment_type`: **narrative role** of the scene, enum (`scenery`, `social`, `action`, `detail`, `transition`, `other`; `other` is the escape hatch to avoid forced classifications);
  - `narrative_value`: integer from 1 to 5, how much the scene deserves a place in a memory video (originality, ability to tell a moment);
  - `people`: boolean.
- Definitions of `moment_type` to put in the prompt:
  - `scenery`: wide view or environment shot with no main subject (panorama, skyline, beach);
  - `social`: people together, posing or interacting (meals, gatherings, groups);
  - `action`: clearly visible movement or activity (swimming, sports, walking, vehicle in motion);
  - `detail`: close-up of an object, food, sign, texture;
  - `transition`: linking material (road, window, travel, waiting, arrivals and departures).
- Watch the names: the `transition` value of `moment_type` (linking scene) has nothing to do with the plan's `transition_in` (hard cut or dissolve, section 8).
- In the prompt say explicitly **not to judge sharpness, exposure or composition** and to judge only the content: technical quality is measured elsewhere and the two assessments must not be mixed.
- If a field is missing or invalid (even `moment_type` outside the enum), use `null` for that field without discarding the frame.
- Do not infer identities, personal emotions or specific places that cannot be observed with sufficient reliability.
- Persistent cache per (file, timestamp, model, prompt, resolution).

### 6. Selection
The score separates **two dimensions**, computed and saved separately:

- **`technical_score` (0-1):** sharpness, exposure, contrast, stability (section 3). Measures how well the shot turned out, regardless of what it shows. Always computed.
- **`narrative_score` (0-1):** measures how worthwhile it is to tell that moment. Components: the AI's `narrative_value`, uniqueness, contribution to variety (`moment_type`, `category`), motion, temporal distribution. Without AI only the non-semantic components are used (uniqueness, motion, temporal distribution) and the report says so.

Combination rules:

1. **Minimum technical threshold:** below the threshold (blurry, black, badly wrong exposure) the candidate is discarded, whatever its narrative value. The threshold uses the normalization of section 3, so night scenes are not discarded just for being dark.
2. **Above the threshold, narrative prevails:** final score as a weighted sum, with greater weight on the narrative (initial values: 0.65 narrative and 0.35 technical, to be tuned). A unique, slightly imperfect moment can beat a perfect but banal shot.
3. **Redundancy penalty** on candidates similar to those already chosen. In the MVP the similarity comes from the `hash` backend of section 4; with the semantic level (Milestone 2) the penalty becomes proportional to the similarity and to the temporal neighborhood.

Weights, thresholds and shares live in a single configuration that can be edited, not scattered in the code.

Use of `moment_type` in the edit (initial values to be tuned on the first real dataset). The rules apply to clips with a known `moment_type`, so with partial AI coverage only to the analyzed ones; without AI use the fallbacks described in "Adapting to the available material":

- `scenery`: opens every place or day (environment shot), with a slightly longer duration.
- `social` and `action`: core of the story.
- `detail`: short clips (2-3 s) used as a rhythm break.
- `transition`: allowed only as a link between places or days, with a cap on total time (about 10%), and discarded if narratively weak.
- Rhythm variety: at most 3 consecutive clips with the same `moment_type`, and configurable soft shares for each type.

General selection rules:

- Spread the choices across the vacation; no single video must monopolize the result (duration cap per source file).
- Order: best-of Opening (deliberately out of chronological order), then Beginning, Development, Finale and Closing (see "Narrative structure"). Within Beginning, Development and Finale the order is chronological.
- Indicative durations: photo 3-4 s, video clip 3-8 s (reduced for short targets, see "Video duration"). The selection fills the budget of each narrative section (see "Narrative structure"), in score order and respecting variety and temporal distribution. The sum of durations, minus the overlaps of the dissolves, must respect `--duration`.

#### Narrative structure

The edit follows a **soft** narrative structure in five sections. The percentages are starting targets, configurable, and adapt to the available material. With `--duration 300` the initial budgets are 30 s, 60 s, 135 s, 60 s and 15 s.

| Section | Share | Allowed range |
|---|---|---|
| Opening | 10% | 5-15% |
| Beginning | 20% | 15-25% |
| Development | 45% | 35-55% |
| Finale | 20% | 15-25% |
| Closing | 5% | 3-8% |

Role and rhythm of each section:

- **Opening (teaser):** the moments with the highest `narrative_score` of the whole vacation, even out of chronological order, with variety of `moment_type` and places. Quick clips (2-3 s), high dynamism. The clips used here are **not** reused in the following sections. No `transition`.
- **Beginning:** first chronological stretch (departure, arrival, first impressions). `scenery` as environment shots, calm or medium rhythm. `transition` allowed; this is the section where it is most natural.
- **Development:** central body, chronological, maximum variety. Alternation of `social`, `action` and `detail`; every new place or day opens with `scenery`. Wave-like rhythm (fast sequences alternating with slower moments), standard durations.
- **Finale:** last chronological stretch, rhythm building up. More dynamic clips of high narrative value, with progressively shorter durations toward the climax.
- **Closing:** calm, concluding scene, slow rhythm. It is chosen by criteria (not by chronological order) among the serene clips with a high `technical_score` (`scenery` or `social`, low dynamism), preferring those from the last stretch of the vacation. Longer clips (video 5-8 s, photo 4-5 s) and a longer final dissolve, in sync with the music's fade-out. No `transition`.

Assignment of content to sections:

1. After the quality and duplicate filters, the candidates are sorted chronologically.
2. Opening and Closing are chosen by the criteria above.
3. The remaining candidates are divided into three chronological bands (Beginning, Development, Finale). The boundaries are computed on the **quantiles of the cumulative useful material**, in proportion to the budgets of the three sections, and not on calendar time: so a vacation with a lot of material concentrated on a single day does not leave empty sections.

Adapting to the available material:

- The effective budget of a section is its share of `--duration`, within the allowed range. If a section does not have enough valid candidates (above the technical threshold and not duplicates), the unused budget is redistributed to the other sections in proportion to their shares, even beyond the range if necessary, **but never by filling with discarded scenes or repeating clips**. If the material is still insufficient, the "Insufficient material" rule of the duration section applies.
- A section with a budget lower than the minimum duration of one of its clips is omitted or merged with the adjacent section: the threshold is 2 s for Opening, Beginning, Development and Finale. The Closing has longer clips (at least 4 s, or 3 s for targets under 60 s and in the compressed structure) but is not omitted: if its share would give less, it still receives one clip and the extra budget is taken from Development.
- Compressed structure: for `--duration` under 60 s, or with very scarce material (roughly fewer than 15 useful clips), a single chronological body is used (Beginning, Development and Finale together) with an Opening of at most 5 s and a Closing of a single clip of at least 3 s, even if the percentage would give less.
- Without AI the structure works anyway, but the criteria that require `moment_type` (e.g. `scenery` at the start of a section, calm scenes for the Closing) fall back on motion and non-semantic scores, and the report says so.

Configuration and CLI:

- Shares and ranges live in the single configuration. `--structure chronological` disables the sections and produces a purely chronological order.
- Changing structure, shares or `--duration` with a valid cache does not rerun technical analysis or AI: it only redoes selection and plan.
- `--dry-run` prints for each section planned duration, number of clips and whether the budget was redistributed.
- Phased implementation: **basic version** in Step 6 of Milestone 1 (budget per section, best-of Opening, calm Closing, chronological body, redistribution, compressed structure); **refinements** in Milestone 2 (section-specific rhythm and Finale crescendo, band boundaries anchored to day or place changes).

### 7. Edit plan
`edit_plan.json` with, for each element: source file (path relative to `--input`), type (photo/video), start/end, duration, final score with `technical_score` and `narrative_score` (and their components), `moment_type`, `category`, `section` (narrative section), description, selection reason, `transition_in` (`cut` or `dissolve` with duration, see section 8), destination timestamp. It must be hand-editable and re-runnable with `--render-only` without repeating the analysis.

At plan level: `schema_version`, `requested_duration`, `planned_duration`, `output` (`width`, `height`, `fps`), time zone assumed for dates and the `runtime` block (see "Runtime profile"). A plan with an unsupported `schema_version` is rejected with a clear error (code 2).

### 8. Rendering
- FFmpeg via `subprocess` with an argument list, **never `shell=True`**; paths with spaces and special characters handled correctly.
- Uniform resolution and fps according to `--resolution`/`--aspect`; sources with proportions different from the output (e.g. vertical videos in a 16:9 edit, or the reverse) are placed on a blurred background, without distorting or cropping by default.
- Photos into clips with a slight Ken Burns (`zoompan`), EXIF orientation respected.
- H.264 + AAC encoding. Default encoder `libx264` (software, more predictable); hardware acceleration is optional and chosen with `--hwaccel` (see "Runtime profile"). Dissolves and cuts follow the rules in "Transitions" below.
- Intermediate files in the working folder. The normalized segments (`<workdir>/segments/`) remain as a cache for later renders and can be deleted by hand to free space, because they are regenerated; the other temporary files are cleaned up at the end of rendering. On error, what is needed to resume is kept.

#### Two-phase rendering and normalization

The renderer does not assemble the sources directly: it first normalizes each clip into an intermediate segment, then assembles the segments.

**Phase A — normalization.** One file per clip in `<workdir>/segments/`, reusable between different renders if source and parameters do not change.

- **Frame-accurate cut**, with re-encoding (never `-c copy`). Take into account a nonzero `start_time` and container edit lists: the plan's start/end are relative to the stream as FFprobe exposes it.
- **Timestamps:** reset to zero at the start of each segment (`setpts=PTS-STARTPTS`; with audio also `asetpts=PTS-STARTPTS` and `aresample=async=1:first_pts=0`).
- **Variable frame rate (VFR):** FFprobe detects VFR videos (typical of smartphones) by comparing `r_frame_rate` and `avg_frame_rate` and, in doubtful cases, the packet timestamps. The `is_vfr` flag goes into the cached metadata. All segments are converted to a **constant** frame rate equal to `output.fps` with the `fps` filter and explicit CFR mode (`-fps_mode cfr`, or `-vsync cfr` on older versions: read the FFmpeg version at startup). No frame blending by default. Videos with a frame rate higher than `output.fps` (60, 120 or 240 fps) are downsampled, not slowed down.
- **Orientation:** the rotation indicated by the metadata (rotation tag or display matrix) is applied to the pixels and then removed from the segment's metadata. Photos (HEIC included) are decoded with Pillow (`pillow-heif` for HEIC), EXIF orientation is applied and an already oriented image is passed to FFmpeg, written as a temporary file in `<workdir>/tmp`: so there is no dependence on FFmpeg's HEIC support or its orientation handling. No dependence on the player's automatic rotation.
- **Uniform format:** same output resolution (with a blurred background for different proportions), `setsar=1`, `yuv420p`, same `time_base`. The `xfade` filter requires inputs with identical resolution, pixel format, frame rate and time base, so every deviation must be corrected here and not in assembly. HDR or 10-bit clips (e.g. iPhone HLG) must be converted to SDR BT.709; if the correct conversion is not available, a warning in the log.
- **Audio:** in the MVP the movie has only the music, so the segments have no audio. When the original audio is kept (Milestone 3), each segment will have an audio stream with uniform parameters (same sample rate and channels, silence generated for clips without audio) and a duration equal to that of the video within 1 frame.
- Each segment is validated with FFprobe before assembly (see "FFprobe verification").

**Phase B — assembly.** Concatenation of the segments according to the rules in "Transitions", then music with fades. Do not build a single filtergraph with hundreds of inputs: for long timelines assembly is done in blocks of limited size (roughly 15-20 clips, to be fixed with tests on RAM and time), with the boundaries between blocks on hard cuts chosen by the planner, and the blocks are then concatenated.

#### Transitions (dissolves and cuts)

- **No dissolve is mandatory.** Every passage between two clips is a `cut` (hard cut) or a `dissolve`, decided by the planner and saved in the plan as the incoming element's `transition_in` (`type`, `duration`). The hard cut is a perfectly valid choice.
- **Choice criteria** (soft, configurable):
  - prefer `dissolve` for changes of place or day, passages between narrative sections, consecutive calm photos and the Closing;
  - prefer `cut` between consecutive `action` clips, in the Opening (fast rhythm), between clips of the same place and moment, for `detail` clips and for short clips;
  - avoid a mechanical rhythm: at most 3 consecutive transitions of the same type, except the Opening, which stays mostly on hard cuts;
  - the fade from black at the start of the video (at most 1 s) and to black at the end of the video (longer, in sync with the music's fade-out) are optional and do not count as transitions between clips.
- **Duration adapted to the shortest clips.** The requested duration `d` has a default of 0.8 s, configurable (shorter in the Opening, longer in the Closing). The effective duration is computed as follows:
  1. `d_eff = min(d, 0.25 × t_min)`, where `t_min` is the duration of the shorter of the two clips involved. This way, for every clip, the incoming and outgoing dissolves added together do not exceed 50% of its duration, and at least half the clip remains without overlaps;
  2. `d_eff` is rounded down to a multiple of the duration of one frame (1 / `output.fps`);
  3. if `d_eff` is less than 0.2 s (configurable threshold), the dissolve becomes a `cut`.

  The same rules apply to photos with Ken Burns.
- **Duration accounting.** Every dissolve overlaps two clips and shortens the total by `d_eff`: timeline duration = sum of clip durations − sum of effective dissolves. Dissolve offsets and destination timestamps in the plan are computed by **a single function shared between planner and renderer**, with frame rounding, to avoid drift between plan and video.
- **Limits always applied.** The renderer applies these rules also to transitions written by hand in the plan (`--render-only`): if they exceed the limits it reduces them or turns them into `cut`, with a warning in the log and the effective value recorded in the report.
- Default durations, maximum percentage (25%), minimum threshold (0.2 s) and tolerances live in the single configuration.

#### FFprobe verification

**On every normalized segment**, before assembly:
- expected resolution, SAR 1:1, pixel format `yuv420p`;
- constant frame rate equal to `output.fps` (`r_frame_rate` and `avg_frame_rate` coincide), uniform `time_base`;
- no residual rotation tag;
- duration equal to the expected one within 1 frame; with audio (Milestone 3), audio and video duration within 1 frame.

A non-conforming segment is regenerated once with more conservative parameters. If it remains non-conforming, the clip is skipped with a warning, the adjacent transitions are recalculated and the total duration updated: a single defective clip does not interrupt rendering.

**On the final file:**
- actual duration equal to `planned_duration` within 0.2 s (configurable threshold; the ±5% tolerance on the requested duration stays the one in the duration section);
- H.264 `yuv420p` video with resolution and constant frame rate as per `output`; AAC audio with the expected sample rate and channels;
- sync: `start_time` of video and audio within 0.05 s, difference between audio and video duration within 0.1 s (the music with its fade-out must end together with the video);
- video packet timestamps monotonic, with no negative values or jumps above 2 frames (cheap check, does not decode the file);
- if a check fails the file stays as `<name>.partial.mp4` (it is not renamed to the final name), the program exits with code 3 and a clear message, without presenting it as successful.

At the end of rendering `<workdir>/render_report.json` records planned and obtained duration, VFR clips converted, clips regenerated or skipped, transitions reduced or turned into `cut` and the outcome of every check.

### 9. Music
- `--music` is optional: without music the file still has a silent AAC track, so players and FFprobe checks behave the same.
- Milestone 1: background adapted to the video's duration, with fade-in and fade-out. Longer track: cut with a final fade-out. Shorter track: loop with crossfade and a warning in the log.
- Milestone 3: `librosa` for beats and sections, cut alignment, duration adaptation, occasional original audio. No frequent cuts in quiet moments.

### 10. Cache
Metadata, scenes, hashes, AI results and scores are in an `sqlite3` database in `<workdir>` (WAL mode, a single writer process, workdir on a local filesystem: on NFS/SMB WAL is not reliable and the README says so). The normalized segments are files in `<workdir>/segments/`. The key of a source file is its path **relative to `--input`** with size and mtime: the cache stays valid when moving the media to another PC or into a container, as long as mtime is preserved (`rsync -a`, `cp -p`). Invalidate when the file, the model or the relevant parameters change.

## CLI

```
docker compose run --rm app \
  --music /music/track.mp3 \
  --duration 300 \
  --aspect 16:9 \
  --resolution 1080p \
  --output memories.mp4
```

`--input` and `--workdir` come from the compose configuration. In the `dev` container that configuration is absent: the same CLI is invoked as `python -m vacation_memories --input dev-media ...`, with the repository's default workdir `./work` (ignored by git). Since in `dev` `OLLAMA_HOST` points to the `ollama` service, `--allow-remote-ollama` is needed (with Ollama started) or `--no-ai`.

Options: `--input`, `--music`, `--output`, `--duration`, `--aspect`, `--resolution` (see below), `--analyze-only`, `--preview` (low resolution, see "Resolution and format"), `--render-only`, `--resume`, `--dry-run`, `--model`, `--no-ai`, `--workdir`, `--structure narrative|chronological` (default `narrative`, see "Narrative structure" in section 6). Execution options (see "Runtime profile"): `--jobs`, `--hwaccel`, `--ai-frames`, `--ai-time-budget`, `--ollama-url`. For non-interactive use (see "Headless execution"): `--log-file`, `--log-format text|json`, `--quiet`, `--config`, `--require-ai`, `--allow-remote-ollama`, `--nice`, `--assume-tz`; subcommands `doctor`, `bench` and `evaluate` (Milestone 2).

### Execution modes and subcommands

- **No mode:** full pipeline (analysis, plan, rendering).
- **`--analyze-only`:** scanning and analysis (technical, duplicates, AI) saved in the cache; no plan or rendering.
- **`--dry-run`:** analysis (reusing the cache) and plan. Prints the profile, the plan per section, planned duration and time and space estimates, without rendering. With the analysis already in the cache it is immediate.
- **`--render-only`:** rendering from an existing `edit_plan.json`, even a hand-edited one; no analysis.
- The three modes are mutually exclusive (code 2 if combined).
- **`--resume`:** resumes an interrupted run in the same workdir, with the parameters saved in `<workdir>/run_config.json`. The original command line can be rerun with `--resume` added: the parameters that change the result must match the saved ones (otherwise code 2), while performance and log options may change. Without `--resume` it restarts from the first phase, but the cache avoids redoing the work already done.
- **Subcommands:** if the first argument is `doctor`, `bench` or `evaluate` (Milestone 2) the subcommand is run, otherwise the pipeline. This way everything is passed as arguments to the `app` container, whose entrypoint is `python -m vacation_memories`.

### Video duration (`--duration`)

The user chooses the final duration of the video. If the option is omitted, the default is 300 seconds.

- **Accepted formats:** seconds (`300`), `90s`, `5m`, `1m30s`, `mm:ss` (`05:00`), `h:mm:ss` (`1:05:00`). Any other format gives a clear error with valid examples.
- **Limits:** minimum 15 s. Above 20 minutes a warning is printed (very long analysis and rendering times) but it does not block.
- **Precision:** the duration computed in the plan must take the overlap of the dissolves into account. The real duration of the final file, measured with FFprobe, must be within ±5% of the requested value. At the end of rendering the requested and obtained durations are printed.
- **Insufficient material:** if after the quality and duplicate filters there is not enough valid material, **do not fill with discarded scenes or repeat clips**. Produce a shorter video and warn with the actual duration and the reason.
- **Duration scale:** the number and length of clips depend on the target. For short videos (under 60 s) shorter clips (photos 2-3 s, videos 2-4 s); for long ones the standard durations of the Selection section are used.
- **Interactions:**
  - `--dry-run` prints the plan with the planned duration, without rendering.
  - `--render-only` uses the plan's duration, possibly hand-edited; if `--duration` is also passed and differs, warn that it is ignored.
  - `edit_plan.json` saves `requested_duration` and `planned_duration`.
  - Changing `--duration` with a valid cache must **not** rerun technical analysis or AI, only selection and plan.

### Resolution and format (`--resolution`, `--aspect`)

The user chooses resolution and proportions of the final video. Default: `1080p` and `16:9`.

- **`--aspect`:** `16:9`, `9:16`, `1:1`, `4:3`.
- **`--resolution`:** presets `480p`, `720p`, `1080p`, `1440p`, `4k` (= `2160p`), or explicit dimensions `WxH` (e.g. `1920x1080`).
- **Meaning of the preset:** it indicates the **short side** of the frame, while `--aspect` determines the other side. Examples with `1080p`: `16:9` → 1920×1080, `9:16` → 1080×1920, `1:1` → 1080×1080, `4:3` → 1440×1080. With `4k` and `16:9` → 3840×2160.
- **Explicit dimensions:** the ratio is deduced from `WxH`. If an inconsistent `--aspect` is also passed, a clear error.
- **Validation:** dimensions always rounded to even values (required by H.264 with `yuv420p`). Minimum short side 320 px, maximum 2160 px. Invalid values: error with examples of accepted formats.
- **Rendering cost:** above 1080p warn that with the software encoder rendering will be much longer, with an estimate based on the time measured on a test clip, without blocking. Suggest `--preview` for trials.
- **Sources smaller than the target:** they are scaled up. If more than 30% of the selected clips have a resolution lower than the output, warn in the log.
- **`--preview`:** forces a short side of 540 px and 24 fps with a fast x264 preset, ignoring `--resolution`. Writes a file with the `_preview` suffix, without overwriting the final output.
- **Interactions:**
  - Quality, duplicates and AI analysis (512 px images) do not depend on the output resolution. Changing `--resolution` or `--aspect` does not rerun analysis or selection: it only redoes rendering.
  - `edit_plan.json` saves `output: {width, height, fps}` (default fps 30).
  - With `--render-only`, if `--resolution` and/or `--aspect` are passed, they replace those saved in the plan.
  - For photos with the Ken Burns effect, internally use a scale higher than the output (e.g. 2×, with a cap) before `zoompan`, to avoid judder.

## Development plan

### Sections to read for each step

The sections "How to work", "Privacy and development data" and "Constraints" apply to all steps. For each one, in addition:

| Step | Sections |
|---|---|
| 1 | Docker image and Docker Compose (including "Integrated Intel GPU for Ollama") |
| 2 | Runtime profile; Headless execution; pipeline 1 and 10; Stack; Structure |
| 3 | Pipeline 2, 3 and 4 |
| 4 | Pipeline 7, 8 and 9; Resolution and format; Headless execution ("Signals, SSH sessions and resumes") |
| 5 | Pipeline 5; Runtime profile ("Detection" and "Calibration"); Headless execution ("Ollama on the server"); Docker image and Docker Compose (`dev` service, for the `-m ollama` tests) |
| 6 | Pipeline 6, with the AI fields of pipeline 5; CLI (with Execution modes, Video duration and Resolution and format); Headless execution (including the tests) |
| 7 | Docker image and Docker Compose (including "Docker tests") |

### Step 0 — by the user (not for Claude Code)
On both machines, with Docker: a temporary Ollama container with GPU access (`/dev/dri` and `OLLAMA_IGPU_ENABLE=1`), with the same `ollama/ollama` tag the compose file will use (the user communicates it in Step 1), and the models volume the compose file will use (see SETUP.md). The model is downloaded (`ollama pull qwen3-vl:4b-instruct-q4_K_M`) and `ollama ps` is used to check that it runs on the GPU. On the server, which decides the AI budget, manual benchmark on 5-10 images with `ollama run`: time per frame, quality of the responses, sufficient resolution, and whether `moment_type` and `narrative_value` turn out sensible and consistent (small models may confuse `detail`, `transition` and `scenery`). The benchmark must be done twice, with the GPU and with `OLLAMA_VULKAN=0`, to measure the gain. On the development PC it is enough to check that the GPU works: there the model is only used for the integration tests.

### Milestone 1 — MVP (one step at a time)
1. **Development Docker environment.** `Dockerfile` (essential `dev` and `runtime` targets), `.dockerignore`, `compose.yaml` with `dev`, `app` (already with the user, volumes and hardening described in "Configuration" and in "Common rules for container execution"), `ollama` (with the `ollama/ollama` tag given by the user, the same as Step 0) and `ollama-pull`, `compose.intel-gpu.yaml`, `compose.selinux.yaml`, `docker/compose.toml`, `.env.example` (with `MEDIA_DIR=./dev-media` and GPU active), initial `requirements.txt` and `requirements-dev.txt`, a minimal `vacation_memories` package (`__main__` that prints the version, so the `runtime` image already builds), `.gitignore` (`.env`, `work/`, `dev-media/`, `music/`, Python and pytest caches). It also creates the development PC's `.env` by copying `.env.example`, with the user's `HOST_UID` and `HOST_GID` (`id -u`, `id -g`) and `MEDIA_DIR=./dev-media` unchanged, and the empty folders `dev-media/`, `music/` and `work/`, which the compose file does not create by itself. Checks: `docker compose --profile dev build` succeeds; `docker compose run --rm dev pytest` runs a minimal test; `docker compose run --rm dev ffmpeg -encoders` shows `libx264`; `docker compose config` is valid with `.env.example` and, with the GPU override, only `ollama` has `/dev/dri` and `OLLAMA_IGPU_ENABLE=1`. When the step is finished the user (not Claude Code) loads the model and checks with `ollama ps` that Ollama in the compose file uses the GPU (see "Integrated Intel GPU for Ollama").
2. **Skeleton and scanning.** Package structure, dependencies (`requirements.txt` for the runtime, `requirements-dev.txt` for tests and development), `media_scanner`, `metadata_extractor`, basic `cache_manager`, `runtime_profile` (detection and override, see "Runtime profile"), `doctor` subcommand, non-interactive logging and configuration options (`--log-file`, `--log-format`, `--quiet`, `--config`, `--nice`, `--assume-tz`, with the precedences of "Headless execution"), workdir lock, atomic writes and `TMPDIR` in the workdir (see "Headless execution"), synthetic media generator for the tests, which can also populate `./dev-media` for trials with the `app` service (parametric, without system fonts, with scenes identifiable by pattern and overlaid text, so it can be reused in Milestone 2 for the dataset with known truth), tests.
3. **Technical analysis.** Scenes/sampling, quality, duplicates. Tests with blurred, black and nearly identical images generated artificially (brightness variations, JPEG recompression, slight resizing), plus different images that must **not** be grouped (false-positive check), and a check that the chosen representative is the best of the group. `duplicate_detector` must expose the replaceable backend described in section 4.
4. **Plan and rendering.** `edit_plan.json` (validated schema) and a renderer that works from a hand-written plan, music with fades, `--jobs`, encoder choice (`--hwaccel auto` may slip to later, and in the compose file it falls back to software anyway) and `--preview`. End-to-end tests on synthetic media with MP4 verification via FFprobe, including the final dimensions for at least three combinations (e.g. 720p 16:9, 1080p 9:16, 1080p 1:1) with horizontal and vertical sources. Tests of robust rendering (section 8) on synthetic media: dissolve between two clips of uniform color, checking that the middle frame is a mix of the two colors; very short clip (e.g. 1.2 s) with the dissolve reduced to 25%; dissolve below threshold turned into `cut`; hand-written transition beyond the limits reduced with a warning; synthetic VFR clip (frames at irregular intervals) converted to constant frame rate; clip with rotation metadata and vertical clip that arrive with correct orientation and dimensions; timeline duration equal to the sum of the clips minus the sum of the dissolves; FFprobe checks on the final file (duration, sync, timestamps); a deliberately defective segment is detected and handled according to the rule. For server use: FFmpeg subprocesses with `-nostdin` and `stdin=DEVNULL`, orderly shutdown on SIGINT/SIGTERM/SIGHUP with termination of children, output written as `.partial.mp4` and renamed after the checks (see "Headless execution"). Without `--music` the file has a silent audio track and passes the checks.
5. **AI analysis.** `vision_analyzer` with schema, retry, cache and fallback, `--ollama-url` and `--allow-remote-ollama`, runtime calibration and frame or time budget (`--ai-frames`, `--ai-time-budget`); recording where the model runs (GPU, CPU or mixed) after the warm-up; `bench vision` subcommand, which measures seconds per frame (by default on synthetic images, or on a folder given by the user with `--images`, which in the compose file is under `/media`). Tests with a simulated HTTP server, no dependence on a real Ollama in the tests, including schema validation (values of `moment_type` and `narrative_value` out of range → `null`, frame kept). Optional `-m ollama` integration tests in the `dev` container, with the real model and synthetic images only: the response respects the schema and where the model runs is recorded.
6. **Selection and full CLI.** `scene_ranker`, `edit_planner`, end-to-end CLI with and without `--no-ai`, parsing and validation of `--duration` and `--resolution`/`--aspect` (tests on all accepted formats, presets and `WxH`, inconsistent combinations, rounding to even values, invalid values and limits), tests that the final video's duration falls within ±5% on several targets (e.g. 30 s, 90 s, 5 min) with synthetic media, test of the "insufficient material" case. Ranking tests on synthetic candidates with known scores: a candidate below the technical threshold is always discarded; a technically good but banal candidate does not beat one above the threshold with high narrative value; no sequence exceeds 3 clips of the same `moment_type`; without AI the selection works and the report says so. Tests of the narrative structure on synthetic datasets with both uniform temporal distribution and very unbalanced (e.g. 80% of the material on a single day): budget per section within the allowed ranges and sum equal to `--duration` (±5%); Opening clips never repeated in the following sections; Beginning, Development and Finale in chronological order; budget redistribution when a section has insufficient material, without repeating clips; compressed structure for short targets; `--structure chronological` produces a purely chronological order. Tests of the modes and paths: `--analyze-only`, `--dry-run` and `--render-only` stop at the expected phase and combined give code 2; `--resume` with parameters different from the saved ones gives code 2; a relative `--output` ends up in the workdir and an `--output` inside `--input` gives code 2; a plan with an unsupported `schema_version` is rejected. **Headless tests** (synthetic media only):
  - full CLI cycle in a subprocess with `stdin=DEVNULL`, without a terminal, without `DISPLAY`, with `LANG=C`, `TZ=UTC` and an unusable `TMPDIR`: ends with code 0, without prompts or windows, and the temporary files are in the workdir;
  - SIGTERM during rendering: code 143, no orphan FFmpeg, no truncated JSON; `--resume` completes and the result passes the checks;
  - second instance on the same workdir: code 5; on a different workdir: it starts;
  - read-only `--input`: content and mtime of the `--input` tree unchanged;
  - non-UTF-8 file name handled without loss; the same EXIF dates with `TZ=UTC` and `TZ=Pacific/Auckland` give the same division into days;
  - non-local Ollama URL without `--allow-remote-ollama`: error; with the flag: warning (simulated Ollama server); `--require-ai` without Ollama: code 4;
  - `doctor` on simulated environments: FFmpeg without `libx264` → code 4;
  - the declared dependencies do not include non-headless `opencv-python`.

  README with installation and use with Docker Compose on the development PC and server, known limits, long jobs with `tmux` or `nohup` and copying of the results.
7. **Production and container tests.** Polishing of the `runtime` image (minimal content, label and version), documentation of transfer without a registry and of resource limits, verification of `app` hardening, `compose.test.yaml`, `scripts/test-docker.sh` with the container tests and the "Docker" section of the README, as described in "Docker image and Docker Compose". It requires the full CLI of Step 6.

### Milestone 2 — Quality
**Order of work:** first the evaluation system (next section), then the improvements below, measuring the effect of each change with it.

Advanced temporal segmentation, better ranking, handling of repetitive scenes, photos, chronological balancing, refinement of the narrative structure (rhythm per section and boundaries anchored to day or place changes). Semantic similarity for repetitive scenes with lightweight visual embeddings (CLIP/SigLIP): first the benchmark on the machine in use and the adoption criteria of section 4, then the implementation of the backend and the integration into the ranking. The MVP stays on `imagehash`.

### Perceived quality evaluation (part of Milestone 2)

Technical metrics do not say whether the video is really good. This system measures perceived quality and serves to decide whether each change to ranking, duplicates or structure improves the result. It is built **before** the other Milestone 2 work.

**Two levels of evaluation**

1. **Synthetic dataset with known truth (development, automatic).** The synthetic media generator from Step 2 is extended to a "synthetic vacation": multiple days and places, scenes recognizable by pattern and overlaid text, known groups of repetitive scenes, blurred or black clips and some moments marked as important (e.g. more motion, or a unique element). A `ground_truth.json` file describes everything. Automatic metrics:
   - important moments included (recall) and clips of the same repetitive group selected together;
   - coverage per day and place, maximum share of a single source;
   - adherence to section budgets and clip duration limits;
   - comparison with random selection at plan level, without rendering, over many seeds (e.g. 100), with mean and spread.

   It serves to find regressions and to verify the infrastructure. **It does not establish whether the video is pleasant and does not replace human evaluation.**
2. **Private evaluation on real material (by the user).** Run only by the user on their own folder, with the tools described below.

**The five questions**

| Question | How it is evaluated | Metrics |
|---|---|---|
| Which important moments were excluded? | Gallery of **excluded** candidates: the best by score plus a random sample for each exclusion reason (technical threshold, duplicate, budget, ranking). The user marks "important". | Number of important moments excluded, recall on the flagged moments, most frequent exclusion reason |
| How many scenes are repetitive? | In the list of selected clips the user marks "repetitive". | Share of flagged clips; distribution of the similarities computed (hash or embedding) for flagged and unflagged pairs, to tune the thresholds of section 4 |
| Is the edit balanced? | Overall 1-5 rating and, for each narrative section, rhythm "too slow / right / too fast". | Ratings, alongside automatic metrics: coverage per day, share per source and per `moment_type`, actual duration versus each section's budget |
| Are there clips that are too long or too short? | While watching, for each clip: "too long", "too short" or ok (default). | Share of flagged clips; distribution of durations by `moment_type` and section, with the flags highlighted |
| How satisfying is it compared to a random selection? | **Blind** comparison between variants (see below). | 1-5 rating per variant, ranking, number of times the full edit beats the random one |

**Blind comparison**
- The variants come from the same material, with the same `--duration` (for the evaluation default 90 s, configurable, to limit rendering times) and a low-resolution preview (`--preview`):
  - `full`: full pipeline;
  - `random`: clips chosen at random with the same duration and per-section budget constraints, in chronological order, fixed and recorded seed; excludes only unreadable files;
  - optional: `no-ai` (quality, duplicates and motion only: tells whether the vision model is worth its cost) and `technical-only` (the best by `technical_score`: tells how much the narrative part adds).
- Anonymous labels (A, B, C) in random order. The mapping is in a separate file, read only by `report` after the ratings.
- With few comparisons the outcome is indicative, not statistical, and the report says so.

**Tool**

`evaluate` subcommand (`docker compose run --rm app evaluate ...`), with three actions:
- `synthetic`: automatic evaluation on the synthetic dataset (also runnable in pytest);
- `prepare`: creates `<workdir>/eval/<run_id>/` with the variants' previews, the thumbnails and `review.html`;
- `report`: reads the `ratings.json` exported from the page and produces `eval_report.md` (readable) and `eval_summary.json` (anonymous).

`review.html` is a local static page, with no server or external resources: it works by opening the file. It contains the preview with the list of clips (thumbnail, start time, duration, section, `moment_type`; clicking the time takes the video to that point), the buttons for the flags, the gallery of excluded clips and the ratings. It saves in the browser while you work and exports `ratings.json` with a button. It is designed to be reused by the Milestone 4 gallery. On a headless server the `eval/<run_id>/` folder is self-contained (relative paths) and is copied to the computer from which it is viewed (rsync/scp); the program never opens a browser and only prints the path.

**Persistent labels and history**
- The flags are associated with a stable segment ID (source, start, end). If the segmentation changes, the association is made by temporal overlap above a threshold. In later cycles it is enough to review what is new, and the metrics on important moments included and on clips already flagged as repetitive are computed automatically on the new configuration.
- `eval_history.json` records for each cycle the configuration hash, code version, metrics and ratings, to compare cycles with each other.

**Procedure for every significant change:** regenerate the plan (analysis cached if unchanged) → `evaluate prepare` → viewing and ratings → `evaluate report` → comparison with the previous cycle and with `random`. No success threshold is fixed a priori: the first cycle, on the Milestone 1 result, sets the baseline.

**Privacy**
- All local: no network, no telemetry, no external resource in the page. Thumbnails and previews stay in `<workdir>/eval/`.
- `eval_summary.json` is anonymous: segment IDs, ratings, flags, durations, sections, scores, `moment_type`, relative day (day 1, 2, ...). No paths, file names, thumbnails or exact dates. It is the only file to share with Claude Code to tune weights and thresholds.

**Implementation steps**
- **E1 (Claude Code):** known truth on the synthetic dataset, `evaluate synthetic`, random baseline on the plans.
- **E2 (Claude Code):** `prepare`, `review.html`, `report`, anonymous summary, persistent labels and history.
- **E3 (user):** first cycle on real material, which sets the baseline.

**Tests** (synthetic data only):
- correct automatic metrics on known cases and reproducible random baseline with the same seed;
- anonymity of the summary: no path or file name of the synthetic media in the JSON;
- page generated with no external `http(s)` references;
- persistent labels still associated after a synthetic re-segmentation;
- blind variant mapping not present in the report before the ratings.

### Milestone 3 — Music-driven editing
Beat tracking, more refined transitions, original audio, duration adaptation, alignment of narrative section boundaries to music sections (energetic Opening, Closing on the outro).

### Milestone 4 — Review
Static HTML gallery (reuses `review.html`) to examine, exclude or confirm segments. A static page cannot write files: it exports the choices to a JSON that a subcommand applies to `edit_plan.json`, then it is rendered with `--render-only`.
