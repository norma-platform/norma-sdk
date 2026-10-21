# Modified for Norma.
"""Authenticated asynchronous HTTP transport for hosted Norma."""

from types import TracebackType
from typing import Any, cast

import httpx

from .configuration import resolve_agent_key, resolve_url
from .errors import raise_for_norma_status
from .validation import ensure_agent_name


class NormaClient:
    """Connect using NORMA_RUNTIME_URL and NORMA_AGENT_KEY or explicit arguments.

    Redirects and requests outside the configured API prefix are rejected before
    credentials can leave the configured endpoint. Requests are never retried.
    Agent identity is supplied separately to registration and check methods.
    """

    def __init__(
        self,
        runtime_url: str | None = None,
        *,
        agent_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = resolve_url(runtime_url)
        self.timeout = timeout
        self._agent_key = resolve_agent_key(agent_key)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _guard_request(self, request: httpx.Request) -> None:
        if "Authorization" in request.headers or "Cookie" in request.headers:
            raise ValueError("Norma requests must use only the configured Agent Key credential.")
        base = httpx.URL(self.base_url)
        prefix = base.path.rstrip("/")
        if (request.url.scheme, request.url.host, request.url.port) != (
            base.scheme,
            base.host,
            base.port,
        ) or (prefix and not request.url.path.startswith(prefix + "/")):
            raise ValueError("Norma requests must remain within the configured runtime URL.")
        request.headers["X-API-Key"] = self._agent_key

    async def _guard_response(self, response: httpx.Response) -> None:
        if 300 <= response.status_code < 400:
            raise httpx.HTTPStatusError(
                "Norma does not follow redirects.", request=response.request, response=response
            )
        if response.is_error:
            await response.aread()
            raise_for_norma_status(response)

    async def __aenter__(self) -> "NormaClient":
        if self._client is not None:
            raise RuntimeError("NormaClient is already open")
        self._client = httpx.AsyncClient(
            base_url=self.base_url + "/",
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard_request], "response": [self._guard_response]},
            headers={"X-Norma-SDK": "python"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The scoped transport, available inside the asynchronous context."""
        if self._client is None:
            raise RuntimeError("Use NormaClient as an async context manager")
        return self._client

    async def check_connection(self, agent_name: str) -> dict[str, Any]:
        """Read an authorized agent to verify scoped access through the hosted gateway."""
        response = await self.http_client.get(f"/api/v1/agents/{ensure_agent_name(agent_name)}")
        return cast(dict[str, Any], response.json())

    async def _request_runtime_resource(
        self,
        method: str,
        path: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        if (target_type is None) != (target_id is None):
            raise ValueError("target_type and target_id must be supplied together")
        return await self.http_client.request(method, path, **kwargs)

    async def post_runtime_checks(
        self,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> httpx.Response:
        return await self._request_runtime_resource(
            "POST",
            "/api/v1/checks",
            json=json,
            headers=headers,
            target_type=target_type,
            target_id=target_id,
        )
