from pathlib import Path

from fastapi.templating import Jinja2Templates

from sito.web.formatting import ago, local_time, plural, safe_url
from sito.web.security import CSRF_FORM_FIELD, CSRF_HEADER, csrf_token

WEB_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(
    csrf_token=csrf_token,
    CSRF_FORM_FIELD=CSRF_FORM_FIELD,
    CSRF_HEADER=CSRF_HEADER,
)
templates.env.filters.update(local_time=local_time, ago=ago, plural=plural, safe_url=safe_url)
