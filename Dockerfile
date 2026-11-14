# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:24.20.0-bookworm-slim

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

# The interface (UI-SPEC.md, section 4): React and TypeScript bundled with esbuild. Node lives only in these stages.
FROM ${NODE_IMAGE} AS frontend-deps
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM frontend-deps AS frontend-build
COPY frontend/ ./
RUN npm run build

# `ui-dev`: the dependencies sit in /node_modules, outside the mounted repository (/src), where Node, tsc and
# esbuild still find them walking up from /src/frontend.
FROM ${NODE_IMAGE} AS ui-dev
COPY frontend/package.json frontend/package-lock.json /deps/
RUN cd /deps && npm ci --ignore-scripts --no-audit --no-fund && mv node_modules /node_modules && cd / && rm -rf /deps
ENV HOME=/tmp
USER node
WORKDIR /src/frontend

FROM base AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="vacation-memories" \
      org.opencontainers.image.description="Creates a memory video from the photos and clips of a vacation, fully local" \
      org.opencontainers.image.version="${APP_VERSION}"
COPY --from=builder /opt/venv /opt/venv
# The image installs nothing at runtime: no package manager.
RUN rm -rf /opt/venv/bin/pip* /opt/venv/lib/python*/site-packages/pip /opt/venv/lib/python*/site-packages/pip-*
COPY vacation_memories /app/vacation_memories
COPY --from=frontend-build /frontend/dist /app/vacation_memories/editor/static
ENV PYTHONPATH=/app \
    IMAGE_VERSION=${APP_VERSION}
USER app
ENTRYPOINT ["python", "-m", "vacation_memories"]
