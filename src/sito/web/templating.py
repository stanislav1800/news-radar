import hashlib
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from sito.web.formatting import ago, compact, local_time, plural, report_html, safe_url
from sito.web.security import CSRF_FORM_FIELD, CSRF_HEADER, csrf_token, pop_flash

WEB_DIR = Path(__file__).parent


def static_version(path: str) -> str:
    """Короткий хеш содержимого файла статики: меняется вместе с файлом.

    Добавляется к адресу (?v=...), поэтому после обновления Сито браузер скачивает новые стили,
    а не показывает старые из своего кэша. Пересчитывается, только когда файл изменился (веб в Docker
    берёт статику прямо из папки проекта — правка видна без перезапуска).
    """
    file = WEB_DIR / "static" / path
    return _content_hash(file, file.stat().st_mtime_ns)


@cache
def _content_hash(file: Path, mtime_ns: int) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()[:10]


templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(
    static_version=static_version,
    csrf_token=csrf_token,
    pop_flash=pop_flash,
    CSRF_FORM_FIELD=CSRF_FORM_FIELD,
    CSRF_HEADER=CSRF_HEADER,
)
templates.env.filters.update(local_time=local_time, ago=ago, plural=plural, safe_url=safe_url, compact=compact,
                            report_html=report_html)
