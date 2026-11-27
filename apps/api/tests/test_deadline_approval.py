"""E-121: a post nobody approved or sent back by its approval deadline counts as approved.

``POST /posts/{id}/schedule`` and MCP ``schedule_draft`` take an optional
``approval_deadline``. Once it has passed, a ``pending_review`` post is approved
(recorded as ``deadline_passed`` under the key's name), scheduled, and the
"approved" webhook fires so the agents wake. A draft, changes requested,
rejected, or a deadline still ahead are refused and change nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.approvals.models import ApprovalAction
from apps.composer.models import PlatformPost, Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount

MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(email="agenten@example.com", password="testpass123", tos_accepted_at=timezone.now())


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="Deadline Org")
    workspace = Workspace.objects.create(
        name="Deadline WS", organization=organization, approval_workflow_mode="required_internal"
    )
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform="linkedin_personal"):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=platform,
        connection_status="connected",
    )


@pytest.fixture
def linkedin(workspace):
    return _account(workspace)


def _client(user, workspace, account, permissions=PERMISSION_KEYS):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="Prüfer",
        permissions=list(permissions),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def client(user, workspace, linkedin):
    return _client(user, workspace, linkedin)


@pytest.fixture
def webhook():
    with mock.patch("apps.approvals.services.notify_webhook") as sent:
        yield sent


def _post(workspace, user, account, status):
    post = Post.objects.create(workspace=workspace, author=user, caption="hi")
    PlatformPost.objects.create(post=post, social_account=account, status=status)
    return post


def _past():
    return timezone.now() - timedelta(minutes=5)


def _when():
    return timezone.now() + timedelta(hours=2)


def _schedule(client, post, deadline):
    body = {"scheduled_at": _when().isoformat(), "approval_deadline": deadline.isoformat()}
    return client.post(f"/api/v1/posts/{post.id}/schedule", data=json.dumps(body), content_type="application/json")


def _mcp(client, arguments):
    rpc = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "schedule_draft", "arguments": arguments},
        "id": 1,
    }
    return client.post(MCP_URL, data=json.dumps(rpc), content_type="application/json").json()


@pytest.mark.django_db
class TestRest:
    def test_overdue_post_is_approved_and_scheduled(
        self, client, workspace, user, linkedin, webhook, django_capture_on_commit_callbacks
    ):
        post = _post(workspace, user, linkedin, "pending_review")

        with django_capture_on_commit_callbacks(execute=True):
            response = _schedule(client, post, _past())

        assert response.status_code == 200, response.content
        assert post.platform_posts.get().status == "scheduled"
        entry = response.json()["approval_history"][0]
        assert entry["action"] == "deadline_passed"
        assert entry["user_name"] == "Prüfer"
        assert "verstrichen" in entry["comment"]
        webhook.assert_called_once()
        assert webhook.call_args.args[0] == EventType.POST_APPROVED
        assert webhook.call_args.kwargs["data"]["post_id"] == str(post.id)

    def test_deadline_ahead_is_refused(self, client, workspace, user, linkedin, webhook):
        post = _post(workspace, user, linkedin, "pending_review")

        response = _schedule(client, post, _when())

        assert response.status_code == 409, response.content
        assert "not passed" in response.json()["detail"]
        assert post.platform_posts.get().status == "pending_review"
        assert not ApprovalAction.objects.filter(post=post).exists()
        webhook.assert_not_called()

    @pytest.mark.parametrize("status", ["draft", "changes_requested", "rejected"])
    def test_a_decision_or_no_review_is_refused(self, client, workspace, user, linkedin, webhook, status):
        post = _post(workspace, user, linkedin, status)

        response = _schedule(client, post, _past())

        assert response.status_code == 409, response.content
        assert status in response.json()["detail"]
        assert post.platform_posts.get().status == status
        assert not ApprovalAction.objects.filter(post=post).exists()

    def test_client_stage_is_refused(self, client, workspace, user, linkedin, webhook):
        workspace.approval_workflow_mode = "required_internal_and_client"
        workspace.save()
        post = _post(workspace, user, linkedin, "pending_review")

        response = _schedule(client, post, _past())

        assert response.status_code == 409, response.content
        assert post.platform_posts.get().status == "pending_review"

    def test_needs_publish_directly(self, workspace, user, linkedin, webhook):
        client = _client(user, workspace, linkedin, [k for k in PERMISSION_KEYS if k != "publish_directly"])
        post = _post(workspace, user, linkedin, "pending_review")

        response = _schedule(client, post, _past())

        assert response.status_code == 403, response.content
        assert post.platform_posts.get().status == "pending_review"

    def test_unpublishable_post_keeps_waiting_and_sends_nothing(
        self, workspace, user, webhook, django_capture_on_commit_callbacks
    ):
        tiktok = _account(workspace, "tiktok")  # needs a video; this post has none
        client = _client(user, workspace, tiktok)
        post = _post(workspace, user, tiktok, "pending_review")

        with django_capture_on_commit_callbacks(execute=True):
            response = _schedule(client, post, _past())

        assert response.status_code == 422, response.content
        assert post.platform_posts.get().status == "pending_review"
        assert not ApprovalAction.objects.filter(post=post).exists()
        webhook.assert_not_called()

    def test_without_deadline_pending_review_stays_unschedulable(self, client, workspace, user, linkedin):
        post = _post(workspace, user, linkedin, "pending_review")

        response = client.post(
            f"/api/v1/posts/{post.id}/schedule",
            data=json.dumps({"scheduled_at": _when().isoformat()}),
            content_type="application/json",
        )

        assert response.status_code == 409, response.content
        assert post.platform_posts.get().status == "pending_review"


@pytest.mark.django_db
class TestMcp:
    def test_overdue_post_is_approved_and_scheduled(self, client, workspace, user, linkedin, webhook):
        post = _post(workspace, user, linkedin, "pending_review")

        body = _mcp(
            client,
            {"post_id": str(post.id), "scheduled_at": _when().isoformat(), "approval_deadline": _past().isoformat()},
        )

        assert "result" in body, body
        assert post.platform_posts.get().status == "scheduled"
        action = ApprovalAction.objects.get(post=post)
        assert action.action == ApprovalAction.ActionType.DEADLINE_PASSED
        assert action.actor_name == "Prüfer"

    def test_deadline_ahead_is_refused(self, client, workspace, user, linkedin, webhook):
        post = _post(workspace, user, linkedin, "pending_review")

        body = _mcp(
            client,
            {"post_id": str(post.id), "scheduled_at": _when().isoformat(), "approval_deadline": _when().isoformat()},
        )

        assert body["error"]["code"] == INVALID_PARAMS
        assert post.platform_posts.get().status == "pending_review"
