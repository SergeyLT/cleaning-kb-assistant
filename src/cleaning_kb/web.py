"""HTML-оболочка панели; данные доступны через защищённые сервисы/API."""

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cleaning_kb.auth import COOKIE_NAME, AuthService
from cleaning_kb.errors import AppError

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def mount_ui(app: FastAPI, get_auth: Callable[[Request], AuthService]) -> None:
    """Ресурсы ищутся относительно установленного пакета, а не рабочего каталога."""
    directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=directory / "templates")
    app.mount("/static", StaticFiles(directory=directory / "static"), name="static")

    def signed_in(request: Request) -> bool:
        try:
            get_auth(request).session(request.cookies.get(COOKIE_NAME))
            return True
        except AppError as error:
            if error.status != 401:
                raise
            return False

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="app.html" if signed_in(request) else "login.html",
            headers={"Content-Security-Policy": CSP},
        )

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request) -> Response:
        if signed_in(request):
            return RedirectResponse("/")
        return templates.TemplateResponse(
            request=request, name="login.html", headers={"Content-Security-Policy": CSP}
        )
