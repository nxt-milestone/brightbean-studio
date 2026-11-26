"""Comments on every saved post (nxt fork): a fresh draft has no approval history, but people comment on it in the
editor („als Kommentar am Entwurf"). Banner, history and review buttons still need approval history."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals.models import ApprovalAction
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class DraftCommentsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(organization=self.org, name="WS", timezone="Europe/Berlin")
        account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="linkedin_company",
            account_platform_id="li-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        self.post = Post.objects.create(workspace=self.ws, author=self.user, caption="draft")
        PlatformPost.objects.create(post=self.post, social_account=account, status="draft")
        self.comment_url = reverse(
            "approvals:add_comment", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id}
        )

    def _edit(self):
        url = reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id})
        return self.client.get(url).content.decode()

    def test_a_draft_without_history_has_the_comment_form(self):
        html = self._edit()
        self.assertIn(self.comment_url, html)
        self.assertNotIn(">Approval History</h4>", html)

    def test_a_post_with_history_still_shows_it(self):
        ApprovalAction.objects.create(post=self.post, user=self.user, action=ApprovalAction.ActionType.SUBMITTED)
        html = self._edit()
        self.assertIn(">Approval History</h4>", html)
        self.assertIn(self.comment_url, html)
