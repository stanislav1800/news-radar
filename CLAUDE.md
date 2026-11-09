# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

«Сито» (пакет `sito`) — self-hosted радар новостей на русском: RSS и Telegram → очистка → векторы e5 → склейка
в сюжеты → темы пользователей → лента (FastAPI + Jinja2 + HTMX), RSS тем, LLM по кнопке. Пользователь пишет
по-русски; ответы, комментарии в коде и тексты интерфейса — тоже по-русски. ТЗ и статус этапов — `docs/ТЗ.md`,
поведение при сбоях и стоимость — `docs/САМОПРОВЕРКА.md`, развёртывание — `docs/DEPLOY.md`, правила оформления
интерфейса (кнопки, ссылки, каркас страниц) — `docs/UI.md`.

## Команды

```bash
uv run pytest                                   # все тесты (~30 с); нужна запущенная база
uv run pytest tests/test_llm.py -k merge        # один файл / по имени
uv run ruff check src tests                     # линтер (BLE001: голый except Exception — только с logger.exception)
uv run mypy src tests                           # проверка типов
uv run alembic revision --autogenerate -m "..." # миграция по моделям (ruff правит файл сам)
uv run alembic upgrade head
docker compose up -d --build                    # выкатка: db, migrate, web, worker, backup
docker compose exec -T db psql -U postgres -d sito   # живая база (с хоста — порт 15432, compose.override.yaml)
```

Тесты идут на настоящем PostgreSQL + pgvector: база `<имя из DATABASE_URL>_test` пересоздаётся на прогон,
тест — в откатываемой транзакции на **одном соединении**. Поэтому в коде, который вызывают тесты, нельзя
параллельно (`gather`/`create_task`) обращаться к базе из нескольких задач. Сеть в тестах не нужна:
Telegram, HTTP и LLM подменяются; `tests/conftest.py` обнуляет `llm_api_key`.

## Как работать в этом репозитории

- Проверка — один прогон pytest + ruff + mypy в конце. Без ручных smoke-скриптов, браузера, ожидания логов
  после выкатки и экспериментов на живой базе без спроса (пользователь бережёт лимиты).
- Запросы к LLM стоят денег: пробные вызовы — только с согласия пользователя.
- Секреты только в `.env` (не коммитить, не печатать, не менять без спроса). Данные не удалять без согласия.
- Коммит и выкатка — когда попросили или когда это согласованный шаг плана, и только при зелёных проверках.

## Архитектура

**Слои:** `models/` (SQLAlchemy 2 async) → `repositories/` (запросы, часто сырой SQL через `text()`) →
`services/` (логика) → `web/routes/` и `worker/__main__.py`. Конфиг — `config.py` (pydantic-settings из `.env`),
в вебе доступен как `request.app.state.settings`.

**Конвейер постов** держится на `post.status`: `new → cleaned → embedded → clustered` (или `skipped`/`failed`).
Каждый шаг — `drain_loop` в worker'е: берёт пачку `take_pending` (`FOR UPDATE SKIP LOCKED`), обрабатывает
в одной транзакции, статус меняется в ней же — падение просто откатывает пачку. Ошибка поста — `record_post_error`,
после `MAX_ATTEMPTS` он `failed` (кнопка «Повторить» в админке).

**Worker** — один процесс asyncio с независимыми циклами (`main()` в `worker/__main__.py`): опрос RSS, шаги
конвейера, полный текст статей, подключения-слушатели, пульс, хранение 90 дней, уведомления, дайджесты, проверка
тем и объединение сюжетов через LLM. Медленные циклы не держат быстрые; тяжёлую CPU-работу — в `asyncio.to_thread`.

**Сборщики** (`services/collector/`, реестр — `registry.py`): опрашиваемые (`BaseCollector.fetch`, RSS) и
слушающие (`ConnectorListener.run`, Telegram на Telethon). Слушателями управляет `services/connectors.py`
(запуск, перезапуск, сторож тишины по постам в базе). Всё сохраняется через `IngestService` (он же `PostSink`):
посты + курсор источника в одной транзакции. Веб в Telegram не ходит — оставляет `ConnectorCommand`, worker
выполняет. Секреты подключений зашифрованы `SITO_SECRET_KEY` (Fernet).

**Склейка** (`services/clustering.py`, `repositories/stories.py`): под advisory-блокировкой `CLUSTERING_LOCK_KEY`;
сначала ссылка на статью другого издания (24 ч), потом похожесть векторов в окне ±6 ч. Сводка сюжета
(`_REFRESH_SQL`) пересчитывается целиком по его постам. Пороги подобраны по `eval/` — менять только с замером.
Недосклеенное доводит `StoryMergeService` (LLM).

**Темы** (`services/topics.py`, `repositories/topics.py`): слова (леммы pymorphy3, `utils/lemmas.py`) + похожесть
на 👍-примеры, 👎 вычитает; результат — `topic_match`. LLM-проверка «спорных» — `TopicCheckService`.

**LLM** (`services/llm.py`, routerai.ru, OpenAI-совместимый API, цена в рублях): всё по кнопке или включается
в теме, в фоне по умолчанию только объединение сюжетов. Обязательный порядок: прочитать из базы → `commit`
(отпустить соединение) → вызвать модель → записать; от двойного запуска — `exclusive(kind, key)`; каждый вызов —
`record_usage`, перед вызовом — `check_limit` (токены и рубли, общий и на пользователя). Без `LLM_API_KEY`
всё остальное работает.

**Веб:** страницы Jinja2 (`web/templates/`), частичные обновления HTMX (шаблоны `_*.html`), CSRF-токен в формах,
вход — сессия в cookie. Время в базе — UTC; показывается в часовом поясе пользователя (`user.timezone`).

**Миграции** — Alembic autogenerate по моделям; индексы и частичные условия объявляются в `__table_args__`
моделей, иначе autogenerate предложит их удалить.
