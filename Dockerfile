# Образ Сито: один на веб и worker, команда задаётся в compose.yaml.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Сначала только зависимости: этот слой (torch и остальное) пересобирается лишь при смене uv.lock.
# Без RUN --mount: так образ собирается и старым сборщиком Docker, без BuildKit
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache


FROM python:3.13-slim
RUN useradd --create-home --uid 1000 sito
WORKDIR /app
COPY --from=build --chown=sito:sito /app/.venv /app/.venv
# Миграции и их настройки ([tool.alembic] в pyproject.toml): alembic upgrade head запускается в контейнере
COPY --chown=sito:sito pyproject.toml ./
COPY --chown=sito:sito migrations ./migrations

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/sito/.cache/huggingface \
    WEB_HOST=0.0.0.0
USER sito
# Каталог для модели эмбеддингов: в compose на него монтируется том, чтобы не качать модель заново
RUN mkdir -p $HF_HOME

EXPOSE 8000
CMD ["sito", "web"]
