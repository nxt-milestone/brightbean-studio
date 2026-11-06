"""Review through the Agent API and MCP: tags, category, list filters,
comments, submit-for-review and approval history.

The contract under test: a drafts-only key (``create_posts``) can label a
post, discuss it and hand it to review, but nothing it can call moves a post
past review — and edits to an approved post send it back.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.approvals.models import ApprovalAction, PostComment
from apps.composer.models import ContentCategory, PlatformPost, Post, Tag
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _user(email, name):
    from apps.accounts.models import User

    return User.objects.create_user(email=email, password="x", name=name, tos_accepted_at=timezone.now())


@pytest.fixture
def setup(db):
    from apps.organizations.models import Organization
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(name="WS", organization=org, approval_workflow_mode="required_internal")
    owner = _user("owner@example.com", "Owner")
    reviewer = _user("reviewer@example.com", "Reviewer")
    OrgMembership.objects.create(user=owner, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)
    WorkspaceMembership.objects.create(
        user=reviewer, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER
    )
    sa = SocialAccount.objects.create(
        workspace=ws,
        platform="linkedin_personal",
        account_platform_id="li-1",
        account_name="LI",
        connection_status="connected",
    )
    ContentCategory.objects.create(workspace=ws, name="Pillar 1")
    issued = services.issue_api_key(
        workspace=ws, social_accounts=[sa], issued_by=owner, name="LinkedIn-Autor", permissions=["create_posts"]
    )
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    return {"ws": ws, "owner": owner, "reviewer": reviewer, "sa": sa, "client": client}


def _create(setup, **extra):
    body = {"social_account_id": str(setup["sa"].id), "caption": "hello", **extra}
    resp = setup["client"].post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _patch(setup, post_id, **fields):
    return setup["client"].patch(f"/api/v1/posts/{post_id}", data=json.dumps(fields), content_type="application/json")


def _mcp(setup, name, arguments):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    resp = setup["client"].post("/api/v1/mcp/", data=json.dumps(rpc), content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _mcp_ok(setup, name, arguments):
    envelope = _mcp(setup, name, arguments)
    assert "error" not in envelope, envelope
    return json.loads(envelope["result"]["content"][0]["text"])


@pytest.mark.django_db
class TestLabels:
    def test_create_with_tags_and_category_round_trips(self, setup):
        post = _create(setup, tags=["P-0001", "format:text", "P-0001"], category="Pillar 1")
        assert post["tags"] == ["P-0001", "format:text"]
        assert post["category"] == "Pillar 1"
        assert set(Tag.objects.filter(workspace=setup["ws"]).values_list("name", flat=True)) == {
            "P-0001",
            "format:text",
        }

    def test_unknown_category_is_422_and_creates_nothing(self, setup):
        body = {"social_account_id": str(setup["sa"].id), "caption": "x", "category": "Nope"}
        resp = setup["client"].post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")
        assert resp.status_code == 422
        assert "Pillar 1" in resp.json()["detail"]
        assert not Post.objects.exists()

    def test_patch_replaces_tags_and_clears_category(self, setup):
        post = _create(setup, tags=["a"], category="Pillar 1")
        resp = _patch(setup, post["id"], tags=["b", "c"], category="")
        assert resp.status_code == 200, resp.content
        assert resp.json()["tags"] == ["b", "c"]
        assert resp.json()["category"] is None
        # Omitted fields stay as they are.
        resp = _patch(setup, post["id"], caption="new")
        assert resp.json()["tags"] == ["b", "c"]

    def test_list_filters_by_tag_and_status(self, setup):
        a = _create(setup, tags=["P-0001"])
        _create(setup, tags=["P-0002"])
        resp = setup["client"].get("/api/v1/posts/?tag=P-0001")
        assert resp.status_code == 200
        assert [p["id"] for p in resp.json()["posts"]] == [a["id"]]
        setup["client"].post(f"/api/v1/posts/{a['id']}/submit")
        resp = setup["client"].get("/api/v1/posts/?status=pending_review")
        assert [p["id"] for p in resp.json()["posts"]] == [a["id"]]
        assert setup["client"].get("/api/v1/posts/?status=bogus").status_code == 422

    def test_list_paginates(self, setup):
        for _ in range(3):
            _create(setup)
        page = setup["client"].get("/api/v1/posts/?limit=2").json()
        assert len(page["posts"]) == 2 and page["next_cursor"]
        rest = setup["client"].get(f"/api/v1/posts/?limit=2&cursor={page['next_cursor']}").json()
        assert len(rest["posts"]) == 1 and rest["next_cursor"] is None


@pytest.mark.django_db
class TestReview:
    def test_submit_moves_to_pending_review_and_notifies_reviewers(self, setup):
        post = _create(setup)
        resp = setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["status"] == "pending_review"
        assert body["approval_history"][0]["action"] == "submitted"
        assert body["approval_history"][0]["user_name"] == "Owner"
        assert Notification.objects.filter(user=setup["reviewer"], event_type=EventType.POST_SUBMITTED).exists()

    def test_submit_twice_is_409(self, setup):
        post = _create(setup)
        setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        assert setup["client"].post(f"/api/v1/posts/{post['id']}/submit").status_code == 409

    def test_no_workflow_workspace_refuses_submit(self, setup):
        setup["ws"].approval_workflow_mode = "none"
        setup["ws"].save()
        post = _create(setup)
        assert setup["client"].post(f"/api/v1/posts/{post['id']}/submit").status_code == 409

    def test_get_shows_who_approved_and_when(self, setup):
        from apps.approvals.services import approve_post

        post = _create(setup)
        setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        approve_post(Post.objects.get(id=post["id"]), setup["reviewer"], setup["ws"], comment="ok")
        body = setup["client"].get(f"/api/v1/posts/{post['id']}").json()
        assert body["status"] == "approved"
        latest = body["approval_history"][0]
        assert (latest["action"], latest["user_name"], latest["comment"]) == ("approved", "Reviewer", "ok")
        assert latest["created_at"].endswith("Z")

    def test_editing_an_approved_post_sends_it_back_to_review(self, setup):
        from apps.approvals.services import approve_post

        post = _create(setup)
        setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        approve_post(Post.objects.get(id=post["id"]), setup["reviewer"], setup["ws"])
        resp = _patch(setup, post["id"], caption="sneaky edit")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_review"

    def test_drafts_only_key_cannot_schedule_an_approved_post(self, setup):
        from apps.approvals.services import approve_post

        post = _create(setup)
        setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        approve_post(Post.objects.get(id=post["id"]), setup["reviewer"], setup["ws"])
        resp = setup["client"].post(
            f"/api/v1/posts/{post['id']}/schedule",
            data=json.dumps({"scheduled_at": "2030-01-01T09:00:00Z"}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_cannot_edit_while_in_review(self, setup):
        post = _create(setup)
        setup["client"].post(f"/api/v1/posts/{post['id']}/submit")
        assert _patch(setup, post["id"], tags=["x"]).status_code == 409


@pytest.mark.django_db
class TestComments:
    def test_comment_shows_key_name_and_defaults_internal(self, setup):
        post = _create(setup)
        resp = setup["client"].post(
            f"/api/v1/posts/{post['id']}/comments",
            data=json.dumps({"body": "Hook is weak"}),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        assert resp.json()["author_name"] == "LinkedIn-Autor"
        assert resp.json()["visibility"] == "internal"
        comment = PostComment.objects.get()
        assert comment.author == setup["owner"]
        assert comment.author_name == "LinkedIn-Autor"

        listed = setup["client"].get(f"/api/v1/posts/{post['id']}/comments").json()["comments"]
        assert [c["body"] for c in listed] == ["Hook is weak"]

    def test_ui_comment_shows_person(self, setup):
        from apps.approvals.comments import create_comment

        post = _create(setup)
        create_comment(Post.objects.get(id=post["id"]), setup["reviewer"], "Looks good", "external")
        listed = setup["client"].get(f"/api/v1/posts/{post['id']}/comments").json()["comments"]
        assert listed[0]["author_name"] == "Reviewer"

    def test_reader_key_sees_only_external_comments_and_cannot_write(self, setup):
        from apps.approvals.comments import create_comment

        post = _create(setup)
        p = Post.objects.get(id=post["id"])
        create_comment(p, setup["owner"], "team only", "internal")
        create_comment(p, setup["owner"], "for client", "external")
        reader = services.issue_api_key(
            workspace=setup["ws"], social_accounts=[setup["sa"]], issued_by=setup["owner"], name="r", permissions=[]
        )
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {reader.plaintext_token}")
        listed = client.get(f"/api/v1/posts/{post['id']}/comments").json()["comments"]
        assert [c["body"] for c in listed] == ["for client"]
        resp = client.post(
            f"/api/v1/posts/{post['id']}/comments", data=json.dumps({"body": "x"}), content_type="application/json"
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestMcpParity:
    def test_mcp_review_flow(self, setup):
        post = _mcp_ok(
            setup,
            "create_draft",
            {"social_account_id": str(setup["sa"].id), "caption": "hi", "tags": ["P-0003"], "category": "Pillar 1"},
        )
        assert (post["tags"], post["category"]) == (["P-0003"], "Pillar 1")

        updated = _mcp_ok(setup, "update_post", {"post_id": post["id"], "tags": ["P-0003", "art:thema"]})
        assert updated["tags"] == ["P-0003", "art:thema"]

        listed = _mcp_ok(setup, "list_posts", {"tag": "art:thema"})
        assert [p["id"] for p in listed["posts"]] == [post["id"]]

        comment = _mcp_ok(setup, "add_post_comment", {"post_id": post["id"], "body": "Check"})
        assert comment["author_name"] == "LinkedIn-Autor"
        comments = _mcp_ok(setup, "list_post_comments", {"post_id": post["id"]})["comments"]
        assert [c["id"] for c in comments] == [comment["id"]]

        submitted = _mcp_ok(setup, "submit_post_for_review", {"post_id": post["id"]})
        assert submitted["status"] == "pending_review"
        assert PlatformPost.objects.get(post_id=post["id"]).status == "pending_review"
        assert ApprovalAction.objects.filter(post_id=post["id"], action="submitted").count() == 1

    def test_mcp_update_rejects_unknown_category(self, setup):
        post = _mcp_ok(setup, "create_draft", {"social_account_id": str(setup["sa"].id), "caption": "hi"})
        envelope = _mcp(setup, "update_post", {"post_id": post["id"], "category": "Nope"})
        assert "error" in envelope or envelope["result"]["isError"]
