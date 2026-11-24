"""The composer's „Edit in Studio" link (nxt fork): a saved post opens its visual in our hosted HyperFrames Studio
at <STUDIO_URL>/p/<post id>, which also starts a new visual when the post has none yet. No setting, no link; a post
not saved yet has no ID to pass, so no link either."""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

LABEL = "Edit in Studio"


class StudioLinkTests(TestCase):
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

    def _edit(self):
        return self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id})
        )

    @override_settings(STUDIO_URL="https://studio.example.com")
    def test_a_saved_post_links_to_its_visual(self):
        html = self._edit().content.decode()
        self.assertIn(LABEL, html)
        self.assertIn(f'href="https://studio.example.com/p/{self.post.id}"', html)
        self.assertIn('rel="noopener"', html)

    @override_settings(STUDIO_URL="")
    def test_no_setting_no_link(self):
        self.assertNotIn(LABEL, self._edit().content.decode())

    @override_settings(STUDIO_URL="https://studio.example.com")
    def test_a_new_post_has_no_link(self):
        html = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.ws.id})).content.decode()
        self.assertNotIn(LABEL, html)
