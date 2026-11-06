"""Ideas board through the Agent API and MCP: cards, columns, tags, media."""

from __future__ import annotations

import json

import pytest
from django.core.files.base import ContentFile
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import Idea, IdeaGroup, Tag
from apps.members.models import OrgMembership, WorkspaceMembership


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def setup(db):
    from apps.accounts.models import User
    from apps.organizations.models import Organization
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(name="WS", organization=org)
    owner = User.objects.create_user(email="o@example.com", password="x", name="Owner", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=owner, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)
    sa = SocialAccount.objects.create(
        workspace=ws,
        platform="linkedin_personal",
        account_platform_id="li",
        account_name="LI",
        connection_status="connected",
    )

    def client(perms):
        key = services.issue_api_key(
            workspace=ws, social_accounts=[sa], issued_by=owner, name="Stratege", permissions=perms
        )
        return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")

    return {"ws": ws, "owner": owner, "client": client(["create_posts"]), "reader": client([])}


def _post(client, path, body):
    return client.post(path, data=json.dumps(body), content_type="application/json")


def _patch(client, path, body):
    return client.patch(path, data=json.dumps(body), content_type="application/json")


def _mcp_ok(client, name, arguments):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    envelope = client.post("/api/v1/mcp/", data=json.dumps(rpc), content_type="application/json").json()
    assert "error" not in envelope and not envelope["result"]["isError"], envelope
    return json.loads(envelope["result"]["content"][0]["text"])


@pytest.mark.django_db
class TestIdeasRest:
    def test_first_card_creates_default_columns_and_lands_in_first(self, setup):
        resp = _post(
            setup["client"], "/api/v1/ideas/", {"title": "I-0001 Hook", "tags": ["art:thema", "stand:kandidat"]}
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        first = IdeaGroup.objects.filter(workspace=setup["ws"]).order_by("position").first()
        assert body["group_id"] == str(first.id) and body["group_name"] == "Unassigned"
        assert body["tags"] == ["art:thema", "stand:kandidat"]
        assert {"art:thema", "stand:kandidat"} <= set(Tag.objects.values_list("name", flat=True))

    def test_update_moves_card_and_replaces_tags(self, setup):
        idea = _post(setup["client"], "/api/v1/ideas/", {"title": "x", "tags": ["a"]}).json()
        col = _post(setup["client"], "/api/v1/ideas/groups", {"name": "Bewährt"}).json()
        resp = _patch(setup["client"], f"/api/v1/ideas/{idea['id']}", {"group_id": col["id"], "tags": ["b"]})
        assert resp.status_code == 200, resp.content
        assert (resp.json()["group_name"], resp.json()["tags"], resp.json()["title"]) == ("Bewährt", ["b"], "x")

    def test_media_attach_in_order_and_foreign_media_rejected(self, setup):
        from apps.media_library.models import MediaAsset

        assets = [
            MediaAsset.objects.create(
                workspace=setup["ws"],
                organization=setup["ws"].organization,
                filename=f"{n}.png",
                file=ContentFile(b"x", name=f"{n}.png"),
                media_type="image",
            )
            for n in ("a", "b")
        ]
        idea = _post(setup["client"], "/api/v1/ideas/", {"title": "x"}).json()
        ids = [str(assets[1].id), str(assets[0].id)]
        resp = _patch(setup["client"], f"/api/v1/ideas/{idea['id']}", {"media_asset_ids": ids})
        assert resp.json()["media_asset_ids"] == ids
        assert str(Idea.objects.get(id=idea["id"]).media_asset_id) == ids[0]
        bad = _patch(setup["client"], f"/api/v1/ideas/{idea['id']}", {"media_asset_ids": [str(setup["ws"].id)]})
        assert bad.status_code == 422

    def test_list_filters_by_tag_and_paginates(self, setup):
        for n in range(3):
            _post(setup["client"], "/api/v1/ideas/", {"title": f"t{n}", "tags": ["muster"] if n else []})
        resp = setup["client"].get("/api/v1/ideas/?tag=muster&limit=1").json()
        assert len(resp["ideas"]) == 1 and resp["next_cursor"]
        assert all("muster" in i["tags"] for i in resp["ideas"])

    def test_blank_title_and_unknown_group_are_422(self, setup):
        assert _post(setup["client"], "/api/v1/ideas/", {"title": ""}).status_code == 422
        resp = _post(setup["client"], "/api/v1/ideas/", {"title": "x", "group_id": str(setup["ws"].id)})
        assert resp.status_code == 422

    def test_group_rename_and_reorder(self, setup):
        col = _post(setup["client"], "/api/v1/ideas/groups", {"name": "Trend"}).json()
        resp = _patch(setup["client"], f"/api/v1/ideas/groups/{col['id']}", {"name": "Trends", "position": 0})
        assert (resp.json()["name"], resp.json()["position"]) == ("Trends", 0)
        assert setup["client"].get("/api/v1/ideas/groups").json()["groups"][0]["name"] == "Trends"

    def test_needs_create_posts(self, setup):
        assert setup["reader"].get("/api/v1/ideas/").status_code == 403
        assert _post(setup["reader"], "/api/v1/ideas/", {"title": "x"}).status_code == 403

    def test_other_workspace_idea_is_404(self, setup):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=setup["ws"].organization)
        foreign = Idea.objects.create(workspace=other, title="secret")
        assert setup["client"].get(f"/api/v1/ideas/{foreign.id}").status_code == 404
        assert _patch(setup["client"], f"/api/v1/ideas/{foreign.id}", {"title": "x"}).status_code == 404


@pytest.mark.django_db
class TestIdeasMcp:
    def test_mcp_round_trip(self, setup):
        c = setup["client"]
        col = _mcp_ok(c, "create_idea_group", {"name": "Kandidat"})
        idea = _mcp_ok(c, "create_idea", {"title": "I-0002", "tags": ["art:muster"], "group_id": col["id"]})
        assert idea["group_name"] == "Kandidat"
        updated = _mcp_ok(c, "update_idea", {"idea_id": idea["id"], "description": "Hypothese: ..."})
        assert updated["description"] == "Hypothese: ..." and updated["tags"] == ["art:muster"]
        assert _mcp_ok(c, "get_idea", {"idea_id": idea["id"]})["id"] == idea["id"]
        assert [i["id"] for i in _mcp_ok(c, "list_ideas", {"tag": "art:muster"})["ideas"]] == [idea["id"]]
        renamed = _mcp_ok(c, "update_idea_group", {"group_id": col["id"], "name": "Kandidaten"})
        assert renamed["name"] == "Kandidaten"
        assert "Kandidaten" in [g["name"] for g in _mcp_ok(c, "list_idea_groups", {})["groups"]]
