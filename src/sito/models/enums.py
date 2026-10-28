from enum import StrEnum


class SourceType(StrEnum):
    RSS = "rss"
    TELEGRAM = "telegram"


class PostStatus(StrEnum):
    NEW = "new"              # шаг 1–2: собран, сохранён
    CLEANED = "cleaned"      # шаг 3: текст очищен
    EMBEDDED = "embedded"    # шаг 4: есть вектор
    CLUSTERED = "clustered"  # шаг 5: в сюжете, обработка закончена
    SKIPPED = "skipped"      # шаг 3: пустой пост, реклама, опрос
    FAILED = "failed"        # попытки кончились, ждёт разбора
