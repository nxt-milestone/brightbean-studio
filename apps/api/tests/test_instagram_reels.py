"""Instagram Reels: music from Instagram's library and a cover, through REST and MCP.

Stored per channel in ``platform_extra["instagram"]`` and only for a post that
goes out as a Reel (one video, Instagram connected through Facebook Login),
because Meta silently ignores parameters a container doesn't use. Agents find
tracks with ``GET /accounts/{id}/instagram/audio`` / ``search_instagram_audio``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError

MUSIC = {"audio_id": "587784541076604", "audio_volume": 80, "video_volume": 50}


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def setup(db):
    from apps.accounts.models import User
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(name="WS", organization=org)
    owner = User.objects.create_user(email="owner@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=owner, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)

    def account(platform):
        return SocialAccount.objects.create(
            workspace=ws,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status="connected",
        )

    accounts = {p: account(p) for p in ("instagram", "instagram_login", "linkedin_personal")}

    def key(permissions):
        issued = services.issue_api_key(
            workspace=ws,
            social_accounts=list(accounts.values()),
            issued_by=owner,
            name="Agent",
            permissions=permissions,
        )
        return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")

    def asset(media_type, mime_type, workspace=ws):
        return MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            file=f"test/{mime_type.replace('/', '.')}",
            filename="file",
            media_type=media_type,
            mime_type=mime_type,
        )

    return {
        "ws": ws,
        "ig": accounts["instagram"],
        "ig_login": accounts["instagram_login"],
        "li": accounts["linkedin_personal"],
        "client": key(["create_posts"]),
        "reader": key([]),
        "video": asset(MediaAsset.MediaType.VIDEO, "video/mp4"),
        "video2": asset(MediaAsset.MediaType.VIDEO, "video/mp4"),
        "jpeg": asset(MediaAsset.MediaType.IMAGE, "image/jpeg"),
        "png": asset(MediaAsset.MediaType.IMAGE, "image/png"),
        "asset": asset,
    }


def _create(setup, account=None, media=None, **extra):
    body = {
        "social_account_id": str((account or setup["ig"]).id),
        "caption": "Reel",
        "media_asset_ids": [str(a.id) for a in (media if media is not None else [setup["video"]])],
        **extra,
    }
    return setup["client"].post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _patch(setup, post_id, **fields):
    return setup["client"].patch(f"/api/v1/posts/{post_id}", data=json.dumps(fields), content_type="application/json")


def _mcp(client, name, arguments):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    resp = client.post("/api/v1/mcp/", data=json.dumps(rpc), content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _mcp_ok(client, name, arguments):
    envelope = _mcp(client, name, arguments)
    assert "error" not in envelope, envelope
    return json.loads(envelope["result"]["content"][0]["text"])


def _stored(post_id):
    return PlatformPost.objects.get(post_id=post_id).platform_extra.get("instagram")


@pytest.mark.django_db
class TestRestCreate:
    def test_music_and_cover_round_trip(self, setup):
        options = {**MUSIC, "thumb_offset": 1500, "cover_media_id": str(setup["jpeg"].id)}
        resp = _create(setup, instagram=options)

        assert resp.status_code == 201, resp.content
        child = resp.json()["platform_posts"][0]
        assert child["instagram"] == options
        assert child["media_audio_type"] is None
        assert _stored(resp.json()["id"]) == options

    def test_unset_fields_are_not_stored(self, setup):
        resp = _create(setup, instagram={"audio_id": "42"})

        assert resp.status_code == 201, resp.content
        assert _stored(resp.json()["id"]) == {"audio_id": "42"}
        assert resp.json()["platform_posts"][0]["instagram"]["audio_volume"] is None

    def test_a_post_without_options_stores_nothing(self, setup):
        resp = _create(setup)

        assert resp.status_code == 201, resp.content
        assert resp.json()["platform_posts"][0]["instagram"] is None
        assert _stored(resp.json()["id"]) is None

    @pytest.mark.parametrize(
        "options",
        [
            {"audio_id": "42", "audio_volume": 0},
            {"audio_id": "42", "video_volume": 101},
            {"audio_id": "abc"},
            {"audio_id": "１２"},
            {"audio_volume": 50},
            {"thumb_offset": -1},
            {"audio_id": "42", "audio_volum": 50},
        ],
        ids=[
            "volume-0",
            "volume-101",
            "letters",
            "non-ascii-digits",
            "volume-without-music",
            "negative-offset",
            "typo",
        ],
    )
    def test_malformed_options_are_422(self, setup, options):
        resp = _create(setup, instagram=options)

        assert resp.status_code == 422, resp.content
        assert not Post.objects.exists()

    @pytest.mark.parametrize(
        ("account", "media", "message"),
        [
            ("li", ["video"], "for Instagram accounts only"),
            ("ig_login", ["video"], "Facebook Login"),
            ("ig", ["jpeg"], "exactly one video"),
            ("ig", ["video", "video2"], "exactly one video"),
            ("ig", ["video", "jpeg"], "exactly one video"),
            ("ig", [], "exactly one video"),
        ],
    )
    def test_anything_but_a_reel_is_refused(self, setup, account, media, message):
        resp = _create(setup, setup[account], media=[setup[m] for m in media], instagram=MUSIC)

        assert resp.status_code == 422, resp.content
        assert message in resp.json()["detail"]
        assert not Post.objects.exists()

    def test_cover_must_be_a_jpeg_of_this_workspace(self, setup):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=setup["ws"].organization)
        foreign = setup["asset"](MediaAsset.MediaType.IMAGE, "image/jpeg", workspace=other)
        for cover in (setup["png"], setup["video2"], foreign):
            resp = _create(setup, instagram={"cover_media_id": str(cover.id)})
            assert resp.status_code == 422, resp.content
            assert "JPEG image" in resp.json()["detail"]
        assert not Post.objects.exists()


@pytest.mark.django_db
class TestRestPatch:
    def test_replace_keep_and_clear(self, setup):
        post_id = _create(setup, instagram=MUSIC).json()["id"]

        resp = _patch(setup, post_id, instagram={"thumb_offset": 2000})
        assert resp.status_code == 200, resp.content
        assert _stored(post_id) == {"thumb_offset": 2000}

        resp = _patch(setup, post_id, caption="other", instagram=None)
        assert resp.status_code == 200, resp.content
        assert _stored(post_id) == {"thumb_offset": 2000}

        resp = _patch(setup, post_id, instagram={})
        assert resp.status_code == 200, resp.content
        assert _stored(post_id) is None
        assert resp.json()["platform_posts"][0]["instagram"] is None

    def test_changing_music_on_an_approved_post_sends_it_back_to_review(self, setup):
        post_id = _create(setup, instagram=MUSIC).json()["id"]
        PlatformPost.objects.filter(post_id=post_id).update(status="approved")

        _patch(setup, post_id, instagram=MUSIC)  # unchanged: stays approved
        assert PlatformPost.objects.get(post_id=post_id).status == "approved"

        resp = _patch(setup, post_id, instagram={"audio_id": "99"})
        assert resp.status_code == 200, resp.content
        assert PlatformPost.objects.get(post_id=post_id).status == "pending_review"

    def test_swapping_the_video_for_an_image_is_refused_while_music_is_set(self, setup):
        post_id = _create(setup, instagram=MUSIC).json()["id"]

        resp = _patch(setup, post_id, media_asset_ids=[str(setup["jpeg"].id)], caption="changed")

        assert resp.status_code == 422, resp.content
        assert "exactly one video" in resp.json()["detail"]
        post = Post.objects.get(id=post_id)
        assert post.caption == "Reel"
        assert [m.media_asset_id for m in post.media_attachments.all()] == [setup["video"].id]

    def test_clearing_music_and_swapping_media_together_is_fine(self, setup):
        post_id = _create(setup, instagram=MUSIC).json()["id"]

        resp = _patch(setup, post_id, media_asset_ids=[str(setup["jpeg"].id)], instagram={})

        assert resp.status_code == 200, resp.content
        assert _stored(post_id) is None

    def test_music_on_a_post_that_is_no_reel_is_refused(self, setup):
        post_id = _create(setup, media=[setup["jpeg"]]).json()["id"]

        resp = _patch(setup, post_id, instagram=MUSIC)

        assert resp.status_code == 422, resp.content
        assert _stored(post_id) is None

    def test_the_audio_type_read_back_after_publishing_is_shown(self, setup):
        post_id = _create(setup, instagram=MUSIC).json()["id"]
        PlatformPost.objects.filter(post_id=post_id).update(
            platform_extra={"instagram": MUSIC, "media_audio_type": "MUSIC"}
        )

        resp = setup["client"].get(f"/api/v1/posts/{post_id}")

        assert resp.json()["platform_posts"][0]["media_audio_type"] == "MUSIC"


@pytest.mark.django_db
class TestAudioSearch:
    TRACK = {"audio_id": "587784541076604", "title": "Sunrise", "display_artist": "Someone", "duration_in_ms": 9000}

    def _provider(self, **kwargs):
        provider = MagicMock()
        provider.search_audio.return_value = {"audio": [self.TRACK], "after": "CURSOR"}
        provider.search_audio.side_effect = kwargs.get("side_effect")
        return patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "page-token"))

    def test_rest_returns_tracks_and_uses_the_account_token(self, setup):
        with self._provider() as factory:
            resp = setup["client"].get(f"/api/v1/accounts/{setup['ig'].id}/instagram/audio?q=sunrise&after=PREV")

        assert resp.status_code == 200, resp.content
        assert resp.json()["after"] == "CURSOR"
        assert resp.json()["audio"][0]["audio_id"] == "587784541076604"
        assert resp.json()["audio"][0]["download_url"] is None
        provider = factory.return_value[0]
        provider.search_audio.assert_called_once_with(
            "page-token", "instagram-1", audio_type="music", query="sunrise", after="PREV"
        )

    def test_rest_refuses_other_platforms_bad_types_and_foreign_accounts(self, setup):
        with self._provider():
            assert setup["client"].get(f"/api/v1/accounts/{setup['li'].id}/instagram/audio").status_code == 422
            login = setup["client"].get(f"/api/v1/accounts/{setup['ig_login'].id}/instagram/audio")
            assert login.status_code == 422
            assert "Facebook Login" in login.json()["detail"]
            bad = setup["client"].get(f"/api/v1/accounts/{setup['ig'].id}/instagram/audio?type=chart")
            assert bad.status_code == 422
            foreign = SocialAccount.objects.create(
                workspace=setup["ws"], platform="instagram", account_platform_id="ig-2", account_name="other"
            )
            assert setup["client"].get(f"/api/v1/accounts/{foreign.id}/instagram/audio").status_code == 403

    def test_rest_needs_create_posts(self, setup):
        with self._provider():
            resp = setup["reader"].get(f"/api/v1/accounts/{setup['ig'].id}/instagram/audio")

        assert resp.status_code == 403

    def test_rest_reports_metas_refusal_as_502(self, setup):
        with self._provider(side_effect=APIError("(#10) not allowed", status_code=400, platform="Instagram")):
            resp = setup["client"].get(f"/api/v1/accounts/{setup['ig'].id}/instagram/audio")

        assert resp.status_code == 502
        assert "not allowed" in resp.json()["detail"]

    def test_mcp_tool(self, setup):
        with self._provider() as factory:
            found = _mcp_ok(
                setup["client"],
                "search_instagram_audio",
                {"social_account_id": str(setup["ig"].id), "query": "sunrise", "type": "original_sound"},
            )

        assert found["audio"][0]["title"] == "Sunrise"
        assert found["after"] == "CURSOR"
        factory.return_value[0].search_audio.assert_called_once_with(
            "page-token", "instagram-1", audio_type="original_sound", query="sunrise", after=""
        )

    def test_mcp_tool_refusals(self, setup):
        with self._provider(side_effect=APIError("(#10) not allowed", status_code=400, platform="Instagram")):
            refused = _mcp(setup["client"], "search_instagram_audio", {"social_account_id": str(setup["ig"].id)})
            wrong = _mcp(setup["client"], "search_instagram_audio", {"social_account_id": str(setup["li"].id)})
            denied = _mcp(setup["reader"], "search_instagram_audio", {"social_account_id": str(setup["ig"].id)})

        assert "not allowed" in refused["error"]["message"]
        assert wrong["error"]["code"] == INVALID_PARAMS
        assert "Facebook Login" in wrong["error"]["message"]
        assert "create_posts" in denied["error"]["message"]


@pytest.mark.django_db
class TestMcp:
    def _draft(self, setup, **arguments):
        return _mcp(
            setup["client"],
            "create_draft",
            {
                "social_account_id": str(setup["ig"].id),
                "caption": "Reel",
                "media_asset_ids": [str(setup["video"].id)],
                **arguments,
            },
        )

    def test_create_draft_and_update_post(self, setup):
        envelope = self._draft(setup, instagram=MUSIC)
        post = json.loads(envelope["result"]["content"][0]["text"])
        assert post["platform_posts"][0]["instagram"]["audio_id"] == MUSIC["audio_id"]
        assert _stored(post["id"]) == MUSIC

        cover = {"cover_media_id": str(setup["jpeg"].id)}
        updated = _mcp_ok(setup["client"], "update_post", {"post_id": post["id"], "instagram": cover})
        assert updated["platform_posts"][0]["instagram"]["cover_media_id"] == cover["cover_media_id"]
        assert _stored(post["id"]) == cover

        _mcp_ok(setup["client"], "update_post", {"post_id": post["id"], "instagram": {}})
        assert _stored(post["id"]) is None

    def test_update_post_sends_an_approved_post_back_to_review(self, setup):
        post = json.loads(self._draft(setup)["result"]["content"][0]["text"])
        PlatformPost.objects.filter(post_id=post["id"]).update(status="approved")

        _mcp_ok(setup["client"], "update_post", {"post_id": post["id"], "instagram": MUSIC})

        assert PlatformPost.objects.get(post_id=post["id"]).status == "pending_review"

    @pytest.mark.parametrize(
        ("instagram", "message"),
        [
            ({"audio_id": "42", "audio_volume": 101}, "audio_volume"),
            ({"audio_id": "x1"}, "audio_id"),
            ({"video_volume": 20}, "need an audio_id"),
            ({"audio_id": "42", "extra": 1}, "extra"),
        ],
    )
    def test_create_draft_refuses_malformed_options(self, setup, instagram, message):
        envelope = self._draft(setup, instagram=instagram)

        assert message in envelope["error"]["message"]
        assert not Post.objects.exists()

    def test_update_post_refuses_music_on_linkedin_and_keeps_the_post(self, setup):
        post = _mcp_ok(setup["client"], "create_draft", {"social_account_id": str(setup["li"].id), "caption": "x"})

        envelope = _mcp(
            setup["client"], "update_post", {"post_id": post["id"], "caption": "changed", "instagram": MUSIC}
        )

        assert envelope["error"]["code"] == INVALID_PARAMS
        assert "Instagram accounts only" in envelope["error"]["message"]
        assert Post.objects.get(id=post["id"]).caption == "x"


@pytest.mark.django_db
@override_settings(APP_URL="https://studio.example")
def test_the_publisher_hands_instagram_the_covers_public_url(setup):
    from apps.publisher.tests import _build_dispatch_mocks

    options = {**MUSIC, "cover_media_id": str(setup["jpeg"].id)}
    engine, platform_post, provider = _build_dispatch_mocks("instagram", "ig-1", platform_extra={"instagram": options})

    with (
        patch("apps.publisher.engine.publish_blocker", return_value=""),
        patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")),
    ):
        engine._dispatch_to_provider(platform_post)

    content = provider.publish_post.call_args.args[1]
    assert content.extra["cover_url"] == f"https://studio.example{setup['jpeg'].file.url}"
    assert content.extra["instagram"] == options


@pytest.mark.django_db
def test_a_reel_cover_is_not_an_orphan(setup):
    from apps.media_library.services import _json_referenced_asset_ids

    _create(setup, instagram={"cover_media_id": str(setup["jpeg"].id)})

    assert setup["jpeg"].id in _json_referenced_asset_ids()
