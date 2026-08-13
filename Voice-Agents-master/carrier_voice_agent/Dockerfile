# Production image for the LiveKit worker.
# The worker connects OUT to LiveKit Cloud, so no ports need to be exposed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libsndfile backs soundfile, which decodes TTS audio only when a model answers
# with a container instead of the raw PCM the worker asks for. Kept because that
# fallback must not be the thing that breaks a live call.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# LiveKit + OpenRouter + Transport Pro credentials are provided at runtime
# (env / secrets), not baked in.
CMD ["lanevoice-worker", "start"]
