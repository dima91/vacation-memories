# Vacation Memories

Creates a memory video from the photos and clips of a vacation. Everything runs locally, in Docker containers.

> This README is incomplete: installation, usage and the other sections are added in Step 6. Only the sections
> below exist so far.

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
