# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /app

# Dependencies first so the layer is cached across code changes.
# The BuildKit cache mount keeps downloaded wheels between rebuilds, even
# when the requirements layer itself is invalidated.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Application code (glob: every root module, so new ones cannot be missed).
COPY VERSION readers.json *.py ./
COPY templates ./templates
COPY static ./static

# The deployed commit (.git is not in the build context); shown by /healthz,
# Options > Data and Home's download. Optional: VERSION already names the release.
ARG STOCKY_COMMIT=""
ENV STOCKY_COMMIT=${STOCKY_COMMIT}

RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["waitress-serve", "--listen=0.0.0.0:8000", "app:app"]
