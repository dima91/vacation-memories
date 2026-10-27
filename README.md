# Vacation Memories

Creates a memory video (5 minutes by default) from the hundreds of photos and clips of a vacation. It discards
blurred, dark and repeated shots, picks the best moments, orders them into a story with an opening, a
chronological body and a calm closing, and adds background music. Everything runs locally, in Docker containers:
the originals are only read, never changed, and nothing leaves the machine.

## Requirements

- Linux with Docker Engine and the Docker Compose plugin. Nothing else is installed on the host: Python, FFmpeg
  and the libraries are in the images.
- Optional: an Intel GPU (`/dev/dri`) for the vision model. Without it the model runs on the CPU, more slowly.
  The program also works without any AI (`--no-ai`).

## Installation

The same steps apply to the development PC and to the server; only `.env` differs.

1. Copy the repository to the machine and create `.env` from the example:

   ```sh
   cp .env.example .env
   ```

   Then edit `.env`:

   - `HOST_UID` and `HOST_GID`: the output of `id -u` and `id -g`, so the files in the working folder belong to
     you;
   - `MEDIA_DIR`: the folder with the photos and clips (mounted read-only). On the development PC keep
     `./dev-media`, with synthetic media only;
   - `MUSIC_DIR`: the folder with the background music (mounted read-only);
   - `WORK_DIR`: the working folder (cache, plan, logs, video). It needs room for the segments: about twice the
     size of the final video;
   - `COMPOSE_FILE`: remove `:compose.intel-gpu.yaml` on machines without `/dev/dri`; add
     `:compose.selinux.yaml` on hosts with SELinux.

2. Create the folders and the volume of the models, which the compose file does not create by itself:

   ```sh
   mkdir -p dev-media music work      # or the folders named in .env
   docker volume create vacation-ollama-models
   ```

3. Build the image and check the installation:

   ```sh
   docker compose build app
   docker compose run --rm -T app doctor
   ```

   The first start of Ollama downloads the vision model (about 3.3 GB) through the `ollama-pull` service. On a
   server without Internet, transfer the images and the models volume from a machine that has them.

4. Optional: measure the speed of the vision model on this machine (`bench vision`, see "Troubleshooting").

On the development PC, `./dev-media` can be filled with a synthetic vacation:

```sh
docker compose --profile dev build
docker compose run --rm -T dev python -m vacation_memories.synthetic_media dev-media/vacation --vacation
```

## Usage

```sh
docker compose run --rm -T app --duration 5m --music /music/track.mp3 --output memories.mp4
```

`--input` (`/media`, that is `MEDIA_DIR`) and `--workdir` (`/work`, that is `WORK_DIR`) come from the compose
configuration (`docker/compose.toml`). A relative `--output` is written in the working folder, so the video above
ends up in `WORK_DIR/memories.mp4`. The music comes from `MUSIC_DIR`, mounted as `/music`. Use `-T` whenever there
is no terminal (scripts, `cron`, `nohup`).

The compose configuration sets `require_ai = true`: if Ollama or the model is missing the run stops with code 4
instead of producing a video without AI. To run without the vision model, pass `--no-ai`; `--no-deps` also avoids
starting Ollama:

```sh
docker compose run --rm --no-deps -T app --no-ai --duration 90s
```

### Main options

| Option | Meaning |
| --- | --- |
| `--duration` | length of the video: `300`, `90s`, `5m`, `1m30s`, `05:00`, `1:05:00` (default 5 minutes, minimum 15 s; above 20 minutes only a warning) |
| `--resolution` | `480p`, `720p`, `1080p` (default), `1440p`, `4k`, or `WxH` such as `1920x1080`. A preset is the short side of the frame |
| `--aspect` | `16:9` (default), `9:16`, `1:1`, `4:3`. With `WxH` the ratio comes from the dimensions |
| `--music` | background music; shorter tracks loop with a crossfade, longer ones fade out at the end |
| `--output` | the video file (`.mp4`); never inside the media folder |
| `--structure` | `narrative` (default: opening, beginning, development, finale, closing) or `chronological` |
| `--preview` | quick trial: short side 540 px, 24 fps, fast encoder; writes `<name>_preview.mp4` |
| `--no-ai`, `--require-ai` | without the vision model; or stop (code 4) if it is not available |
| `--model`, `--ai-frames`, `--ai-time-budget` | vision model, a fixed number of analyzed frames, or the seconds the AI may take |
| `--jobs`, `--hwaccel`, `--nice` | parallel FFmpeg processes, hardware encoder, lower priority |
| `--log-file`, `--log-format`, `--quiet`, `--config` | see "Configuration" |

