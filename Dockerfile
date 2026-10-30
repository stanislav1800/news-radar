# syntax=docker/dockerfile:1
# Образ Сито: один на веб и worker, команда задаётся в compose.yaml.
# Собирается BuildKit'ом (docker compose build или docker buildx build).

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1
WORKDIR /app

# Сначала только зависимости: слой с torch и остальным пересобирается лишь при смене uv.lock.
# Файлы проекта подключаются на время команды (bind), скачанные пакеты остаются в кэше между сборками
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Затем сам проект: не в режиме редактирования, чтобы в итоговый образ хватило одного .venv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable


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
