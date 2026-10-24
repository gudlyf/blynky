# Build from this directory - the context is the repo itself:
#
#   docker build -t blynky .
#
#   docker run --rm -p 127.0.0.1:8080:8080 -v ./data:/data \
#     -e OLLAMA_URL=http://host.docker.internal:11434/v1 blynky
#
# blinkpy is fetched here rather than copied from ../blinkpy, so the build
# does not depend on a sibling checkout. See patches/README.md.
#
# Settings come from the environment (-e / --env-file); .env and the cached
# credentials are deliberately kept out of the image.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The OAuth v2 login this depends on is on blinkpy's dev branch and has never
# been released to PyPI, so this tracks a commit rather than a version. Pinned,
# not floating: dev moves, and an unpinned build would change underneath you.
ARG BLINKPY_REPO=https://github.com/fronzbot/blinkpy.git
ARG BLINKPY_REF=4d0c274f8e20bdf268949eb2d756bf527dc0ae7e

# Fetching the one commit rather than cloning the history; GitHub serves a
# bare SHA to fetch, so no branch name is needed and the pin is exact.
RUN git init -q /src/blinkpy \
    && git -C /src/blinkpy remote add origin "$BLINKPY_REPO" \
    && git -C /src/blinkpy fetch -q --depth 1 origin "$BLINKPY_REF" \
    && git -C /src/blinkpy checkout -q FETCH_HEAD

# Local fixes that are not upstream. git apply exits non-zero if a patch does
# not apply, so bumping BLINKPY_REF past a conflicting change fails the build
# here rather than silently dropping the fix.
COPY patches /src/patches
RUN git -C /src/blinkpy apply --verbose /src/patches/*.patch

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
USER blynky
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/api/state\", timeout=4)"

CMD ["python", "run.py"]
