FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv-bin
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY --from=uv-bin /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/call-audit-venv \
    PATH="/opt/call-audit-venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUDIO_STORAGE_PATH=/var/lib/call-audit/audio

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 callaudit \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin callaudit \
    && mkdir -p /var/lib/call-audit/audio \
    && chown -R 10001:10001 /var/lib/call-audit

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra transcription --extra privacy --no-install-project

COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY prompts ./prompts
COPY web ./web
RUN uv sync --frozen --no-dev --extra transcription --extra privacy

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
