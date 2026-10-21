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

# Application code.
COPY edgar_dashboard.py xbrl_extract.py verify_facts.py app.py charts.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /app/financial_graphs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["python", "app.py"]
