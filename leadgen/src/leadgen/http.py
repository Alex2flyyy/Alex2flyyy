"""Shared async HTTP client.

One place that knows about timeouts, retries, redirects, user agents, and rate
limiting. Every outbound request in the system goes through :class:`HttpFetcher`
so politeness settings are impossible to bypass by accident.

Retry policy is deliberate: retry transport errors and 429/5xx with exponential
backoff plus jitter, never retry 4xx (a 404 will still be a 404), and cap total
attempts low. A nightly batch that retries aggressively turns one slow host
into an hour of wall clock.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from leadgen.config import get_settings
from leadgen.logging import get_logger
from leadgen.ratelimit import HostRateLimiter

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int | None
    text: str = ""
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    error_kind: str | None = None
    ssl_expires_at: datetime | None = None
    ssl_valid: bool = False
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and self.status_code < 400

    @property
    def content_length_kb(self) -> float:
        return round(len(self.content) / 1024, 1)


def classify_error(exc: Exception) -> str:
    """Map a transport exception to a stable, reportable category.

    The category ends up on the lead as "why we couldn't reach their site",
    and a DNS failure is a very different sales conversation from an expired
    certificate — so the distinction is worth preserving.
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.PoolTimeout):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_error"
    if isinstance(exc, httpx.ConnectError):
        message = str(exc).lower()
        if "name or service not known" in message or "nodename nor servname" in message:
            return "dns_failure"
        if "certificate" in message or "ssl" in message:
            return "tls_error"
        if "refused" in message:
            return "connection_refused"
        return "connection_refused"
    if isinstance(exc, httpx.TooManyRedirects):
        return "http_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return "error"


class HttpFetcher:
    """Rate-limited, retrying HTTP client.

    Use as an async context manager so connections are always closed:

        async with HttpFetcher() as fetcher:
            result = await fetcher.get("https://example.com")
    """

    def __init__(
        self,
        *,
        timeout: float | None = None,
        max_attempts: int = 3,
        verify_ssl: bool = True,
        follow_redirects: bool = True,
        limiter: HostRateLimiter | None = None,
        user_agent: str | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.timeout = timeout or float(settings.audit_timeout_seconds)
        self.max_attempts = max_attempts
        self.follow_redirects = follow_redirects
        self.user_agent = user_agent or settings.user_agent
        self.limiter = limiter or HostRateLimiter(settings.per_host_rps, settings.global_rps)

        # HTTP/2 needs the optional `h2` package. It is declared in
        # pyproject (`httpx[http2]`), but a partial or system install can
        # still be missing it — degrade to HTTP/1.1 rather than refusing to
        # start, since nothing here depends on HTTP/2.
        try:
            import h2  # noqa: F401

            http2 = True
        except ImportError:
            http2 = False
            log.debug("http.http2_unavailable", hint="pip install 'httpx[http2]' to enable")

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            follow_redirects=follow_redirects,
            max_redirects=6,
            verify=verify_ssl,
            http2=http2,
            headers={**DEFAULT_HEADERS, "User-Agent": self.user_agent},
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        rate_limit: bool = True,
        max_bytes: int = 5_000_000,
    ) -> FetchResult:
        return await self._request(
            "GET", url, headers=headers, params=params, rate_limit=rate_limit, max_bytes=max_bytes
        )

    async def head(self, url: str, *, rate_limit: bool = True) -> FetchResult:
        return await self._request("HEAD", url, rate_limit=rate_limit, max_bytes=0)

    async def post_json(
        self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> FetchResult:
        return await self._request("POST", url, json=payload, headers=headers)

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Form-encoded POST. Overpass and several older APIs require it."""
        return await self._request("POST", url, data=data, headers=headers, timeout=timeout)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        rate_limit: bool = True,
        max_bytes: int = 5_000_000,
        timeout: float | None = None,
    ) -> FetchResult:
        host = urlparse(url).netloc or url
        last_error: Exception | None = None
        result = FetchResult(url=url, final_url=url, status_code=None)

        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            if rate_limit:
                await self.limiter.acquire(host)

            started = asyncio.get_event_loop().time()
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
                elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)

                content = b""
                text = ""
                if method != "HEAD" and max_bytes > 0:
                    content = response.content[:max_bytes]
                    try:
                        text = content.decode(response.encoding or "utf-8", errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        text = content.decode("utf-8", errors="replace")

                result = FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    text=text,
                    content=content,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    redirect_chain=[str(r.url) for r in response.history],
                    elapsed_ms=elapsed_ms,
                    attempts=attempt,
                    ssl_valid=str(response.url).startswith("https://"),
                )

                if response.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                    await self._backoff(attempt, response.headers.get("retry-after"))
                    continue
                return result

            except Exception as exc:  # noqa: BLE001 - categorized below
                last_error = exc
                kind = classify_error(exc)
                # DNS and refused connections are stable facts about the host,
                # not transient blips. Retrying them wastes the whole timeout
                # budget on a site that is simply gone — which is itself a
                # useful finding for a lead.
                if kind in {"dns_failure", "tls_error"} or attempt >= self.max_attempts:
                    return FetchResult(
                        url=url,
                        final_url=url,
                        status_code=None,
                        error=str(exc)[:500],
                        error_kind=kind,
                        attempts=attempt,
                    )
                await self._backoff(attempt, None)

        return FetchResult(
            url=url,
            final_url=url,
            status_code=None,
            error=str(last_error)[:500] if last_error else "unknown error",
            error_kind=classify_error(last_error) if last_error else "error",
            attempts=self.max_attempts,
        )

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                # Cap server-suggested waits: a site telling us to come back in
                # an hour should be skipped, not waited on.
                await asyncio.sleep(min(float(retry_after), 10.0))
                return
            except (TypeError, ValueError):
                pass
        delay = min(2 ** (attempt - 1), 8) * (0.5 + random.random())
        await asyncio.sleep(delay)


async def probe_ssl(hostname: str, port: int = 443, timeout: float = 8.0) -> dict[str, Any]:
    """Inspect the TLS certificate directly.

    httpx will happily complete a request without telling you the certificate
    expires in nine days. Expiry is a genuine sales hook ("your certificate
    lapses next month and browsers will warn visitors"), so we open a raw
    socket to read it.
    """
    out: dict[str, Any] = {
        "valid": False, "expires_at": None, "days_remaining": None,
        "issuer": None, "error": None,
    }
    context = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, port, ssl=context, server_hostname=hostname),
            timeout=timeout,
        )
    except ssl.SSLCertVerificationError as exc:
        out["error"] = f"certificate verification failed: {exc.verify_message or exc}"
        return out
    except (OSError, asyncio.TimeoutError) as exc:
        out["error"] = str(exc)[:200]
        return out

    try:
        sslobj = writer.get_extra_info("ssl_object")
        cert = sslobj.getpeercert() if sslobj else None
        if cert:
            out["valid"] = True
            not_after = cert.get("notAfter")
            if not_after:
                expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                out["expires_at"] = expires
                out["days_remaining"] = (expires - datetime.utcnow()).days
            issuer = dict(x[0] for x in cert.get("issuer", ()))
            out["issuer"] = issuer.get("organizationName")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
        _ = reader
    return out
