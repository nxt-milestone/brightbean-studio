"""``/api/v1/ideas/*`` — read and write the Ideas board (cards and columns).

Ideas are workspace-wide, not tied to a connected account, so the key's
account allowlist does not narrow them; the workspace does. Every route
needs ``create_posts``, the permission the web board itself requires.
There is no delete: like posts, ideas are removed in the web UI.

The ``save_idea`` / ``save_idea_group`` helpers are the single write path
shared with the MCP idea tools; they raise ``ValueError`` so each surface
maps errors onto its own shape.
"""

from __future__ import annotations

import uuid

from django.db import models, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.routers.posts import _require_perm
from apps.api.schemas import (
    CreateIdeaGroupRequest,
    CreateIdeaRequest,
    IdeaGroupRequest,
    IdeaGroupResponse,
    IdeaGroupsListResponse,
    IdeaRequest,
    IdeaResponse,
    IdeasListResponse,
)
from apps.common.validators import normalize_tags
from apps.composer.models import Idea, IdeaGroup

router = Router(tags=["ideas"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 100


# ---------------------------------------------------------------------------
# Shared with apps.mcp.handlers
# ---------------------------------------------------------------------------


def ideas_qs(workspace_id, *, tag: str | None = None, group_id=None) -> QuerySet[Idea]:
    qs = (
        Idea.objects.filter(workspace_id=workspace_id)
        .select_related("group")
        .prefetch_related("media_attachments")
        .order_by("position", "-created_at", "id")
    )
    if tag:
        qs = qs.filter(tags__contains=[tag])
    if group_id:
        qs = qs.filter(group_id=group_id)
    return qs


def groups_qs(workspace_id) -> QuerySet[IdeaGroup]:
    return IdeaGroup.objects.filter(workspace_id=workspace_id).order_by("position", "created_at")


def save_idea(
    workspace,
    idea: Idea | None = None,
    *,
    author=None,
    title=None,
    description=None,
    tags=None,
    group_id=None,
    media_asset_ids=None,
    post_id=None,
    api_key=None,
) -> Idea:
    """Create (``idea=None``) or update an idea; ``None`` arguments are left unchanged.

    A new idea without ``group_id`` lands in the board's first column, as in
    the web UI. ``media_asset_ids`` replaces the attachments in order (the
    first is the card's cover). ``post_id`` links the post made from this idea
    (one post per idea, as the web board's "create post"); the post must be
    visible to ``api_key`` and not linked to another idea.
    """
    from apps.composer.views import _idea_columns, _sync_idea_media_attachments, _sync_tags_to_model
    from apps.media_library.models import MediaAsset

    if idea is None and not (title or "").strip():
        raise ValueError("title is required.")
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank.")
    if tags is not None:
        tags = normalize_tags(tags)
    group = None
    if group_id is not None:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
        if group is None:
            raise ValueError(f"Idea group {group_id} not found in this workspace.")
    elif idea is None:
        if not IdeaGroup.objects.filter(workspace=workspace).exists():
            _idea_columns(workspace)  # first use: create the default columns, as opening the board does
        group = groups_qs(workspace.id).first()
    media_ids: list[str] | None = None
    if media_asset_ids is not None:
        media_ids = list(dict.fromkeys(str(m) for m in media_asset_ids))
        found = {
            str(i)
            for i in MediaAsset.objects.filter(workspace=workspace, id__in=media_ids).values_list("id", flat=True)
        }
        missing = [m for m in media_ids if m not in found]
        if missing:
            raise ValueError(f"Media asset(s) not in workspace: {missing}")

    post = None
    if post_id is not None:
        from apps.api.routers.posts import visible_posts_qs

        post = visible_posts_qs(api_key).filter(id=post_id).first() if api_key is not None else None
        if post is None:
            raise ValueError(f"Post {post_id} not found.")
        if Idea.objects.filter(post=post).exclude(id=getattr(idea, "id", None)).exists():
            raise ValueError(f"Post {post_id} is already linked to another idea.")

    with transaction.atomic():
        if idea is None:
            idea = Idea(workspace=workspace, author=author, status=Idea.Status.UNASSIGNED)
        if title is not None:
            idea.title = title.strip()
        if description is not None:
            idea.description = description
        if tags is not None:
            idea.tags = tags
        if group is not None:
            idea.group = group
        if post is not None:
            idea.post = post
        idea.save()
        if media_ids is not None:
            _sync_idea_media_attachments(idea, workspace, media_ids)
    _sync_tags_to_model(workspace, tags)
    return ideas_qs(workspace.id).get(id=idea.id)


def save_idea_group(workspace, group: IdeaGroup | None = None, *, name=None, position=None) -> IdeaGroup:
    """Create (``group=None``, appended as the last column) or rename/move a column."""
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be blank.")
    if group is None:
        if name is None:
            raise ValueError("name is required.")
        last = IdeaGroup.objects.filter(workspace=workspace).aggregate(models.Max("position"))["position__max"]
        group = IdeaGroup(workspace=workspace, position=(last or 0) + 1)
    if name is not None:
        group.name = name
    if position is not None:
        if position < 0:
            raise ValueError("position must be >= 0.")
        group.position = position
    group.save()
    return group


# ---------------------------------------------------------------------------
# Routes — groups first so ``/groups`` never reads as an idea id.
# ---------------------------------------------------------------------------


def _read(request: HttpRequest) -> None:
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "create_posts")


def _write(request: HttpRequest) -> None:
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")


@router.get("/groups", response=IdeaGroupsListResponse, summary="List idea columns")
def list_groups(request):
    _read(request)
    log_audit_entry(request, action="idea_group.list", target_id=None, status_code=200)
    return IdeaGroupsListResponse(groups=[IdeaGroupResponse.from_group(g) for g in groups_qs(request.workspace.id)])


@router.post("/groups", response={201: IdeaGroupResponse}, summary="Add an idea column")
def create_group(request, payload: CreateIdeaGroupRequest):
    _write(request)
    try:
        group = save_idea_group(request.workspace, name=payload.name, position=payload.position)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="idea_group.create", target_id=group.id, status_code=201)
    return 201, IdeaGroupResponse.from_group(group)


