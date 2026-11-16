# Vacation Memories

Creates a memory video (5 minutes by default) from the hundreds of photos and clips of a vacation. It discards
blurred, dark and repeated shots, picks the best moments, orders them into a story with an opening, a
chronological body and a calm closing, and adds background music. Everything runs locally, in Docker containers:
the originals are only read, never changed, and nothing leaves the machine.

There are three ways to use it, all on the same project files, so you can start with one and continue with another:

- the **command line** (`app`): analysis, plan and rendering in one command, also on a server without a display;
- the **editor**, a local web page where you look at the plan, change it (clips, transitions, music) and export the
  video;
- the **launcher**, a single file that starts the editor with a double click, for a PC.

**Contents:**
[Requirements](#requirements) ·
[Installation](#installation) ·
[Usage](#usage) ·
[Editor](#editor) ·
[Launcher](#launcher-one-file-double-click) ·
[Long jobs on a server](#long-jobs-on-a-server) ·
[Copying the results](#copying-the-results) ·
[Privacy](#privacy) ·
[Known limits](#known-limits) ·
[Evaluation](#evaluation) ·
[Docker](#docker) ·
[Development](#development) ·
[Troubleshooting](#troubleshooting) ·
[Documentation](#documentation) ·
[License](#license)

If you only want the editor on your own PC, go straight to "Launcher": it needs no repository and no `.env`.

## Requirements

- Linux with Docker Engine and the Docker Compose plugin. Nothing else is installed on the host: Python, FFmpeg
  and the libraries are in the images.
- Optional: an Intel GPU (`/dev/dri`) for the vision model. Without it the model runs on the CPU, more slowly.
  The program also works without any AI (`--no-ai`).
- Optional, for the launcher only: Google Chrome. Without it the editor opens in your default browser.
- Disk: about 3.3 GB for the vision model, the images, and a working folder with room for the segments (about twice the
  size of the final video).

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
     `:compose.selinux.yaml` on hosts with SELinux;
   - optional: `ASSUME_TZ`, the time zone of the files without a date in their metadata (for example
     `Europe/Rome`), and `OLLAMA_MODEL`, the vision model (default `qwen3-vl:4b-instruct-q4_K_M`).

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

   The first start of Ollama downloads the vision model (about 3.3 GB) through the `ollama-pull` service. A server
   without Internet needs the images and the models volume from another machine: see "Server without Internet".

4. Optional: measure the speed of the vision model on this machine with `docker compose run --rm -T app bench
   vision` (synthetic images; `--images /media/<folder>` uses your own photos, `--count N` how many). See also
   "Troubleshooting".

On the development PC, `./dev-media` can be filled with a synthetic vacation:

```sh
docker compose --profile dev build
docker compose run --rm -T dev python -m vacation_memories.evaluate.synthetic_media dev-media/vacation --vacation
```

### Environment variables

All of them go in `.env` (the template is `.env.example`) or in the shell, which wins over `.env`. Only the first five rows
are needed; the rest has a default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEDIA_DIR` | `./dev-media` | photos and clips, mounted read-only |
| `MUSIC_DIR` | `./music` | background music, mounted read-only |
| `WORK_DIR` | `./work` | cache, plans, logs, videos |
| `HOST_UID`, `HOST_GID` | `1000` | the user the containers run as: use `id -u` and `id -g` |
| `COMPOSE_FILE` | `compose.yaml:compose.intel-gpu.yaml` | the compose files in use (see "Docker") |
| `OLLAMA_MODEL` | `qwen3-vl:4b-instruct-q4_K_M` | vision model |
| `OLLAMA_VULKAN` | unset | `0` turns the Vulkan backend of Ollama off (see "Troubleshooting") |
| `ASSUME_TZ` | unset | time zone of the files without a date in their metadata |
| `EDITOR_ALLOWED_HOSTS` | `127.0.0.1:8080,localhost:8080` | `Host` values the editor accepts (SSH tunnel on another port) |
| `APP_VERSION` | `dev` | version written in the image label |
| `VM_IMAGE` | `vacation-memories:local` | image of `app` and `editor`; the launcher sets it to the one on Docker Hub |
| `IMAGE_REPO` | unset | Docker Hub repository written into the launcher; read only by `launcher-build` |

### Updating

After a change of the code (a `git pull`, or your own), rebuild the image, which holds a copy of it:

```sh
docker compose build app          # `editor` is the same image
docker compose run --rm -T app doctor
```

The cache in the working folder is keyed by the relative path, the size and the modification time of each file (and by
the analysis parameters), so an update does not make you analyze everything again; a file that changed is analyzed
again, and a cache from an incompatible version is rebuilt by itself. Copying the media with a tool that resets the
modification times (without `rsync -t` or `cp -p`) empties the cache.

## Usage

```sh
docker compose run --rm -T app --duration 5m --music /music/track.mp3 --output memories.mp4
```

`--input` (`/media`, that is `MEDIA_DIR`) and `--workdir` (`/work`, that is `WORK_DIR`) come from the compose
configuration (`docker/compose.toml`). A relative `--output` is written in the working folder, so the video above
ends up in `WORK_DIR/memories.mp4`. The music comes from `MUSIC_DIR`, mounted as `/music`. Use `-T` whenever there
is no terminal (scripts, `cron`, `nohup`).

The media folder is read with all its subfolders. Photos: JPEG, PNG and, when the decoder is available (`doctor`
says so), HEIC/HEIF. Clips: MP4, MOV, MKV. Other files are ignored.

To use another media folder for a single session, without editing `.env`, export the variables in the shell: Compose
prefers them to `.env`, and closing the shell restores it.

```sh
export MEDIA_DIR=/path/to/photos WORK_DIR=$HOME/vacation-work
mkdir -p "$WORK_DIR"
docker compose run --rm -T app --duration 3m --preview
```

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
| `--resolution` | `480p`, `720p`, `1080p` (default), `1440p`, `4k` (or `2160p`), or `WxH` such as `1920x1080`. A preset is the short side of the frame |
| `--aspect` | `16:9` (default), `9:16`, `1:1`, `4:3`. With `WxH` the ratio comes from the dimensions |
| `--music` | background music, a file under the music folder (`/music/track.mp3`, or `track.mp3`); shorter tracks loop with a crossfade, longer ones fade out at the end. A plan with its own `music` tracks ignores it |
| `--output` | the video file (`.mp4`); never inside the media folder |
| `--structure` | `narrative` (default: opening, beginning, development, finale, closing) or `chronological` |
| `--preview` | quick trial: short side 540 px, 24 fps, fast encoder; writes `<name>_preview.mp4` |
| `--no-ai`, `--require-ai` | without the vision model; or stop (code 4) if it is not available |
| `--model`, `--ai-frames`, `--ai-time-budget` | vision model, a fixed number of analyzed frames, or the seconds the AI may take (default 600) |
| `--ollama-url`, `--allow-remote-ollama` | address of Ollama; one that is not on this machine is refused unless allowed |
| `--assume-tz` | time zone of the files without a date in their metadata, e.g. `Europe/Rome` (`ASSUME_TZ` in `.env`) |
| `--jobs`, `--hwaccel`, `--nice` | parallel FFmpeg processes (`auto` or a number), hardware encoder (default `none`), lower priority (0-19) |
| `--input`, `--workdir` | media folder and working folder; with Compose they are already set |
| `--log-file`, `--log-format`, `--quiet`, `--config` | see "Configuration" |
| `--version` | prints the version |

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

### Other commands

| Command | What it does |
| --- | --- |
| `doctor` | checks the installation and exits with 0 unless a check fails: Python, the image version, FFmpeg with the encoders and filters it needs, the Python libraries, the HEIC decoder (a warning if missing), that the working folder is writable and has free space, the container limits, and Ollama with the model (not with `--no-ai`) |
| `bench vision` | seconds per frame of the vision model on this machine; `--images <folder>` uses your own photos, `--count N` how many (default 5) |
| `serve` | the editor's web server (see "Editor"); `--host` (default `127.0.0.1`), `--port` (default 8000) and `--allowed-host HOST:PORT`, repeatable. Compose already runs it behind `nginx` |
| `evaluate synthetic`, `prepare`, `report` | the measurement of the edit (see "Evaluation") |

Every command also takes the options that are not about the video itself: `--config`, `--input`, `--workdir`, the log
options, `--assume-tz`, `--jobs`, `--hwaccel`, `--nice` and the AI ones. `docker compose run --rm -T app <command> --help`
lists the options of each command.

### How the video is built

1. **Analysis.** Every photo, and every scene of a clip in parts of at most 10 s, gets a technical score (sharpness,
   exposure, contrast, black frames): below the threshold it is discarded, and of a group of near-duplicates only
   the best is kept. With the AI, the vision model gives each analyzed frame a moment type (`scenery`, `social`,
   `action`, `detail`, `transition`, `other`) and a value from 1 to 5. The final score weighs the narrative side
   (AI value, uniqueness, variety, motion, spread over time) more than the technical one. Without the AI there are
   no moment types, and the narrative side uses only uniqueness, motion and spread over time.
2. **Time order.** The capture time comes from EXIF `DateTimeOriginal` (then `DateTimeDigitized`, `DateTime`) for
   photos, and from `com.apple.quicktime.creationdate` or `creation_time`, the start of the recording, for clips. A
   part of a clip takes the start of the recording plus its position in the file. A file without a date in its
   metadata takes the date of the file, in the `ASSUME_TZ` time zone. Embedded dates are used as written, without
   converting time zones: what counts is the clock of the camera, and a `Z` suffix (as some action cameras write it)
   is ignored.
3. **Structure** (`--structure narrative`, the default). Five sections, as shares of `--duration` that can move
   within a range to fit the material:
   - Opening (10%): the best moments of the whole vacation, as quick clips of at most 3 s, one per clip, in time
     order;
   - Beginning (20%), Development (45%) and Finale (20%): the rest of the material, split into three consecutive
     stretches of time in proportion to the material, not to the calendar days. Each stretch is filled in score
     order and shown in time order;
   - Closing (5%): a calm, technically good shot, preferably from the end of the vacation. It is chosen by these
     criteria, so it may come from an earlier moment.

   Time therefore goes back at the start of the Beginning, because the Opening is a teaser of the whole vacation,
   and sometimes at the Closing. Targets under 60 s, or fewer than 15 usable clips, use a compressed structure: an
   Opening of at most 5 s, one chronological body and a Closing of one clip. `--structure chronological` drops the
   sections: one selection in time order.
4. **Variety.** At most 3 clips of the same moment type in a row; no file over 20% of the duration (at least 10 s);
   `transition` clips only between two days and at most 10% of the time. A section that runs out of material gives
   its time to the others, never by repeating clips.
5. **Durations.** Photos 3-4 s, clips 3-8 s (`scenery` 0.5 s longer, `detail` at most 3 s); under 60 s, photos
   2-3 s and clips 2-4 s. The durations are adjusted so that the video lasts what was asked; if the material is not
   enough, the video is shorter and the report says why.

A clip with several good moments is not cut into many short pieces: it gives one piece to each section, the Opening
included. The clip of the Closing gives none to the section before it, where it would end one section and start
again in the next. A further good moment of a clip already in the section lengthens that shot, without a break,
into the next part of the same clip, up to 15 s (`video_shot_max_seconds`); in the Opening, and once the shot is
full, the other moments are left out. A second piece of the same clip comes only when the section would otherwise
run short, and the report says so. In the Closing this gives one calm, continuous shot. Such a shot says
"continuous shot over N adjacent segments" in its reason, and `runtime.extended_shots` counts them. Targets under
60 s and the `detail` and `transition` clips keep their ordinary durations.

### Plan and report

`WORK_DIR/edit_plan.json` lists every clip with its section, scores (`technical_score`, `narrative_score` and the
final score), moment type, capture date and the reason it was chosen. Its `runtime` key says how the sections were
filled, whether a budget was redistributed, how much the AI covered, and why the video is shorter when the material
is not enough. `render_report.json` describes the rendering and the checks of the final file; a `--preview` writes
`render_report_preview.json` instead, so it never overwrites it.

The plan is `schema_version: 2`. Every clip has a stable `id`, an `origin` (`planner`, or `user` for a clip added by
hand) and its `effects` (`fade_in`, `fade_out`, `dissolve`, `dip`, each with `offset` and `duration`): a dissolve is
stored once, in the incoming clip, and the fade from black and to black are effects of the first and last clip. At
plan level `excluded` lists the moments deleted by hand, which the planner never chooses again, and `music` the
tracks, their parts and the shift of the music. A schema 1 plan is still read and rendered as before (its
`transition_in` becomes a dissolve effect); it is written as schema 2 the next time it is saved.

The renderer applies the effects of every clip: `fade_in` and `fade_out` at the edges of the clip (also in the middle
of the video), a `dip` (to black and back) anywhere, and the dissolves as before. The fade from black and to black of
the video are no longer global: they are the effects of the first and last clip. An effect that leaves its clip,
overlaps another effect or a dissolve, or lasts less than 0.2 s is reduced or left out, with a warning, and
`render_report.json` lists what was applied and what was changed (`effects`).

The `music` block of the plan lists the tracks (files relative to the music folder, `music_dir`), in order, each cut
into parts (`start` and `end` in seconds of the track, `muted`, `fade_in`, `fade_out`), and an `offset`: positive, the
music starts later; negative, it skips its beginning. The music beyond the end of the video is cut, with a fade out as
long as the last `fade_out` effect of the video (3 s without one), and a music that is shorter leaves the rest silent.
A plan without a `music` block plays the `--music` file as before.

A full run or `--dry-run` over a plan that holds edits made by hand (clips with `origin: user`, a changed photo
duration, effects of the user, `excluded`, `music`) first copies it into `WORK_DIR/plan_history/` (the last 10
copies) and keeps its `excluded` moments out of the new plan. A plan without edits is simply replaced.

After every analysis (`--analyze-only`, `--dry-run`, a full run) the pipeline also writes
`WORK_DIR/candidates.json`: the media files with the size and `mtime` they had, and every candidate with its
scores, AI fields, day, group of near duplicates and, when it was left out, the reason. Paths are relative to the
input folder.

### Configuration

Every option can also come from a TOML file (`--config`, or the `VACATION_MEMORIES_CONFIG` variable) or from a
`VACATION_MEMORIES_<OPTION>` variable; the command line wins over the variables, which win over the file. The
`app` service reads `docker/compose.toml`. The weights, thresholds and shares of the selection are in the
`[selection]` table, for example:

```toml
duration = "4m"
music_dir = "/music"          # the root of the music: --music and the tracks of the plan are relative to it
music = "track.mp3"

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

The names and default values are in `vacation_memories/planning/selection_config.py`.

## Editor

The editor is a local web server on top of the CLI: the analysis, the plan and the rendering stay the CLI, run as
child processes on the same files, so a project can be continued from either. The interface is in Italian, in React and
TypeScript (`frontend/`). It shows the analysis and the plan, and lets you edit the clips, the transitions and the music,
preview the result and export the video.

```sh
docker compose build editor           # after every change of the code
docker compose up -d editor nginx     # starts the editor and its proxy; Ollama is not needed
docker compose stop editor nginx
```

Then open `http://127.0.0.1:8080`. The port is published only on the `127.0.0.1` of the machine that runs Docker. On a
server without a display use an SSH tunnel and open the same address on your PC:

```sh
ssh -L 8080:127.0.0.1:8080 <server>
```

For a tunnel on another local port (`ssh -L 9090:127.0.0.1:8080 …`) add that address to `EDITOR_ALLOWED_HOSTS` in `.env`
(`127.0.0.1:9090,localhost:9090`): the editor refuses requests whose `Host` or `Origin` is not one of its own, so no
other web page open in your browser can use it.

- **The flow.** The first page lists the folders under `MEDIA_DIR` with the number of photos and videos and the state of
  their project. A folder without a plan opens the settings (duration, structure, format, music, AI) and, with
  "Analizza", the analysis with its progress and a stop button; if Ollama is not available the page offers to wait or to
  continue without AI. A folder with a plan opens the editor: the Media panel (photos by interest score, videos with
  their moments, transitions, music), the Detail panel (preview, scores, limits, the summary of the whole video) and the
  timeline with zoom, scrolling, the playhead, the transitions on the clips and the music. The settings stay reachable
  from the top bar. The address holds the project (`#/p/<id>`), so a reload comes back to it.
- **Editing the clips.** Photos and moments go into the timeline by dragging them from the Media panel (or with "Aggiungi
  alla timeline", "Aggiungi tutte" for a group and "Aggiungi i momenti" for a video); a clip is reordered by dragging it
  (or with the "Sposta prima" and "Sposta dopo" buttons, or Alt and the arrows). A photo lasts 1 to 30 seconds: drag the
  two handles of the selected photo, use the slider of the Detail panel or the arrow keys (0.1 s, Shift 1 s). A moment is
  trimmed from the two handles on its bar in the Videos tab or from the sliders of the Detail panel, and a click on a free
  point of the bar adds a moment of about six seconds. "Nascondi" (a photo) and "Elimina" (a moment) take an item out of
  the timeline and tell the planner not to choose it again; "Ripristina" brings it back. Every limit comes from the
  server, which checks the plan again on every save.
- **Undo and saving.** Undo and redo (the top bar, Ctrl+Z, Ctrl+Shift+Z or Ctrl+Y) cover the last 60 changes; a drag or a
  slider is one step. The plan is saved after every change, with the revision that was read: if it changed elsewhere (for
  example by a command-line run) the editor offers to reload the version on disk or to overwrite it, keeping the
  overwritten one in `plan_history/`. Delete removes the selected clip, Space plays and stops the preview, Escape clears
  the selection. While another process uses the project, or the analysis runs, the plan is read-only.
- **A new plan.** "Rigenera il piano…" in the settings builds the plan again, after a confirmation. If the current plan has
  your edits a copy goes into `plan_history/`, and the hidden photos and deleted moments are not chosen again. A
  reordering or a removal from the timeline is not, by itself, recognized as an edit: for what must not come back use
  "Nascondi" or "Elimina".
- **Editing the transitions.** A transition of the library (Transizioni tab: "Taglio netto", "Da nero", "A nero",
  "Dissolvenza", "Passaggio per il nero") is dragged onto a clip of the timeline; without a drag, the buttons of a selected
  clip put it at its start or end ("Da nero all'inizio", "Dissolvenza all'inizio", "Dissolvenza alla fine", "A nero alla
  fine", "Passaggio per il nero qui", the last one at the playhead), and "Applica a tutto il video" of a library
  transition puts it wherever it makes sense (a dissolve between all the clips, "from black" on the first, "to black" on
  the last, the hard cut takes every dissolve away). A transition moves by dragging, also onto another clip, snapping to the
  nearest place it is allowed; it is resized with the two handles that appear when it is selected, the Detail sliders or
  the arrow keys (0.1 s, Shift 1 s); Delete, "Rimuovi la transizione" or the hard cut dragged onto it remove it. A transition
  never leaves its clip nor overlaps another, lasts at least 0.2 s, and a dissolve fits in both clips it joins; a boundary
  has one transition, so a new one replaces the one it finds there.
- **Editing the music.** Tracks of the music root (Musica tab) go onto the music lane by dragging them or with the "+"
  button of their row; the tracks lie one after another, and are reordered by dragging their names on the lane or with the
  "Brano prima" and "Brano dopo" buttons. "Taglia la musica qui" cuts the part under the playhead in two (each piece at
  least a second); a part can be muted, has its own fade-in and fade-out, and "Elimina la parte" makes the following ones
  shift (a track left without parts leaves the video; "Ripristina le parti eliminate" puts back what the tracks in the video
  leave uncovered). The whole music is shifted against the video by dragging the body of a part, the arrow keys on it
  (0.1 s, Shift 1 s) or the Detail slider, and "Riporta la musica all'inizio" brings it back; the part beyond the end of the
  video is dimmed, and the export cuts it there with the final fade-out. Once the plan has a `music` block it replaces the
  `--music` option.
- **Preview.** The preview of the whole video is a job (`--render-only --preview`, short side 540 px, 24 fps) that starts by
  itself after every new plan, unless "Prepara l'anteprima dopo ogni nuovo piano" is off in the settings; "Anteprima
  completa" in the summary of the Detail panel starts it by hand. It plays in the summary; after any edit it is marked
  "Anteprima non aggiornata" (top bar and summary) and "Aggiorna l'anteprima" makes it again. It is not real time, and a plan
  saved while it renders leaves it marked as outdated. The preview of a single clip, in the Detail panel, follows the trim
  and the duration (a moment after the last change) and has a retry button if FFmpeg could not make it. A moment that is
  not in the timeline, and a whole video of the Media panel, play in the same way (the first request for a long video waits
  for FFmpeg); playing them does not move the playhead. A photo outside the timeline shows its thumbnail.
- **Export.** "Esporta video" in the top bar asks for a file name (no extension: `memories` by default, the same as the
  last export, with a warning if it would replace a file), then shows the progress with a stop button. When it ends the
  dialog says where the file is (the path from `WORK_DIR`, for example `projects/<name>-<hash>/memories.mp4`) and offers it
  for download; the summary lists every export with its path and a download link. The editor stays usable while it renders
  (the render keeps the version of the plan it read when it started, and the page says so); only one job runs at a time, so
  the button is off with the reason while another one runs, while edits are being saved or when there is no plan. A stopped
  export starts again from where it was with the same name. If the final check fails (code 3) the message of the CLI is
  shown and the `<name>.partial.mp4` file is offered for inspection; code 4 and the others show their message too.
- **Projects.** The editor lists the folders under `MEDIA_DIR`. Every folder has its own working folder,
  `WORK_DIR/projects/<name>-<hash>/`, with the cache, the plan (`edit_plan.json`), `candidates.json` (the catalog the
  analysis leaves for the editor), `project.json` (the settings), the logs and the videos. The CLI can work on the same
  one: `docker compose run --rm -T app --input /media/<folder> --workdir /work/projects/<name>-<hash> …`.
- **Jobs.** Analysis and plan, preview and export are `python -m vacation_memories` processes started by the editor,
  one at a time. Stopping the editor (`docker compose stop`) stops the job in an orderly way, and running it again
  resumes it.
- **Access.** There is no authentication: on `127.0.0.1` any user of the machine can open the editor and so see the
  photos. Use it on your own PC, or on a server whose users you trust.
- **Without Ollama.** The editor starts and works with Ollama stopped; it checks it only when you ask for an analysis
  with AI, and then it never produces a result without AI silently.

### The interface (`frontend/`)

React and TypeScript, bundled with esbuild and type-checked with `tsc` (strict). The plan and API types are in
`frontend/src/api/types.ts`; the texts are in a catalog per language (`frontend/src/i18n/it.ts` is the reference, the
values are Italian and everything else is English); the fonts (IBM Plex Sans, IBM Plex Mono and Bricolage Grotesque, all
under the SIL Open Font License) are bundled, and the page makes no request outside the editor. The versions and
licenses of the dependencies are in `frontend/THIRD-PARTY.md`.

```sh
docker compose run --rm -T ui-dev npm run check   # tsc --noEmit
docker compose run --rm -T ui-dev npm test        # the tests of the interface
docker compose build editor                       # after a change of the interface: the bundle is built in the image
```

`ui-dev` has the repository mounted, no network and no media; its dependencies are in the image, so a change to
`frontend/package.json` needs `docker compose --profile dev build ui-dev`. Node is only in the build stage and in
`ui-dev`, never in the image that runs. The bundle (`frontend/dist/`) is not committed.

The browser tests (Playwright, in `frontend/e2e/`) drive the editor in a real Chromium, on a synthetic vacation:

```sh
docker compose run --rm -T dev python frontend/e2e/make_media.py   # once: dev-media/e2e-vacation and dev-media/e2e-music
docker compose build editor                                         # the page under test is the one in the image
MUSIC_DIR=./dev-media/e2e-music docker compose run --rm -T e2e npm test   # starts editor and nginx; a few minutes
docker compose stop editor nginx
```

`MUSIC_DIR=./dev-media/e2e-music` gives the editor the two synthetic tracks the music tests use, instead of your own music
folder (the editor started that way keeps them until it is started again without the variable).

The `e2e` service shares the network of `nginx`, so the page is `http://localhost:8080` as in your browser. The tests
make their plan with the real analysis job (no AI), put it back before each test, and check both the page and the plan the
server saved; the screenshots of a failure are in `frontend/e2e/results/`. A change to `frontend/e2e/package.json` needs
`docker compose --profile dev build e2e`.

### Launcher (one file, double click)

The launcher is a single executable that starts everything and opens the editor in its own window. Every release has it,
as `vacation-memories-X.Y.Z-linux-x86_64` with its `.sha256`, on the GitHub releases page. It needs
Docker Engine with the Compose plugin and Google Chrome on the PC, nothing else (Python is inside the file). Without
Google Chrome the page opens in your default browser (`xdg-open`); then nothing tells the launcher that you have finished,
so stop it with the menu action "Stop Vacation Memories" or `vacation-memories --stop`.

```sh
sha256sum -c vacation-memories-X.Y.Z-linux-x86_64.sha256     # in the folder of the download
chmod +x vacation-memories-X.Y.Z-linux-x86_64
./vacation-memories-X.Y.Z-linux-x86_64 --install-desktop    # adds "Vacation Memories" to the applications menu
```

The menu entry points to the file where it is, so run `--install-desktop` again if you move it. Then a double click
works.

On the first start the window asks the folders (the vacations, where to write the projects and videos, the music), saves
the answers in `~/.config/vacation-memories/`, downloads the application image from Docker Hub (a few minutes, once),
starts the editor and shows it. Closing the window stops the containers. `--reconfigure` (or the "Change folders and
settings" action of the menu entry) asks the folders again; `--stop` stops a running launcher and its containers;
`--browser PATH` chooses another Chrome; the log is
`~/.config/vacation-memories/launcher.log`. Your repository `.env` is not used by the launcher. The launcher runs the
editor (and Ollama, in the background, with the Intel GPU if `/dev/dri` exists): for the command line and the
`evaluate` commands use the repository (see "Installation").

To remove what the launcher left on the computer, run `--uninstall`: it stops the launcher, removes the containers and
the networks, deletes `~/.config/vacation-memories/`, `~/.local/share/vacation-memories/` (the extracted files and the
default music folder) and the menu entry. Add `--remove-images` for the Docker images, `--remove-models` for the Docker
volume `vacation-ollama-models` (the AI models, several GB, shared with an installation from the repository) and
`--remove-work-data` for the projects and the **exported videos** in the work folder (only what the application wrote
there; it asks for a confirmation, or add `--yes`). `--all` is the three together. The folders with the vacations and
the music are never touched.

To build it yourself instead, once the image of that version is on Docker Hub (the CI publishes it on the release tag,
see "Continuous integration"; `IMAGE_REPO` is the value of the `IMAGE_NAME` variable, e.g. `yourname/vacation-memories`).
The file is `dist/vacation-memories`:

```sh
docker compose --profile dev build launcher-build
IMAGE_REPO=yourname/vacation-memories APP_VERSION=0.2.0 docker compose run --rm -T launcher-build
```

Without the CI, publish the image yourself first: `docker build --target runtime --build-arg APP_VERSION=0.2.0 -t
yourname/vacation-memories:0.2.0 .` and `docker push yourname/vacation-memories:0.2.0` (it logs in with your Docker Hub
account).

The file embeds `compose.yaml` and the files it needs, so it can be copied to another PC on its own. It is built on an
old glibc and runs on any Linux x86-64 with a newer one. A change to `requirements-launcher.txt` needs
`docker compose --profile dev build launcher-build`.

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
(Ctrl-C, `docker stop`, SIGHUP) exits with `128 + signal`: run the same command again with `--resume` added. The
parameters that change the result must stay the same (otherwise code 2); `--jobs`, `--hwaccel`, `--nice` and the
log options may change. The segments already rendered are reused.

Exit codes: `0` done (also with too little material: the video is shorter and the log says why), `1` error,
`2` invalid usage or configuration, `3` the final file failed the checks (it is left as `<name>.partial.mp4`),
`4` something missing (FFmpeg, space, Ollama with `--require-ai`), `5` working folder in use, `128 + signal`
interrupted (130 Ctrl-C, 143 `docker stop`, 129 SIGHUP).

## Copying the results

The video, the plan and the reports are in the working folder of the server (`WORK_DIR`). Copy them with `rsync`
or `scp`, for example:

```sh
rsync -av --progress server:/path/to/vacation-memories/work/memories.mp4 .
rsync -av server:/path/to/vacation-memories/work/{edit_plan.json,render_report.json,run.log} reports/
```

The working folder has private permissions (`0700`): it holds thumbnails and metadata of private photos. The cache
(`cache.sqlite3`) and the segments (`segments/`) can be deleted once the video is done; they only make later runs
faster.

## Privacy

The program is built so that the photos stay on your machine:

- The originals are mounted read-only and are never modified; the program refuses an `--output` inside the media folder.
- The vision model runs in a local Ollama container. `app`, `editor` and `dev` are on an internal Docker network with no
  route to the Internet; only `ollama` can reach it, to download models. A remote Ollama is refused unless you pass
  `--allow-remote-ollama`.
- The editor's page makes no request outside the editor (fonts included), and the editor accepts only its own `Host` and
  `Origin`, so no other web page open in your browser can use it. The only published port is `127.0.0.1:8080`. It has no
  authentication: see "Editor", item "Access".
- The working folder (`0700`) holds thumbnails, previews, metadata and descriptions of your photos. Do not publish or
  commit it. Of the evaluation, only `eval_summary.json` is anonymous and meant to be shared.
- The images run as a normal user with no capabilities and no Docker socket; see "Services and networks".

## Known limits

- Duplicates are recognized by a perceptual hash: the same scene with another framing, zoom or moment, or a photo
  and a frame of a clip of the same scene, are not grouped.
- Days come from the dates written in the files (see "How the video is built"). A file without one takes its file
  date, so a copy that resets the file dates puts it on the wrong day. Places are not known: a new day, not a new
  place, opens with a landscape.
- The order follows the clock of each camera. Two cameras whose clocks differ (or one set to another time zone)
  interleave their shots wrongly, and a file with a wrong date ends up far from its moment: remove it from the
  media folder, or fix its date in a copy.
- Small vision models may confuse `detail`, `transition` and `scenery`; the analysis covers only as many frames as
  the AI budget allows, and the others are ranked without it.
- When the AI labels almost everything with the same moment type, the rule "at most 3 clips of the same type in a
  row" gives way rather than the duration, and the report says so.
- A continuous shot grows only into parts of the clip that passed the technical threshold and were not grouped as
  near-duplicates: an almost still scene, whose parts look alike, keeps ordinary clips.
- The scores and shares are initial values, tuned on synthetic media only.
- Rendering above 1080p with the software encoder is much slower; use `--preview` for trials.

## Evaluation

`evaluate` measures the edit, so that every change to the ranking, the duplicates or the structure can be compared
with the previous one. No threshold of success is fixed in advance: the first run sets the reference.

### Synthetic evaluation

```sh
docker compose run --rm -T --no-deps app evaluate synthetic
docker compose run --rm -T dev python -m vacation_memories evaluate synthetic --seeds 100 --duration 90
```

It generates a synthetic vacation with known truth (4 days, 2 places a day, about 90 files; generated once, then
reused) in `WORK_DIR/synthetic-eval/`, analyzes it without AI, and plans it twice: with the real selection, and at
random as many times as `--seeds` (default 100). The random plans keep the section budgets and the clip durations,
but take their clips by chance among all the readable files, blurred and duplicate ones included. Everything stays
at plan level: nothing is rendered and Ollama is not needed, so `--no-deps` is enough.

The table, also saved in `synthetic_report.json`, puts each metric of the real plan next to the mean, spread and
range of the random plans, with the share of random plans it beats or ties:

- important moments included: fast pans, and scenes seen nowhere else that day;
- repetitions: near-identical bursts, and the same scene framed differently (which the perceptual hash does not
  recognize, so the plan is not expected to do better than chance there);
- blurred or black shots selected;
- days and places covered, balance between the days, largest share of a single file;
- section budgets, sections inside their allowed range, clips inside their duration limits.

The `[selection]` table of the configuration applies, so a change of weights or thresholds can be measured. These
metrics find regressions and check the evaluation itself; they do not say whether the video is pleasant. To look at
the vacation, write it on its own (with its `ground_truth.json`):

```sh
docker compose run --rm -T dev python -m vacation_memories.evaluate.synthetic_media dev-media/evaluation --evaluation
```

### Evaluation on your vacation

A blind comparison of edits of your own material, which you watch and rate. It runs on the server; the review
happens in a browser, on any computer. The first cycle sets the baseline.

1. Prepare the previews and the review page. The analysis comes from the cache, and Ollama is used as in a normal
   run (`--require-ai` included):

   ```sh
   docker compose run --rm app evaluate prepare --duration 90
   ```

   It writes `WORK_DIR/eval/<run_id>/`: `--run-id` chooses the name, by default the date and time. Inside, there is
   a low-resolution preview of each variant, under a letter drawn in random order:

   - `full`: the real selection;
   - `random`: clips drawn by chance from every readable file, with the same section budgets and clip durations.
     The seed is recorded; `--seed` fixes it;
   - optional, with `--variants full,random,no-ai,technical-only`: `no-ai`, the same candidates scored without the
     vision model, which tells whether the model is worth its time; and `technical-only`, the best by technical
     quality, which tells how much the narrative part adds.

   The folder also holds the thumbnails and `review.html`. `--music` adds a background track to the previews, and
   `--aspect` changes their shape. The program never opens a browser: it prints the path.

2. Copy the folder to the computer you watch it on, outside the repository. Leave out the JSON files: they say which
   letter is which variant.

   ```sh
   rsync -a --exclude '*.json' server:/path/to/WORK_DIR/eval/<run_id>/ ~/vacation-review/<run_id>/
   ```

3. Open `review.html`. It is a local file, so no server and no Internet are needed. Your work is kept in the browser
   while you go.

   - For each version: watch it, and click a start time to jump there. Flag clips as repetitive, too long or too
     short, judge the rhythm of each part, and rate the version from 1 to 5.
   - In *Left out*: flag the moments that should be in the video. These are the best excluded candidates, plus a
     sample for each reason.
   - In *Rank and export*: rank the versions and export `ratings.json`.

4. Copy `ratings.json` back into the run folder on the server, then make the report. The report needs no Ollama:

   ```sh
   scp ratings.json server:/path/to/WORK_DIR/eval/<run_id>/
   docker compose run --rm --no-deps app evaluate report --run-id <run_id>
   ```

   Without `--run-id` it reports the latest prepared run. `--ratings` reads the file from another place; a relative
   path is inside the workdir.

`report` checks the ratings before it reads which letter is which variant. It writes two files:

- `eval_report.md`: the answers to the five questions and the comparison with the previous cycle. It is private,
  because it names your files. The five questions are: important moments left out (and why), repetitive scenes,
  balance (ratings, rhythm, days, sources, moment types, budgets), clip durations, and the comparison with the
  random selection;
- `eval_summary.json`: anonymous. Segment ids are hashed with a secret of the workdir (`eval/.salt`) and days are
  relative, with no paths, file names, thumbnails, descriptions or dates. It is the only file to share for tuning
  weights and thresholds.

Your flags are remembered in `WORK_DIR/eval/labels.json`, by segment, even when a new analysis cuts the clips
slightly differently. The next cycle fills them in and marks what is new, so only that needs reviewing.
`eval_history.json` keeps every cycle. With few cycles, the comparison with the random selection is indicative, not
statistical, and the report says so.

The `eval/` folder holds previews and thumbnails of private photos: keep it in `WORK_DIR` and, on the PC, outside
the repository. For every change to the ranking, the duplicates or the structure:

1. change the configuration or the code;
2. run `evaluate prepare`;
3. review the versions;
4. run `evaluate report`;
5. compare with the previous cycle and with `random`.

## Docker

Docker is the only way to run the program, on the development PC and on the server. A single `compose.yaml`
serves both; only `.env` changes.

| File | Purpose |
| --- | --- |
| `compose.yaml` | the services `editor`, `nginx`, `ollama`, `ollama-pull`, `app` (profile `cli`), `dev`, `ui-dev`, `e2e` and `launcher-build` (profile `dev`) |
| `compose.intel-gpu.yaml` | gives `ollama` the Intel GPU (`/dev/dri`, `OLLAMA_IGPU_ENABLE=1`) |
| `compose.selinux.yaml` | `label=disable` for `app`, `dev` and `editor` |
| `compose.test.yaml` | for `scripts/test-docker.sh` only: a simulated Ollama, no model downloaded |
| `docker/compose.toml` | the configuration of `app` and `editor` (`/media`, `/work`, the Ollama URL, `require_ai`) |
| `docker/nginx.conf` | the configuration of `nginx`, the reverse proxy of the editor |

### Services and networks

- `app` is the `runtime` image: Python, FFmpeg and the code, nothing else. It has no compilers, package manager,
  tests, fixtures, `.git`, `.env` or media. The entrypoint is `python -m vacation_memories`, so the arguments of
  `docker compose run app` are those of the CLI. The code is copied in the image: after every change run
  `docker compose build app` (or `editor`: it is the same image).
- `app` is in the `cli` profile, so a bare `docker compose up` does not start a full job on `MEDIA_DIR`;
  `docker compose run app …` still works, because naming a service enables its profile.
- `editor` is the same image with the `serve` command (see "Editor"); `nginx` is the reverse proxy in front of it.
- `ollama` serves the vision model; the models are in the external volume `vacation-ollama-models`. `ollama-pull`
  downloads the model only when it is missing, then exits.
- `dev` is the `dev` image with the repository mounted at `/src`, for the tests. It never mounts `MEDIA_DIR`.
- `ui-dev` is the Node image of the interface with the repository mounted, for `npm run check` and `npm test`. Like
  `dev` it never mounts `MEDIA_DIR`, and it has no network at all (`network_mode: none`).
- `e2e` is Playwright with Chromium, for the browser tests of the editor. It shares the network of `nginx` (so the page is
  `http://localhost:8080`), has no network of its own and no media; the screenshots of a failure are in
  `frontend/e2e/results/`.
- `launcher-build` is the PyInstaller image that builds the launcher into `./dist`. Like `ui-dev` it has the repository
  mounted, no network and no media.
- `app`, `editor` and `dev` sit only on the internal network `backend`, which has no route to the Internet: they
  reach Ollama and nothing else. Only `ollama` is also on `egress`, to download models. `nginx` is on `backend` and on
  `frontend`, an ordinary network with nothing else on it (an internal network cannot publish a port). The only
  published port is `127.0.0.1:8080`, of `nginx`.

`app` and `editor` run as `HOST_UID:HOST_GID` (the files in `WORK_DIR` are yours), with a read-only root filesystem, no
capabilities (`cap_drop: [ALL]`), `no-new-privileges`, `init: true` and 30 s to stop. The only writable folder is
`/work`; `/media` and `/music` are read-only. The image works with any UID: nothing is written outside `/work`.
There is no `--privileged` and no Docker socket anywhere.

### Image version

The image carries its version in the label `org.opencontainers.image.version` and in `IMAGE_VERSION`, and
`doctor` prints it with the FFmpeg version. Set it at build time:

```sh
APP_VERSION=0.2.0 docker compose build app      # or APP_VERSION in .env
docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' vacation-memories:local
```

Without `APP_VERSION` the label is `dev`. The base image is pinned by tag, not by digest: the `PYTHON_IMAGE` default
of the `Dockerfile` is `python:3.12-slim-bookworm`, so a new build picks up the security updates of that tag, and two
builds of the same version can differ in the base layers. To pin an exact base, build with `docker build --target runtime --build-arg
PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest> .` (read the digest with
`docker buildx imagetools inspect python:3.12-slim-bookworm`). The image is built for the
architecture of the machine; for another one use `docker buildx build --platform linux/arm64 --target runtime ...`. A dependency
without a wheel for that architecture makes the build fail.

### Resource limits

Ollama and the program share the machine. The program reads the limits of its container (cgroup) and adapts the
number of parallel jobs and the memory it uses (`runtime_profile.json` and `doctor` show them). To limit the
services, put the limits in a file of your own and add it to `COMPOSE_FILE`:

```yaml
# compose.limits.yaml
services:
  app:
    cpus: 4
    mem_limit: 8g
  ollama:
    cpus: 4
    mem_limit: 8g      # at least the size of the model (about 3.3 GB) plus its context
```

```sh
COMPOSE_FILE=compose.yaml:compose.intel-gpu.yaml:compose.limits.yaml
```

### Server without Internet

The build downloads the base image and the packages, and Ollama downloads the model. On a machine that has
Internet, with the repository and Docker:

```sh
docker compose build app
docker volume create vacation-ollama-models
docker compose run --rm -T ollama-pull        # starts Ollama and downloads the model into the volume
docker compose stop ollama
docker save vacation-memories:local ollama/ollama:0.40.2 | gzip > vacation-images.tar.gz
docker run --rm -v vacation-ollama-models:/models:ro -v "$PWD":/backup --entrypoint tar \
    ollama/ollama:0.40.2 czf /backup/vacation-models.tar.gz -C /models .
```

Copy the two archives and the repository (without `.env`) to the server, then:

```sh
docker load < vacation-images.tar.gz
docker volume create vacation-ollama-models
docker run --rm -v vacation-ollama-models:/models -v "$PWD":/backup:ro --entrypoint tar \
    ollama/ollama:0.40.2 xzf /backup/vacation-models.tar.gz -C /models
cp .env.example .env     # and edit it, as in "Installation"
docker compose run --rm -T app doctor
```

The image is not built on the server: Compose builds only when `vacation-memories:local` is missing, and the
loaded one is there (do not add `--build`). To update the program, transfer a new image the same way.

### Container tests

`scripts/test-docker.sh` tests the images and the compose files with the Docker CLI alone, on synthetic media. It
ignores `.env` and the environment of the shell: its folders are in `./.test-docker` (removed at the end), the
project has its own name, and Ollama is simulated by `compose.test.yaml`, so no model is needed and nothing of the
normal setup is touched. It needs Docker Compose 2.24.4 or later and a few minutes.

```sh
scripts/test-docker.sh                 # all the checks
scripts/test-docker.sh -k 'cycle|stop' # only the checks whose name matches
scripts/test-docker.sh --keep          # keep ./.test-docker for inspection
```

It checks that:

- the images build and `pytest` passes in `dev`;
- `doctor --no-ai` exits with 0 without starting Ollama, with `libx264`, AAC and all the filters;
- the `runtime` image runs as non-root and holds no media, `.git`, `.env`, fixtures or non-headless OpenCV;
- a full job on synthetic media (with music, AI through the simulated Ollama) produces a video that passes the
  FFprobe checks, leaves the originals unchanged and writes nothing outside `/work`;
- `docker stop` during the rendering exits with 143 in less than 10 s, FFmpeg does not stay behind, and `--resume`
  completes;
- with `cpus: 1` and `mem_limit: 1g`, `runtime_profile.json` reports at most 1 core and 1 GiB;
- `dev` has no media folder, no Internet and reaches Ollama, and its files belong to you;
- `app` is hardened as above, reaches Ollama but not the Internet, and no port is published;
- the editor: only `nginx` publishes a port, only on `127.0.0.1:8080`; `editor` is only on `backend`, without Internet
  and without `depends_on` on Ollama, and serves the page through `nginx` with Ollama stopped, refusing a foreign `Host`
  or `Origin`; an analysis and plan job and a preview run through `nginx`; `docker stop` during a render exits with 143
  in less than 10 s and the same job then finishes; the page served through `nginx`, its script and its stylesheet have
  no external reference and its fonts are served; a bare `docker compose up` does not start `app` or `ui-dev`, and
  `docker compose run app` still works;
- `ui-dev` has no network and no media folder, its files belong to you, and `npm run check` and `npm test` pass;
- the compose files are valid: clear errors without `MEDIA_DIR` or `HOST_UID`, no folder created for a missing
  mount, the GPU only on `ollama` and only with the override, `OLLAMA_VULKAN` only when set, SELinux only on `app`,
  `dev` and `editor`.

## Development

Code and tests run only in containers:

```sh
docker compose --profile dev build
docker compose run --rm -T dev pytest
docker compose run --rm -T ui-dev npm run check && docker compose run --rm -T ui-dev npm test   # the interface
docker compose run --rm -T dev python -m vacation_memories --input dev-media --no-ai --duration 30 --resolution 720p
scripts/test-docker.sh      # the production image and the compose files, see "Container tests"
```

In the `dev` container there is no compose configuration: pass `--input`; the working folder is `./work`. Since
`OLLAMA_HOST` points to the `ollama` service there, use `--no-ai`, or start Ollama and add `--allow-remote-ollama`.

### Code layout

The package `vacation_memories/` keeps the entry points at the top (`__main__`, `cli`, `pipeline`) and groups the
other modules by role:

| Subpackage | Contents |
| --- | --- |
| `core/` | configuration, `doctor`, runtime profile, FFmpeg runner and orderly shutdown, workdir lock and `status.json`, atomic writes, logging, exit codes |
| `analysis/` | scanning, metadata, probing, scenes, frame sampling, technical quality, duplicates, analysis cache; `runner` runs the whole analysis of a folder |
| `ai/` | Ollama client, vision prompt and schema, vision analyzer, `bench vision` |
| `planning/` | selection weights, ranking, narrative planner, edit plan, transitions, `--duration`/`--resolution`/`--aspect`, estimates |
| `rendering/` | segments (phase A), assembly (phase B), effects of the clips, music, filters, encoders, checks of the output |
| `editor/` | the `serve` subcommand: HTTP server and API, projects, jobs, catalog, thumbnails, waveforms and per-clip previews, and `static/` (the built interface: it exists only in the image) |
| `evaluate/` | the `evaluate` subcommand and the synthetic media generator (`synthetic_media`) |

The tests stay flat in `tests/`, one `test_<module>.py` per module. The sources of the interface are in `frontend/`,
and the JSON fixtures that its tests and the server's share are in `tests/fixtures/editor/` (regenerate them after a
deliberate change of the API with `docker compose run --rm -T dev python tests/test_editor_fixtures.py --write`).

### Continuous integration

The GitHub Actions workflow `.github/workflows/ci.yml` runs the same commands as above, in the same containers:

| When | What runs |
| --- | --- |
| push to `main`, `develop`, `release/**`; every pull request; tags `v*` | build of `dev` and `ui-dev`, `pytest`, `npm run check` and `npm test` |
| pull requests; tags `v*` | also `scripts/test-docker.sh` |
| tags `v*`, after both of the above pass | publishes the `runtime` image on Docker Hub (`publish-image` job) |
| tags `v*`, after the image | builds the launcher and creates the GitHub release with it (`release` job) |

On a tag `vX.Y.Z` the workflow also checks that `X.Y.Z` is the `__version__` of the package. The tests with the real
model (`-m ollama`) do not run in CI: they are skipped there because no Ollama is reachable.

**Releasing.** The `publish-image` job builds the `runtime` target and pushes `<IMAGE_NAME>:X.Y.Z`, plus
`<IMAGE_NAME>:latest` unless the version is a pre-release (`X.Y.Z-rc1`). The tags come from
`scripts/ci_image_tags.sh`, which also refuses an unusable name or tag. Set up once, on GitHub (Settings → Secrets and
variables → Actions) and on Docker Hub:

| Where | Name | Value |
| --- | --- | --- |
| Variable | `IMAGE_NAME` | `<namespace>/<image_name>`, lowercase, **without** `docker.io/` (e.g. `yourname/vacation-memories`) |
| Secret | `DOCKERHUB_USERNAME` | your Docker Hub user |
| Secret | `DOCKERHUB_TOKEN` | an access token of that account with read and write permission |

The Docker Hub repository must exist and be public, so that the launcher pulls without a login. The `release` job needs
nothing else: it uses the token of the workflow.

Then the `release` job builds the launcher with `IMAGE_REPO` set to `IMAGE_NAME` and the version of the tag, and creates
the GitHub release `vX.Y.Z` with the files `vacation-memories-X.Y.Z-linux-x86_64` and `….sha256`. The notes of the
release are the section `## [X.Y.Z]` of `CHANGELOG.md`; a version with a hyphen (`0.2.0-rc1`) is marked as a
pre-release. If the release already exists, its notes are replaced and its files are uploaded again.

To release:

1. rename `[Unreleased]` in `CHANGELOG.md` to `[X.Y.Z] - date`, and raise `__version__` to `X.Y.Z`;
2. merge into `main`, tag `vX.Y.Z` and push the tag.

The `tests` job fails right away, before anything is published, if the tag does not match `__version__` or the changelog
has no section with content for the version (`scripts/ci_release_notes.sh`).

### Tests with the real model

The `-m ollama` tests use the real model and synthetic images only. Start Ollama first:

```sh
docker compose up -d ollama
docker compose run --rm -T dev pytest -m ollama
```

They are skipped, with a message, when Ollama does not answer or the model is missing. They allow a frame the
model fails on, because of the instability described in "Troubleshooting"; a run in which every frame fails is
still an error.

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

## Documentation

| File | Contents |
| --- | --- |
| `SPEC.md` | the specification of the program: requirements, selection and planning rules, development plan |
| `UI-SPEC.md` | the specification of the editor: API, plan schema, interface and launcher |
| `CHANGELOG.md` | the changes of every version (it is also the text of the GitHub releases) |
| `frontend/THIRD-PARTY.md` | versions and licenses of the interface's dependencies and fonts |
| `docs/ui-mockup/` | the interface mockup the editor was built from |

## License

MIT, see `LICENSE`.