Examples: `--resolution 1080p --aspect 9:16` gives 1080x1920; `--resolution 4k` gives 3840x2160;
`--resolution 1080x1350` keeps its own 4:5 ratio.

### Modes

| Mode | What it does |
| --- | --- |
| (none) | analysis, plan and rendering |
| `--analyze-only` | analysis only (technical, duplicates, AI), saved in the cache |
| `--dry-run` | analysis (from the cache when possible) and plan: prints the sections, the planned duration and rough time and space estimates, and saves `edit_plan.json`, without rendering |
| `--render-only` | renders the existing `edit_plan.json`, also after editing it by hand; `--resolution` and `--aspect` replace the saved ones, `--duration` is ignored |
| `--resume` | resumes the interrupted run of the working folder (see below) |

The three modes exclude each other. The analysis is cached in the working folder: a second run with another
duration, structure or format redoes only the selection, the plan and the rendering, so it is quick. A typical
session:

```sh
docker compose run --rm -T app --analyze-only          # long: decodes everything, asks the model
docker compose run --rm -T app --dry-run --duration 3m  # immediate: look at the plan
docker compose run --rm -T app --duration 3m --preview  # quick trial video
docker compose run --rm -T app --duration 3m --music /music/track.mp3
```

### Plan and report

`WORK_DIR/edit_plan.json` lists every clip with its section, scores (`technical_score`, `narrative_score` and the
final score), moment type, capture date and the reason it was chosen. Its `runtime` key says how the sections were
filled, whether a budget was redistributed, how much the AI covered, and why the video is shorter when the material
is not enough. `render_report.json` describes the rendering and the checks of the final file.

The Opening shows the best moments of the whole vacation, as quick clips in chronological order. A clip with several
good moments is not cut into many short pieces: it gives one piece to each section, the Opening included. The clip of
the Closing gives none to the section before it, where it would end one section and start again in the next. A further
good moment of a clip already in the section lengthens that shot, without a break, into the next part of the same clip,
up to 15 s (`video_shot_max_seconds`); in the Opening, and once the shot is full, the other moments are left out. A
second piece of the same clip comes only when the section would otherwise run short, and the report says so. In the
Closing this gives one calm, continuous shot. Such a shot says "continuous shot over N adjacent segments" in its reason, and
`runtime.extended_shots` counts them. Targets under 60 s and the `detail` and `transition` clips keep their ordinary
durations.

### Configuration

Every option can also come from a TOML file (`--config`, or the `VACATION_MEMORIES_CONFIG` variable) or from a
`VACATION_MEMORIES_<OPTION>` variable; the command line wins over the variables, which win over the file. The
`app` service reads `docker/compose.toml`. The weights, thresholds and shares of the selection are in the
`[selection]` table, for example:

```toml
duration = "4m"
music = "/music/track.mp3"

[selection]
technical_threshold = 0.3     # below it a shot is discarded as blurred, dark or badly exposed
narrative_weight = 0.7        # with technical_weight, adds up to 1
technical_weight = 0.3
video_shot_max_seconds = 12   # longest continuous shot; equal to video_max_seconds (8) turns the shots off

[selection.sections.opening]
share = 0.08                  # the shares of the five sections add up to 1
[selection.sections.development]
share = 0.47
```

The names and default values are in `vacation_memories/selection_config.py`.

## Long jobs on a server

A run on hundreds of files can take hours, mostly in the AI analysis and in the rendering. Do not tie it to an SSH
session: if the connection drops, the job can be stopped with it (in an orderly way, resumable, but stopped).

With `tmux`:

```sh
tmux new -s memories
docker compose run --rm -T app --duration 5m --music /music/track.mp3
# detach with Ctrl-b d; later: tmux attach -t memories
```

With `nohup`:

```sh
nohup docker compose run --rm -T app --duration 5m --music /music/track.mp3 > memories.out 2>&1 &
```

Follow the job from another session:

```sh
cat work/status.json        # phase, items done, estimated remaining time, state
tail -f work/run.log
```

A second run on the same working folder exits at once with code 5 and names the active one. An interrupted job
(Ctrl-C, `docker stop`, SIGHUP) exits with 130, 143 or 129: run the same command again with `--resume` added. The
parameters that change the result must stay the same (otherwise code 2); `--jobs`, `--nice` and the log options
may change. The segments already rendered are reused.

Exit codes: `0` done (also with too little material: the video is shorter and the log says why), `1` error,
`2` invalid usage or configuration, `3` the final file failed the checks (it is left as `<name>.partial.mp4`),
`4` something missing (FFmpeg, space, Ollama with `--require-ai`), `5` working folder in use, `128 + signal`
interrupted.

## Copying the results

