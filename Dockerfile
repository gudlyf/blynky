# Build from this directory - the context is the repo itself:
#
#   docker build -t blynky .
#
#   docker run --rm -p 127.0.0.1:8080:8080 -v ./data:/data \
#     -e OLLAMA_URL=http://host.docker.internal:11434/v1 blynky
#
# blinkpy is fetched here rather than copied from ../blinkpy, so the build
# does not depend on a sibling checkout. See docs/guide.md, "Where blinkpy
# comes from".
#
# Settings come from the environment (-e / --env-file); .env and the cached
# credentials are deliberately kept out of the image.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*


# Records the short commit this is built from, for the settings menu and bug
# reports; the version itself comes from pyproject.toml. Its own stage so
# that a new commit, which changes .git, rebuilds only this and the one COPY
# that uses it. The [t] makes .git optional: a tarball with no history still
# builds, with no commit.
FROM base AS version
COPY .gi[t] /src/blynky.git
RUN { GIT_DIR=/src/blynky.git git rev-parse --short HEAD 2>/dev/null || true; } > /BUILD


FROM base AS build

# Fetched from gudlyf/blinkpy, a fork of fronzbot/blinkpy, so the build
# survives upstream disappearing. The pin is a commit on the fork's blynky
# branch: upstream dev (which has fixes no blinkpy release does yet) plus
# Blynky's own fixes as commits on top. Pinned, not floating, and the SHA is
# what makes the fork safe to depend on: git verifies the content against it,
# so nothing can be swapped in under a pinned commit.
ARG BLINKPY_REPO=https://github.com/gudlyf/blinkpy.git
ARG BLINKPY_REF=4d61f0d35f620847054fa2d26ee59e64be5a7d30

# Fetching the one commit rather than cloning the history; GitHub serves a
# bare SHA to fetch, so no branch name is needed and the pin is exact.
RUN git init -q /src/blinkpy \
    && git -C /src/blinkpy remote add origin "$BLINKPY_REPO" \
    && git -C /src/blinkpy fetch -q --depth 1 origin "$BLINKPY_REF" \
    && git -C /src/blinkpy checkout -q FETCH_HEAD

WORKDIR /src/blynky

# uv resolves blinkpy from ../blinkpy, which is why the clone above lands at
# /src/blinkpy: it makes the path in pyproject.toml true inside the image too.
# Dependencies are keyed on the manifests alone, so editing app code does not
# invalidate the install. uv.lock is gitignored, hence the optional glob.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY run.py ./
COPY blynky ./blynky
# Maintenance scripts, run with `docker compose run` against the same /data.
COPY scripts ./scripts
RUN uv sync --no-dev


FROM python:3.13-slim-bookworm

# ffmpeg samples frames out of each clip; without it the app silently falls
# back to the single snapshot, which is a much worse description.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Same path as the build stage - the venv records absolute paths.
COPY --from=build /src/blynky /src/blynky

ENV PATH="/src/blynky/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data \
    CREDFILE=/data/.blink_creds.json

# Clips, stills and the auth token all live here: mount it or the container
# has to sign in again on every start, and the archive starts over empty.
# aiohttp also needs DATA_DIR to exist before it will serve /media/.
RUN useradd --create-home --uid 10001 blynky \
    && mkdir -p /data/latest /data/events /data/stills \
    && chown -R blynky:blynky /data
VOLUME /data

WORKDIR /src/blynky
# Last, because it changes with every commit.
COPY --from=version /BUILD /src/blynky/blynky/BUILD

USER blynky
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/api/state\", timeout=4)"

CMD ["python", "run.py"]
