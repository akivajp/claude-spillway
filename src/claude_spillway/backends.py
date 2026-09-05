"""HTTP forwarding to Anthropic and Ollama Cloud.

Anthropic / Ollama Cloud への実際のHTTP転送を担当するモジュール。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from .config import Settings
from .quota import QuotaSnapshot, parse_quota_headers

# Hop-by-hop headers that must be dropped when relaying a response verbatim.
# content-length is always dropped as well because the body may be rewritten
# (e.g. the model name), so the downstream ASGI server recomputes it.
#
# レスポンスをそのまま転送する際に落とすべきホップバイホップ系ヘッダー。
# content-length は本文を書き換える(モデル名変更等)可能性があるため常に落とし、
# 下流のASGIサーバーに再計算させる。
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


#: Endpoint Claude Code reads for its own ``/usage`` command. Consumes no quota.
#: Claude Codeが ``/usage`` で参照しているエンドポイント。quotaを消費しない。
OAUTH_USAGE_PATH = "/api/oauth/usage"

#: Beta opt-in the usage endpoint requires alongside the OAuth bearer token.
#: 使用量エンドポイントがOAuthのBearerトークンと併せて要求するbeta指定。
OAUTH_BETA_HEADER = "oauth-2025-04-20"


def _filter_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


@dataclass
class OllamaStats:
    """Self-tracked counters standing in for an Ollama quota API.

    Ollama Cloud has no official quota API (as of 2026-09), so this proxy
    tracks the outcome of the requests it relayed instead.

    Ollama Cloud側は公式のquota取得APIが存在しない(2026-09時点)ため、
    このプロキシが中継したリクエストの成否をセルフトラッキングして代用する。
    """

    requests_sent: int = 0
    requests_failed: int = 0
    last_request_at: float | None = None
    last_status_code: int | None = None
    last_error: str | None = None

    def record_success(self, status_code: int) -> None:
        self.requests_sent += 1
        self.last_status_code = status_code
        self.last_request_at = time.time()
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.requests_sent += 1
        self.requests_failed += 1
        self.last_request_at = time.time()
        self.last_error = error


class ProxyBackends:
    """Holds the HTTP clients for both Anthropic and Ollama Cloud.

    AnthropicとOllama Cloud双方へのHTTPクライアントをまとめて保持するクラス。
    """

    def __init__(
        self,
        settings: Settings,
        anthropic_transport: httpx.AsyncBaseTransport | None = None,
        ollama_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # The transports are injectable so tests can swap in httpx.MockTransport.
        # トランスポートを差し替え可能にして、テストから httpx.MockTransport を注入できるようにしている。
        self._settings = settings
        self.ollama_stats = OllamaStats()
        self._anthropic_client = httpx.AsyncClient(
            base_url=settings.anthropic.base_url,
            timeout=120.0,
            transport=anthropic_transport,
        )
        self._ollama_client = httpx.AsyncClient(
            base_url=settings.ollama.base_url,
            timeout=120.0,
            transport=ollama_transport,
        )

    async def aclose(self) -> None:
        await self._anthropic_client.aclose()
        await self._ollama_client.aclose()

    async def forward_to_anthropic(
        self, request: Request, body: bytes
    ) -> tuple[httpx.Response, QuotaSnapshot | None]:
        """Relay the request to Anthropic unchanged.

        The auth header (x-api-key, or the OAuth bearer token) is simply
        passed through from Claude Code, so this proxy never holds any
        Anthropic credentials of its own.

        リクエストをそのままAnthropicへ転送する。
        認証ヘッダー(x-api-keyまたはOAuthのBearerトークン)はClaude Codeが
        送ってきたものをそのまま横流しするだけなので、このプロキシ自体は
        Anthropicの認証情報を一切保持しない。
        """
        upstream_request = self._anthropic_client.build_request(
            method=request.method,
            url=request.url.path,
            params=request.url.query.encode("utf-8"),
            headers=_filter_headers(request.headers),
            content=body,
        )
        response = await self._anthropic_client.send(upstream_request, stream=True)
        snapshot = parse_quota_headers(response.headers)
        return response, snapshot

    async def fetch_oauth_usage(self, authorization: str) -> httpx.Response:
        """Read the account's quota from the OAuth usage endpoint.

        This is the endpoint Claude Code itself reads for ``/usage``. It is a
        plain read whose response carries no rate-limit headers, so unlike
        :meth:`probe_anthropic` it consumes no quota at all. It is not part of
        the published API, so callers must treat any non-200 as "no data".

        OAuthの使用量エンドポイントからアカウントのquotaを読み取る。
        Claude Code自身が ``/usage`` で参照しているものと同じ。単なる読み取りで
        レスポンスにレート制限ヘッダーが付かない = :meth:`probe_anthropic` と
        違いquotaを一切消費しない。公開APIではないため、呼び出し側は200以外を
        「データなし」として扱うこと。
        """
        return await self._anthropic_client.get(
            OAUTH_USAGE_PATH,
            headers={
                "authorization": authorization,
                "anthropic-beta": OAUTH_BETA_HEADER,
                "accept": "application/json",
            },
        )

    async def probe_anthropic(self, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
        """Send the lightweight recovery-check request to Anthropic while in fallback.

        Costs a small amount of quota, so it is only used when
        :meth:`fetch_oauth_usage` is unavailable.

        フォールバック中に、回復確認のための軽量リクエストをAnthropicへ送る。
        わずかにquotaを消費するため、:meth:`fetch_oauth_usage` が使えない場合の
        フォールバックとしてのみ使用する。
        """
        return await self._anthropic_client.post("/v1/messages", headers=headers, json=payload)

    async def forward_to_ollama(
        self, request: Request, body: bytes, rewritten_model: str | None
    ) -> httpx.Response:
        """Relay ``POST /v1/messages`` — and only that — to Ollama Cloud.

        Because of a known compatibility bug (ollama/ollama#13949: hitting an
        unsupported endpoint hangs and affects subsequent requests), callers
        must never use this method for anything but ``/v1/messages``.

        ``POST /v1/messages`` のみをOllama Cloudへ転送する。
        既知の互換性バグ(ollama/ollama#13949: 未対応エンドポイントを叩くと
        ハングして後続リクエストにも影響する)を踏まえ、呼び出し側で
        必ずこのメソッドを ``/v1/messages`` 以外には使わないこと。
        """
        payload = body
        if rewritten_model is not None:
            data = json.loads(body)
            data["model"] = rewritten_model
            payload = json.dumps(data).encode("utf-8")

        headers = _filter_headers(request.headers)
        # Ollama Cloud's Anthropic-compatible endpoint rejects x-api-key and
        # only accepts Authorization: Bearer (ollama/ollama#16922).
        # Ollama CloudのAnthropic互換エンドポイントは x-api-key を受け付けず、
        # Authorization: Bearer のみ有効(ollama/ollama#16922)。
        headers.pop("x-api-key", None)
        headers["authorization"] = f"Bearer {self._settings.ollama.api_key}"

        upstream_request = self._ollama_client.build_request(
            method=request.method,
            url=request.url.path,
            params=request.url.query.encode("utf-8"),
            headers=headers,
            content=payload,
        )
        try:
            response = await self._ollama_client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            self.ollama_stats.record_failure(str(exc))
            raise
        if response.status_code >= 400:
            self.ollama_stats.record_failure(f"HTTP {response.status_code}")
        else:
            self.ollama_stats.record_success(response.status_code)
        return response


async def _stream_response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    # A real network response is a lazy stream, but some transports
    # (httpx.MockTransport as used in the tests, for one) read the response
    # eagerly and leave is_stream_consumed=True. In that case aiter_raw()
    # would raise StreamConsumed, so the already-read content is yielded as-is.
    #
    # 実ネットワーク越しの応答は遅延ストリームだが、一部のトランスポート
    # (テストで使う httpx.MockTransport 等)は応答をその場で全読み込みして
    # is_stream_consumed=True にしてしまう。その場合は aiter_raw() が
    # StreamConsumed を送出するため、既に読み込み済みの内容をそのまま返す。
    if response.is_stream_consumed:
        yield response.content
    else:
        async for chunk in response.aiter_raw():
            yield chunk
    await response.aclose()


def to_streaming_response(response: httpx.Response) -> StreamingResponse:
    """Convert an httpx response into a FastAPI response, still streaming.

    Claude Code relies on SSE streaming, so the body is passed downstream
    chunk by chunk instead of being buffered in full.

    httpxのレスポンスをストリーミングしたままFastAPIのレスポンスへ変換する。
    Claude CodeはSSEでのストリーミング応答を使うため、全文をバッファせず
    そのままチャンク単位で下流に流す。
    """
    return StreamingResponse(
        _stream_response_body(response),
        status_code=response.status_code,
        headers=_filter_headers(response.headers),
        media_type=response.headers.get("content-type"),
    )
