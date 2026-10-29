from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from sito.web.deps import CurrentUser
from sito.web.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: CurrentUser) -> HTMLResponse:
    # Заглушка: здесь будет лента сюжетов (следующий пункт этапа 7)
    return templates.TemplateResponse(request, "index.html", {"user": user})
