"""Custom calendar events through the Agent API and MCP."""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.calendar.models import CustomCalendarEvent
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

    return {"ws": ws, "client": client(["create_posts"]), "reader": client([])}


def _send(client, method, path, body=None):
    return getattr(client, method)(path, data=json.dumps(body or {}), content_type="application/json")


@pytest.mark.django_db
class TestCalendarEventsRest:
    def test_create_defaults_end_to_start_and_lists_in_window(self, setup):
        resp = _send(
            setup["client"],
            "post",
            "/api/v1/calendar/events",
            {"title": "Ratgeber: Pflege", "start_date": "2026-12-01"},
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert (body["start_date"], body["end_date"], body["color"]) == ("2026-12-01", "2026-12-01", "#3B82F6")
        _send(setup["client"], "post", "/api/v1/calendar/events", {"title": "Later", "start_date": "2027-02-01"})
        listed = setup["client"].get("/api/v1/calendar/events?start=2026-11-01&end=2026-12-31").json()["events"]
        assert [e["title"] for e in listed] == ["Ratgeber: Pflege"]

    def test_update_and_delete(self, setup):
        event = _send(
            setup["client"], "post", "/api/v1/calendar/events", {"title": "x", "start_date": "2026-12-01"}
        ).json()
        path = f"/api/v1/calendar/events/{event['id']}"
        resp = _send(setup["client"], "patch", path, {"end_date": "2026-12-03", "color": "#FF5733"})
        assert resp.status_code == 200, resp.content
        assert (resp.json()["title"], resp.json()["end_date"], resp.json()["color"]) == ("x", "2026-12-03", "#FF5733")
        assert setup["client"].delete(path).status_code == 204
        assert not CustomCalendarEvent.objects.exists()

    def test_bad_input_is_422(self, setup):
        base = {"title": "x", "start_date": "2026-12-05"}
        assert (
            _send(setup["client"], "post", "/api/v1/calendar/events", {**base, "end_date": "2026-12-01"}).status_code
            == 422
        )
        assert _send(setup["client"], "post", "/api/v1/calendar/events", {**base, "color": "red"}).status_code == 422
        assert (
            _send(setup["client"], "post", "/api/v1/calendar/events", {"start_date": "2026-12-05"}).status_code == 422
        )

    def test_reader_can_list_but_not_write(self, setup):
        assert setup["reader"].get("/api/v1/calendar/events").status_code == 200
        resp = _send(setup["reader"], "post", "/api/v1/calendar/events", {"title": "x", "start_date": "2026-12-01"})
        assert resp.status_code == 403

    def test_other_workspace_event_is_404(self, setup):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=setup["ws"].organization)
        foreign = CustomCalendarEvent.objects.create(
            workspace=other, title="s", start_date="2026-12-01", end_date="2026-12-01"
        )
        assert setup["client"].delete(f"/api/v1/calendar/events/{foreign.id}").status_code == 404
        assert CustomCalendarEvent.objects.filter(id=foreign.id).exists()


@pytest.mark.django_db
def test_mcp_calendar_round_trip(setup):
    def call(name, arguments):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        env = setup["client"].post("/api/v1/mcp/", data=json.dumps(rpc), content_type="application/json").json()
        assert "error" not in env and not env["result"]["isError"], env
        return json.loads(env["result"]["content"][0]["text"])

    event = call("create_calendar_event", {"title": "Ratgeber", "start_date": "2026-12-01", "end_date": "2026-12-02"})
    assert call("update_calendar_event", {"event_id": event["id"], "title": "Ratgeber 2"})["title"] == "Ratgeber 2"
    listed = call("list_calendar_events", {"start_date": "2026-12-02"})["events"]
    assert [e["id"] for e in listed] == [event["id"]]
    assert call("delete_calendar_event", {"event_id": event["id"]}) == {"deleted": True, "event_id": event["id"]}
