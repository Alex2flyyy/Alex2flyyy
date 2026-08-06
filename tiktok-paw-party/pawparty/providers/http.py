"""Shared HTTP helpers for network-backed providers.

Centralises the three things every AI API integration needs and every ad-hoc
integration gets wrong: bounded polling, honest timeouts, and streamed
downloads that don't hold a 40MB video in RAM.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import requests

from ..logging_setup import get_logger
from .base import ProviderError

log = get_logger("providers.http")

DEFAULT_TIMEOUT = 60


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """One JSON request, with the response body included in any error."""
    try:
        response = requests.request(
            method, url, headers=headers, json=json_body, timeout=timeout
        )
    except requests.RequestException as exc:
        raise ProviderError(f"{method} {url} failed: {exc}") from exc

    if response.status_code >= 400:
        raise ProviderError(
            f"{method} {url} -> HTTP {response.status_code}: {response.text[:600]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{url} returned non-JSON: {response.text[:300]}") from exc


def poll_until(
    fetch: Callable[[], dict[str, Any]],
    *,
    is_done: Callable[[dict[str, Any]], bool],
    is_failed: Callable[[dict[str, Any]], bool],
    timeout_s: int = 900,
    interval_s: float = 3.0,
    backoff: float = 1.15,
    max_interval_s: float = 15.0,
    label: str = "job",
) -> dict[str, Any]:
    """Poll ``fetch`` until done/failed/timeout, backing off between attempts.

    Video generation routinely takes minutes; a fixed 1s poll just burns rate
    limit. Backoff keeps long jobs cheap to wait on.
    """
    deadline = time.time() + timeout_s
    delay = interval_s
    while time.time() < deadline:
        payload = fetch()
        if is_failed(payload):
            raise ProviderError(
                f"{label} failed: {payload.get('error') or payload.get('detail') or payload}"
            )
        if is_done(payload):
            return payload
        time.sleep(delay)
        delay = min(max_interval_s, delay * backoff)
    raise ProviderError(f"{label} timed out after {timeout_s}s")


def download(url: str, output: Path, *, timeout: int = 300) -> Path:
    """Stream a URL to disk. Writes to a temp file then renames."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            if response.status_code >= 400:
                raise ProviderError(f"download {url} -> HTTP {response.status_code}")
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        handle.write(chunk)
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise ProviderError(f"download {url} failed: {exc}") from exc

    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ProviderError(f"download {url} produced an empty file")
    tmp.replace(output)
    return output


def first_url(payload: Any) -> str | None:
    """Find the first URL-looking string in an arbitrary API response.

    Model outputs are shaped inconsistently across vendors (`output` may be a
    string, a list, or a dict with `url`/`video`/`audio`). Rather than special-
    casing every model slug, walk the structure.
    """
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("url", "video", "audio", "image", "output", "file", "signed_url"):
            if key in payload:
                found = first_url(payload[key])
                if found:
                    return found
        for value in payload.values():
            found = first_url(value)
            if found:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = first_url(item)
            if found:
                return found
    return None
