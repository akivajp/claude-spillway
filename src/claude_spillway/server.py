"""Thin wrapper that boots the proxy with uvicorn.

uvicornでプロキシサーバーを起動するためのラッパー。
"""

from __future__ import annotations

import uvicorn

from .config import Settings
from .proxy_app import create_app


def run_server(settings: Settings) -> None:
    """Build the ASGI app and serve it on the configured host/port.

    ASGIアプリを構築し、設定されたホスト・ポートで待ち受ける。
    """
    app = create_app(settings)
    uvicorn.run(app, host=settings.listen.host, port=settings.listen.port, log_level=settings.log_level.lower())