@router.patch("/groups/{group_id}", response=IdeaGroupResponse, summary="Rename or move an idea column")
def update_group(request, group_id: uuid.UUID, payload: IdeaGroupRequest):
    _write(request)
    group = get_object_or_404(IdeaGroup, id=group_id, workspace_id=request.workspace.id)
    try:
        group = save_idea_group(request.workspace, group, name=payload.name, position=payload.position)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="idea_group.update", target_id=group.id, status_code=200)
    return IdeaGroupResponse.from_group(group)


@router.get("/", response=IdeasListResponse, summary="List ideas")
def list_ideas(
    request,
    tag: str | None = Query(None, description="Only ideas carrying this exact tag."),
    group_id: uuid.UUID | None = Query(None, description="Only ideas in this column."),
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    cursor: str | None = Query(None, description="Opaque cursor from a previous call's next_cursor."),
):
    """Board order: column position, then newest first."""
    _read(request)
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "cursor is not a valid pagination cursor") from exc
    rows = list(ideas_qs(request.workspace.id, tag=tag, group_id=group_id)[offset : offset + limit + 1])
    log_audit_entry(request, action="idea.list", target_id=None, status_code=200)
    return IdeasListResponse(
        ideas=[IdeaResponse.from_idea(i) for i in rows[:limit]],
        limit=limit,
        next_cursor=encode_offset_cursor(offset + limit) if len(rows) > limit else None,
    )


@router.post("/", response={201: IdeaResponse}, summary="Create an idea card")
def create_idea(request, payload: CreateIdeaRequest):
    _write(request)
    try:
        idea = save_idea(
            request.workspace,
            author=request.user if not request.user.is_anonymous else None,
            api_key=request.api_key,  # type: ignore[attr-defined]
            **payload.dict(exclude_unset=True),
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="idea.create", target_id=idea.id, status_code=201)
    return 201, IdeaResponse.from_idea(idea)


@router.get("/{idea_id}", response=IdeaResponse, summary="Read an idea card")
def retrieve_idea(request, idea_id: uuid.UUID):
    _read(request)
    idea = get_object_or_404(ideas_qs(request.workspace.id), id=idea_id)
    log_audit_entry(request, action="idea.read", target_id=idea.id, status_code=200)
    return IdeaResponse.from_idea(idea)


@router.patch("/{idea_id}", response=IdeaResponse, summary="Update an idea card")
def update_idea(request, idea_id: uuid.UUID, payload: IdeaRequest):
    """Only the fields you send change; ``tags`` and ``media_asset_ids`` replace the whole list."""
    _write(request)
    idea = get_object_or_404(Idea, id=idea_id, workspace_id=request.workspace.id)
    try:
        idea = save_idea(
            request.workspace,
            idea,
            api_key=request.api_key,  # type: ignore[attr-defined]
            **payload.dict(exclude_unset=True),
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="idea.update", target_id=idea.id, status_code=200)
    return IdeaResponse.from_idea(idea)