The video, the plan and the reports are in the working folder of the server (`WORK_DIR`). Copy them with `rsync`
or `scp`, for example:

```sh
rsync -av --progress server:/path/to/vacation-memories/work/memories.mp4 .
rsync -av server:/path/to/vacation-memories/work/{edit_plan.json,render_report.json,run.log} reports/
```

The working folder has private permissions (`0700`): it holds thumbnails and metadata of private photos. The cache
(`cache.sqlite3`) and the segments can be deleted once the video is done; they only make later runs faster.

## Known limits

- Duplicates are recognized by a perceptual hash: the same scene with another framing, zoom or moment, or a photo
  and a frame of a clip of the same scene, are not grouped.
- Days come from the dates written in the files (EXIF, container tags). Without them the file date is used with
  the `--assume-tz` time zone; copies that reset the file dates put those files on the wrong day. Places are not
  known: a new day, not a new place, opens with a landscape.
- Small vision models may confuse `detail`, `transition` and `scenery`; the analysis covers only as many frames as
  the AI budget allows, and the others are ranked without it.
- When the AI labels almost everything with the same moment type, the rule "at most 3 clips of the same type in a
  row" gives way rather than the duration, and the report says so.
- A continuous shot grows only into parts of the clip that passed the technical threshold and were not grouped as
  near-duplicates: an almost still scene, whose parts look alike, keeps ordinary clips.
- The scores and shares are initial values, tuned on synthetic media only.
- Rendering above 1080p with the software encoder is much slower; use `--preview` for trials.

## Development

Code and tests run only in containers:

```sh
docker compose --profile dev build
docker compose run --rm -T dev pytest
docker compose run --rm -T dev python -m vacation_memories --input dev-media --no-ai --duration 30 --resolution 720p
```

In the `dev` container there is no compose configuration: pass `--input`; the working folder is `./work`. Since
`OLLAMA_HOST` points to the `ollama` service there, use `--no-ai`, or start Ollama and add `--allow-remote-ollama`.

## Troubleshooting

### Ollama on the integrated Intel GPU is unstable

**Symptoms** (seen with Ollama 0.40.2, the Vulkan backend and an integrated Intel GPU):

- `docker compose logs ollama` shows `vk::Queue::submit: ErrorDeviceLost`, sometimes followed by
  `llama-server terminated` or `terminate called after throwing an instance of 'vk::DeviceLostError'`.
- The vision requests fail with `Ollama answered with status 500`.
- After a crash the model can be very slow (30-50 s per frame instead of a few seconds) and answer with
  degenerate text or with JSON cut off before the end (`Unterminated string ...` in the log).
- `docker compose exec ollama ollama ps` still says `100% GPU`.

The application tolerates this. A request is retried once, a frame without an answer keeps only its
non-semantic scores, and after 3 frames in a row without an answer the AI analysis stops, leaving the
remaining frames without a semantic score. It is a problem of the GPU driver stack, not of the application,
but it can make the AI phase slow or incomplete.

#### What to do

1. Restart Ollama to reset the GPU state: `docker compose restart ollama`.
2. If it happens again, run the model on the CPU, which is slower but stable:
   - set `OLLAMA_VULKAN=0` in `.env` (the line is in `.env.example`);
   - recreate the container, because the variable is read only at creation: `docker compose up -d ollama`;
   - or, to turn the GPU off altogether, remove `:compose.intel-gpu.yaml` from `COMPOSE_FILE` in `.env` and
     recreate the container the same way.
3. Check where the model runs. With the model loaded, `docker compose exec ollama ollama ps` shows the
   PROCESSOR column (`100% CPU`, `100% GPU` or a mix). `docker compose run --rm -T app bench vision` prints
   `model runs on: ...` and records it in `<workdir>/runtime_profile.json`.
4. Measure the difference: run `bench vision` with and without `OLLAMA_VULKAN=0` and compare the seconds
   per frame. The AI time budget adapts to the speed measured at the start of each run.

On a CPU a single frame can take longer than the default request timeout of 120 s. For one run, raise it with
the environment variable (or `ai_timeout` in the configuration file):

```sh
docker compose run --rm -T -e VACATION_MEMORIES_AI_TIMEOUT=600 app bench vision --count 2
```

To go back to the GPU, comment out `OLLAMA_VULKAN=0` again (or restore the override in `COMPOSE_FILE`) and run
`docker compose up -d ollama`.

### Tests with the real model

The `-m ollama` tests use the real model and synthetic images only. Start Ollama first:

```sh
docker compose up -d ollama
docker compose run --rm -T dev pytest -m ollama
```

They are skipped, with a message, when Ollama does not answer or the model is missing. They allow a frame the
model fails on, because of the instability described above; a run in which every frame fails is still an error.
