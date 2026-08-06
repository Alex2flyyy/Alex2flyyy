"""TikTok Content Posting API client.

**Read this before assuming publishing is automatic.**

Automated posting to TikTok requires all of the following, and none of it can be
provisioned by this code:

1. A TikTok for Developers account and a registered app.
2. The **Content Posting API** product added to that app and *approved by
   TikTok*. Approval is a manual review with no guaranteed outcome or timeline.
3. Scopes ``video.upload`` (drafts) and, for posting without a human tap,
   ``video.publish`` — the second is granted more sparingly than the first.
4. A completed OAuth flow producing an access token + refresh token for the
   target account. Access tokens are short-lived; refresh tokens expire too.
5. For unaudited apps, posts are restricted to private/self-view until TikTok
   audits the app.

If any of that isn't in place, use
:mod:`pawparty.publishing.manual_export` — it takes seconds per video and has no
approval dependency.

Two upload modes are implemented:

* ``inbox`` (``/v2/post/publish/inbox/video/init/``) — the video lands in the
  user's TikTok inbox as a draft; they tap to finish posting. Needs only
  ``video.upload``. **This is the realistic default.**
* ``direct`` (``/v2/post/publish/video/init/``) — posts directly. Needs
  ``video.publish``.

The API surface changes; this client targets the v2 endpoints and keeps the
request shapes in one place so an update is a small edit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from ..logging_setup import get_logger
from ..providers.base import ProviderError, ProviderUnavailable
from ..providers.http import poll_until, request_json

log = get_logger("publishing.tiktok")

API_ROOT = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API_ROOT}/oauth/token/"

#: TikTok requires chunks of 5MB-64MB, and a whole-file single chunk is allowed
#: below 64MB. Our videos are far smaller, so single-chunk is always used.
SINGLE_CHUNK_LIMIT = 64 * 1024 * 1024


@dataclass
class PublishResult:
    publish_id: str
    status: str
    mode: str
    detail: str = ""


class TikTokPublisher:
    """Thin client over the Content Posting API."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        client_key: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        mode: str = "inbox",
    ) -> None:
        self.access_token = (access_token or os.getenv("TIKTOK_ACCESS_TOKEN", "")).strip()
        self.client_key = (client_key or os.getenv("TIKTOK_CLIENT_KEY", "")).strip()
        self.client_secret = (client_secret or os.getenv("TIKTOK_CLIENT_SECRET", "")).strip()
        self.refresh_token = (refresh_token or os.getenv("TIKTOK_REFRESH_TOKEN", "")).strip()
        if mode not in ("inbox", "direct"):
            raise ValueError("mode must be 'inbox' or 'direct'")
        self.mode = mode

    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        return bool(self.access_token)

    def require(self) -> None:
        if not self.access_token:
            raise ProviderUnavailable(
                "TIKTOK_ACCESS_TOKEN is not set. Automated posting needs an approved "
                "TikTok developer app — see docs/PUBLISHING.md. Use "
                "`pawparty publish --manual` in the meantime."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def refresh(self) -> str:
        """Exchange the refresh token for a new access token."""
        if not (self.client_key and self.client_secret and self.refresh_token):
            raise ProviderUnavailable(
                "Token refresh needs TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and "
                "TIKTOK_REFRESH_TOKEN."
            )
        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=60,
        )
        if response.status_code >= 400:
            raise ProviderError(f"token refresh failed: {response.text[:400]}")
        payload = response.json()
        self.access_token = payload.get("access_token", "")
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        if not self.access_token:
            raise ProviderError(f"token refresh returned no access_token: {payload}")
        log.info("refreshed TikTok access token")
        return self.access_token

    # ------------------------------------------------------------------ #
    def creator_info(self) -> dict[str, Any]:
        """Query posting limits and the creator's privacy options.

        TikTok requires this call before a direct post so the app shows the
        creator's real options rather than guessing.
        """
        self.require()
        return request_json(
            "POST", f"{API_ROOT}/post/publish/creator_info/query/", headers=self._headers
        )

    def publish(
        self,
        video: Path,
        *,
        caption: str,
        privacy_level: str = "SELF_ONLY",
        disable_comment: bool = False,
        disable_duet: bool = False,
        disable_stitch: bool = False,
        is_ai_generated: bool = True,
        poll: bool = True,
    ) -> PublishResult:
        """Upload and (in ``direct`` mode) post a video.

        ``is_ai_generated`` sets the AI-generated content label. Leave it on —
        it is required by TikTok's synthetic-media policy for content like this,
        and mislabelling risks the account, not just the post.
        """
        self.require()
        video = Path(video)
        if not video.exists():
            raise FileNotFoundError(video)

        size = video.stat().st_size
        if size > SINGLE_CHUNK_LIMIT:
            raise ProviderError(
                f"{video.name} is {size / 1e6:.0f}MB; this client only does single-chunk "
                f"uploads (<= 64MB). Lower render quality or add chunked upload."
            )

        init_payload: dict[str, Any] = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            }
        }

        if self.mode == "direct":
            init_url = f"{API_ROOT}/post/publish/video/init/"
            init_payload["post_info"] = {
                "title": caption,
                "privacy_level": privacy_level,
                "disable_comment": disable_comment,
                "disable_duet": disable_duet,
                "disable_stitch": disable_stitch,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": is_ai_generated,
            }
        else:
            init_url = f"{API_ROOT}/post/publish/inbox/video/init/"

        init = request_json("POST", init_url, headers=self._headers, json_body=init_payload)
        data = init.get("data") or {}
        publish_id = data.get("publish_id", "")
        upload_url = data.get("upload_url", "")
        if not (publish_id and upload_url):
            raise ProviderError(f"TikTok init returned no upload target: {init}")

        self._upload(upload_url, video, size)
        log.info("uploaded %s (publish_id=%s)", video.name, publish_id)

        if not poll:
            return PublishResult(publish_id=publish_id, status="UPLOADED", mode=self.mode)

        final = poll_until(
            lambda: request_json(
                "POST", f"{API_ROOT}/post/publish/status/fetch/",
                headers=self._headers, json_body={"publish_id": publish_id},
            ),
            is_done=lambda p: (p.get("data") or {}).get("status")
            in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"),
            is_failed=lambda p: (p.get("data") or {}).get("status") == "FAILED",
            timeout_s=600,
            interval_s=5.0,
            label=f"tiktok publish {publish_id}",
        )
        status = (final.get("data") or {}).get("status", "UNKNOWN")
        return PublishResult(
            publish_id=publish_id,
            status=status,
            mode=self.mode,
            detail=str((final.get("data") or {}).get("fail_reason", "")),
        )

    def _upload(self, upload_url: str, video: Path, size: int) -> None:
        """PUT the whole file as a single chunk."""
        with video.open("rb") as handle:
            response = requests.put(
                upload_url,
                data=handle,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                timeout=600,
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"upload failed with HTTP {response.status_code}: {response.text[:400]}"
            )
