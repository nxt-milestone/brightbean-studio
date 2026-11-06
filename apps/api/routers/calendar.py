"""``/api/v1/calendar/events/*`` — custom calendar events (launches, campaigns, deadlines).

Events are the coloured all-day bars on the workspace calendar. They belong
to the workspace, not to a connected account, so the key's account
allowlist does not narrow them. Reading needs no extra permission (the
calendar is visible to every member); writing needs ``create_posts``, as in
the web calendar.

``save_event`` is the single write path shared with the MCP event tools and
raises ``ValueError`` so each surface maps errors onto its own shape.
"""

from __future__ import annotations

import datetime as dt
import uuid

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.posts import _require_perm
from apps.api.schemas import (
    CalendarEventRequest,
    CalendarEventResponse,
    CalendarEventsListResponse,
    CreateCalendarEventRequest,
)
from apps.calendar.models import CustomCalendarEvent
from apps.common.validators import is_valid_hex_color

router = Router(tags=["calendar"])


def events_qs(workspace_id, *, start: dt.date | None = None, end: dt.date | None = None) -> QuerySet:
    """Events of a workspace, optionally only those overlapping ``[start, end]``."""
    qs = CustomCalendarEvent.objects.filter(workspace_id=workspace_id).order_by("start_date", "created_at", "id")
    if start:
        qs = qs.filter(end_date__gte=start)
    if end:
        qs = qs.filter(start_date__lte=end)
    return qs


def save_event(workspace, event: CustomCalendarEvent | None = None, *, created_by=None, **fields):
    """Create (``event=None``) or update an event; fields left out (or ``None``) stay unchanged.

    Unlike the web form, an end before the start is refused rather than
    silently clamped, so a caller's typo can't land on the wrong day.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    if "title" in fields:
        fields["title"] = fields["title"].strip()
        if not fields["title"]:
            raise ValueError("title cannot be blank.")
    if "color" in fields and not is_valid_hex_color(fields["color"]):
        raise ValueError("color must be a 6-digit hex value like #3B82F6.")
    if event is None:
        event = CustomCalendarEvent(workspace=workspace, created_by=created_by)
        fields.setdefault("end_date", fields.get("start_date"))
    for name, value in fields.items():
        setattr(event, name, value)
    if not event.title or not event.start_date:
        raise ValueError("title and start_date are required.")
    if event.end_date < event.start_date:
        raise ValueError("end_date cannot be before start_date.")
    event.save()
    return event


@router.get("/events", response=CalendarEventsListResponse, summary="List calendar events")
def list_events(
    request,
    start: dt.date | None = Query(None, description="Only events ending on or after this day."),
    end: dt.date | None = Query(None, description="Only events starting on or before this day."),
):
    enforce_http_rate_limits(request, is_write=False)
    events = events_qs(request.workspace.id, start=start, end=end)[:500]
    log_audit_entry(request, action="calendar_event.list", target_id=None, status_code=200)
    return CalendarEventsListResponse(events=[CalendarEventResponse.from_event(e) for e in events])


@router.post("/events", response={201: CalendarEventResponse}, summary="Create a calendar event")
def create_event(request, payload: CreateCalendarEventRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    try:
        event = save_event(
            request.workspace,
            created_by=request.user if not request.user.is_anonymous else None,
            **payload.dict(),
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="calendar_event.create", target_id=event.id, status_code=201)
    return 201, CalendarEventResponse.from_event(event)


@router.get("/events/{event_id}", response=CalendarEventResponse, summary="Read a calendar event")
def retrieve_event(request, event_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    event = get_object_or_404(CustomCalendarEvent, id=event_id, workspace_id=request.workspace.id)
    log_audit_entry(request, action="calendar_event.read", target_id=event.id, status_code=200)
    return CalendarEventResponse.from_event(event)


@router.patch("/events/{event_id}", response=CalendarEventResponse, summary="Update a calendar event")
def update_event(request, event_id: uuid.UUID, payload: CalendarEventRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    event = get_object_or_404(CustomCalendarEvent, id=event_id, workspace_id=request.workspace.id)
    try:
        event = save_event(request.workspace, event, **payload.dict())
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="calendar_event.update", target_id=event.id, status_code=200)
    return CalendarEventResponse.from_event(event)


@router.delete("/events/{event_id}", response={204: None}, summary="Delete a calendar event")
def delete_event(request, event_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    event = get_object_or_404(CustomCalendarEvent, id=event_id, workspace_id=request.workspace.id)
    event_pk = event.id
    event.delete()
    log_audit_entry(request, action="calendar_event.delete", target_id=event_pk, status_code=204)
    return 204, None
