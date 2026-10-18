# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    HOME=/work \
    XDG_CACHE_HOME=/work/.cache \
    USER=app \
    LOGNAME=app \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# Keep this list aligned with the filters checked by `doctor` (zscale and tonemap are optional there).
RUN ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264 \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -qE '^ A[^ ]* +aac ' \
    && for f in xfade zoompan fps scale boxblur overlay format setsar afade acrossfade anullsrc; do \
         ffmpeg -hide_banner -filters 2>/dev/null | grep -qE "^ [A-Z.]+ +$f " || { echo "missing FFmpeg filter: $f" >&2; exit 1; }; \
       done
RUN useradd --uid 10001 --no-create-home --home-dir /work --shell /usr/sbin/nologin app \
    && mkdir /work \
    && chown app:app /work
WORKDIR /

FROM base AS builder
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && pip install --no-cache-dir -r /tmp/requirements.txt

FROM builder AS dev
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
USER app
WORKDIR /src

FROM base AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="vacation-memories" \
      org.opencontainers.image.version="${APP_VERSION}"
COPY --from=builder /opt/venv /opt/venv
COPY vacation_memories /app/vacation_memories
ENV PYTHONPATH=/app
USER app
ENTRYPOINT ["python", "-m", "vacation_memories"]
