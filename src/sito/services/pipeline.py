"""Общее для шагов пайплайна, которые обрабатывают посты пачками."""

from sito.models import Post, PostStatus

MAX_ATTEMPTS = 3
ERROR_MAX_LENGTH = 2000


def record_post_error(post: Post, exc: Exception) -> None:
    """Засчитать неудачную попытку. После MAX_ATTEMPTS пост больше не берётся (failed)."""
    post.attempt += 1
    post.last_error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX_LENGTH]
    if post.attempt >= MAX_ATTEMPTS:
        post.status = PostStatus.FAILED


def mark_post_done(post: Post, status: PostStatus) -> None:
    """Шаг прошёл: следующий шаг начинает счёт попыток заново."""
    post.status = status
    post.attempt = 0
    post.last_error = None
