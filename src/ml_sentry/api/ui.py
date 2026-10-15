"""Serve the minimal reviewer UI (static files bundled in the package)."""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

router = APIRouter(include_in_schema=False)

_TYPES = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _file(name: str) -> Response:
    body = files("ml_sentry.ui").joinpath(name).read_bytes()
    return Response(body, media_type=_TYPES[name], headers=_HEADERS)


@router.get("/ui")
def ui_redirect() -> RedirectResponse:
    return RedirectResponse("/ui/")


@router.get("/ui/")
def ui_index() -> Response:
    return _file("index.html")


@router.get("/ui/app.js")
def ui_js() -> Response:
    return _file("app.js")


@router.get("/ui/app.css")
def ui_css() -> Response:
    return _file("app.css")
