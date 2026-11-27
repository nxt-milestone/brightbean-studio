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

    def test_the_comment_form_is_not_nested_in_the_editor_form(self):
        # Comments sit under Notes inside #composer-form; the comment form is outside and fields join via form="…".
        html = self._edit()
        editor_form = html[
            html.index('<form id="composer-form"') : html.index("</form>", html.index('<form id="composer-form"'))
        ]
        self.assertNotIn("<form", editor_form[1:])
        self.assertIn(f'form="comment-form-{self.post.id}" name="body"', editor_form)
        self.assertIn(f'<form id="comment-form-{self.post.id}"', html)

    def test_the_author_can_delete_their_comment_from_the_editor(self):
        from apps.approvals.comments import create_comment

        comment = create_comment(post=self.post, author=self.user, body="Bitte ändern", visibility="external")
        delete_url = reverse(
            "approvals:delete_comment",
            kwargs={"workspace_id": self.ws.id, "post_id": self.post.id, "comment_id": comment.id},
        )
        self.assertIn(delete_url, self._edit())
        resp = self.client.post(delete_url)
        self.assertEqual(resp.status_code, 200)
        comment.refresh_from_db()
        self.assertIsNotNone(comment.deleted_at)

    def test_a_comment_from_another_workspace_cannot_be_deleted(self):
        from apps.approvals.comments import create_comment

        other_ws = Workspace.objects.create(organization=self.org, name="Other", timezone="Europe/Berlin")
        other_post = Post.objects.create(workspace=other_ws, author=self.user, caption="x")
        comment = create_comment(post=other_post, author=self.user, body="nope", visibility="external")
        url = reverse(
            "approvals:delete_comment",
            kwargs={"workspace_id": self.ws.id, "post_id": self.post.id, "comment_id": comment.id},
        )
        self.assertEqual(self.client.post(url).status_code, 400)
        comment.refresh_from_db()
        self.assertIsNone(comment.deleted_at)
