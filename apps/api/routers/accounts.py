"""``GET /api/v1/accounts`` — list the connected accounts this key may target."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.posts import _require_perm, _resolve_account
from apps.api.schemas import AccountsListResponse, AccountSummary, InstagramAudioResponse
from providers.exceptions import ProviderError

router = Router(tags=["accounts"])


@router.get(
    "/",
    response=AccountsListResponse,
    summary="List the SocialAccounts this API key is allowed to act on",
)
def list_accounts(request):
    enforce_http_rate_limits(request, is_write=False)
    api_key = request.api_key
    accounts = [AccountSummary.from_social_account(sa) for sa in api_key.social_accounts.all()]
    log_audit_entry(request, action="accounts.list", target_id=None, status_code=200)
    return AccountsListResponse(accounts=accounts)


def search_instagram_audio(account, *, query: str = "", audio_type: str = "music", after: str = ""):
    """Meta's ``/ig_audio`` for ``account``, as ``InstagramAudioResponse``; shared with MCP.

    Raises ``ValueError`` for an account that can't use the Audio API, and lets
    ``ProviderError`` through when Meta refuses.
    """
    from apps.publisher.engine import _provider_and_access_token

    if account.platform != "instagram":
        raise ValueError(
            "Instagram music search needs an Instagram account connected through Facebook Login "
            f"(this account is {account.platform})."
        )
    provider, access_token = _provider_and_access_token(account)
    found = provider.search_audio(
        access_token, account.account_platform_id, audio_type=audio_type, query=query, after=after
    )
    return InstagramAudioResponse(**found)


@router.get(
    "/{account_id}/instagram/audio",
    response=InstagramAudioResponse,
    summary="Search Instagram's music library for a Reel",
)
def instagram_audio(
    request,
    account_id: uuid.UUID,
    q: str = Query("", max_length=100, description="Search words; empty returns trending tracks."),
    type: Literal["music", "original_sound"] = Query("music", description="Library music or original sounds."),
    after: str = Query("", max_length=512, description="Cursor from a previous call's ``after``."),
):
    """Tracks to use as ``instagram.audio_id`` on a Reel. Calls Meta with the account's token."""
    enforce_http_rate_limits(request, is_write=False)
    # Only useful for writing a post, and it spends the account's Meta rate budget.
    _require_perm(request, "create_posts")
    account = _resolve_account(request, account_id)
    try:
        body = search_instagram_audio(account, query=q, audio_type=type, after=after)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    except ProviderError as exc:
        raise HttpError(502, f"Instagram refused the music search: {exc}") from exc
    log_audit_entry(request, action="accounts.instagram_audio", target_id=account.id, status_code=200)
    return body
