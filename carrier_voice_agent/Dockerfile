# Production image for the LiveKit worker.
# The worker connects OUT to LiveKit Cloud, so no ports need to be exposed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libsndfile is required by soundfile (decoding Groq TTS audio).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# LiveKit + Groq credentials are provided at runtime (env / secrets), not baked in.
CMD ["lanevoice-worker", "start"]
