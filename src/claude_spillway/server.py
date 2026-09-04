"""uvicornでプロキシサーバーを起動するためのラッパー。"""

from __future__ import annotations

import uvicorn

from .config import Settings
from .proxy_app import create_app


def run_server(settings: Settings) -> None:
    app = create_app(settings)
    uvicorn.run(app, host=settings.listen.host, port=settings.listen.port, log_level=settings.log_level.lower())
