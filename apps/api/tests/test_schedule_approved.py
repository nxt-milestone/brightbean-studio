"""An API key can schedule a post a person approved, in a workspace that requires approval.

In ``required_internal`` / ``required_internal_and_client`` the approval gate
refuses every draft → scheduled move from the API, and ``/schedule`` and
``schedule_draft`` only picked up draft children, so an agent could never
schedule a post even after a reviewer approved it. Approved children are now
scheduled; drafts are still refused, and ``publish_directly`` is still needed.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount

MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="approved-owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="Approved Org")
    workspace = Workspace.objects.create(
        name="Approved WS", organization=organization, approval_workflow_mode="required_internal"
    )
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


@pytest.fixture
def linkedin(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-1",
        account_name="li",
        connection_status="connected",
    )


def _client(user, workspace, account, permissions=PERMISSION_KEYS):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="approved",
        permissions=list(permissions),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def client(user, workspace, linkedin):
    return _client(user, workspace, linkedin)


def _post(workspace, user, account, status):
    post = Post.objects.create(workspace=workspace, author=user, caption="hi")
    PlatformPost.objects.create(post=post, social_account=account, status=status)
    return post


def _when():
    return timezone.now() + timedelta(hours=2)


def _schedule(client, post, when):
    return client.post(
        f"/api/v1/posts/{post.id}/schedule",
        data=json.dumps({"scheduled_at": when.isoformat()}),
        content_type="application/json",
    )


def _mcp(client, tool, arguments):
    rpc = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": arguments}, "id": 1}
    return client.post(MCP_URL, data=json.dumps(rpc), content_type="application/json").json()


@pytest.mark.django_db
class TestRest:
    def test_approved_post_is_scheduled(self, client, workspace, user, linkedin):
        post = _post(workspace, user, linkedin, "approved")
        when = _when()

        response = _schedule(client, post, when)

        assert response.status_code == 200, response.content
        child = post.platform_posts.get()
        assert child.status == "scheduled"
        assert child.scheduled_at == when

    def test_draft_is_still_refused(self, client, workspace, user, linkedin):
        post = _post(workspace, user, linkedin, "draft")

        response = _schedule(client, post, _when())

        assert response.status_code == 422, response.content
        assert "approval" in response.json()["detail"]
        assert post.platform_posts.get().status == "draft"

    def test_post_waiting_for_the_client_is_not_scheduled(self, client, workspace, user, linkedin):
        workspace.approval_workflow_mode = "required_internal_and_client"
        workspace.save()
        post = _post(workspace, user, linkedin, "pending_client")

        response = _schedule(client, post, _when())

        assert response.status_code == 409, response.content
        assert post.platform_posts.get().status == "pending_client"

    def test_needs_publish_directly(self, workspace, user, linkedin):
        client = _client(user, workspace, linkedin, [k for k in PERMISSION_KEYS if k != "publish_directly"])
        post = _post(workspace, user, linkedin, "approved")

        response = _schedule(client, post, _when())

        assert response.status_code == 403, response.content
        assert post.platform_posts.get().status == "approved"


@pytest.mark.django_db
class TestMcp:
    def test_approved_post_is_scheduled(self, client, workspace, user, linkedin):
        post = _post(workspace, user, linkedin, "approved")

        body = _mcp(client, "schedule_draft", {"post_id": str(post.id), "scheduled_at": _when().isoformat()})

        assert "result" in body, body
        assert post.platform_posts.get().status == "scheduled"

    def test_draft_is_still_refused(self, client, workspace, user, linkedin):
        post = _post(workspace, user, linkedin, "draft")

        body = _mcp(client, "schedule_draft", {"post_id": str(post.id), "scheduled_at": _when().isoformat()})

        assert body["error"]["code"] == INVALID_PARAMS
        assert "approval" in body["error"]["message"]
        assert post.platform_posts.get().status == "draft"
