# syntax=docker/dockerfile:1

# The image carries code and the embedding model. It carries no data: the corpus and
# the vector store are uploaded to the Railway volume once, by hand, and found there
# via SBPEYE_DATA_DIR (deployment plan, 5.1).

# --------------------------------------------------------------- frontend build
FROM node:20-slim AS frontend

WORKDIR /build/frontend
# Manifest first so `npm ci` is cached against dependency changes rather than every
# source edit.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# vite writes to `../src/sbpeye/static/spa` (see vite.config.ts), so the bundle lands
# at /build/src/sbpeye/static/spa — already in the layout the app serves it from.
RUN npm run build

# --------------------------------------------------------------- python runtime
FROM python:3.12-slim AS runtime

# libgomp1 is required by onnxruntime, which fastembed imports on the hot path; without
# it the first embedding call fails at import with a missing libgomp.so.1.
#
# No opencv libraries: `docling` is an optional extra and this image builds without it,
# which drops opencv along with torch and the CUDA runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies before source, so editing a .py file does not re-resolve the whole
# wheel set. README.md is here because pyproject's `readme` field points at it.
#
# No `--extra checklist`: that extra pulls docling, and with it torch, transformers,
# opencv and the CUDA runtime — 4.9 GB of GPU tooling on a target with no GPU. The
# application starts and serves without it; only checklist generation is unavailable,
# and it says so (`checklist.DoclingUnavailable`).
COPY pyproject.toml uv.lock README.md ./
# The cache is deleted inside the same RUN. Left behind it is a second copy of every
# wheel — 5.5 GB on this dependency set — baked into the layer, because a later `rm`
# in its own RUN removes the files from the filesystem but not from the layer below.
RUN uv sync --frozen --no-dev --no-install-project && rm -rf /root/.cache/uv

COPY src/ ./src/
COPY run.py ./
COPY --from=frontend /build/src/sbpeye/static/spa ./src/sbpeye/static/spa
RUN uv sync --frozen --no-dev && rm -rf /root/.cache/uv

ENV PATH="/app/.venv/bin:$PATH"

# Bake the embedding weights in rather than downloading them on first boot. They are
# ~230 MB of third-party ONNX, they are not application data, and fetching them at
# runtime puts a cold-start download in front of the first search. This must stay in
# step with EMBEDDING_MODEL: a mismatch against the model the shipped Chroma index was
# built with returns nonsense rather than an error (deployment plan, 9.3).
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN python -c "\
from fastembed import TextEmbedding; \
TextEmbedding(model_name='BAAI/bge-base-en-v1.5', cache_dir='/opt/models')"

# Where mutable data lives. Set as a default so a plain `docker run -v ...:/data` behaves
# the same way Railway does; Railway sets it explicitly too.
#
# Deliberately no `VOLUME` instruction: Railway rejects the build outright with "docker
# VOLUME is not supported, use Railway Volumes". It bought nothing anyway — a bind mount
# or a Railway volume attaches to this path regardless, and declaring it only changes
# what an *undeclared* `docker run` does with writes to it.
ENV SBPEYE_DATA_DIR=/data

# run.py reads $PORT and falls back to 8000. Railway injects the port it routes to.
EXPOSE 8000
CMD ["python", "run.py"]
