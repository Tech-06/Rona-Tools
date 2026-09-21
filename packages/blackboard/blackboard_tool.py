"""The 5 read-only Blackboard Learn tools the model calls.

Field names and behaviour here come from a live discovery session against a
real Blackboard Ultra tenant (see the plan's Phase 3), not from Blackboard's
public docs -- which turned out not to fully match what the API actually
returns. Three bugs in the draft this replaces only showed up that way:

  * ``bb_get_assignments`` filtered calendar items on
    ``type in ["GradebookColumn", "Assignment", "Assessment"]``. The real
    field is ``itemSourceType``, and every gradable item -- assignments,
    tests, in-class exercises, attendance records alike -- reports the same
    value: ``blackboard.platform.gradebook2.GradableItem``. None of the
    draft's three guesses ever matched anything, so the tool always
    returned an empty list, for every course, unconditionally.
  * Calendar items have no ``id``, ``start`` or ``end`` fields at all -- the
    real names are ``itemSourceId``, ``startDate``, ``endDate``. Both
    ``bb_get_calendar`` and ``bb_get_assignments`` read the wrong keys and
    silently got ``None`` back for every date.
  * ``users/me/preferences/favorite.courses`` doesn't return a
    ``{"results": [...]}`` list the way every other endpoint here does --
    it's ``{"value": "<json-encoded string>", ...}``, a JSON object
    serialized *as a string* inside the JSON response. Reading it as a list
    made ``is_favorite`` false for every course, always.

``status="completed"`` on ``bb_get_assignments`` is gone rather than fixed:
telling completed from pending needs a per-item submission check (the
gradebook's per-column ``attempts`` endpoint), which is deliberately out of
scope here -- see ``bb_get_assignment_detail`` in a later phase. Returning
an empty list for a status this endpoint has no way to know was worse than
just not offering it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from toolbox.custom.blackboard import bb_client, bb_config

_GRADABLE_ITEM_TYPE = "blackboard.platform.gradebook2.GradableItem"

# Blackboard's course-role identifiers (a fixed, documented set).
_ROLE_NAMES = {
    "P": "Instructor",
    "S": "Student",
    "TA": "Teaching Assistant",
    "CB": "Course Builder",
    "GA": "Grader",
    "GS": "Guest",
}

_HANDLER_LABELS = {
    "resource/x-bb-folder": "folder",
    "resource/x-bb-lesson": "lesson",
    "resource/x-bb-blti-link": "external_tool",
    "resource/x-bb-file": "file",
    "resource/x-bb-document": "document",
    "resource/x-bb-assignment": "assignment",
    "resource/x-bb-asmt-test-link": "test",
    "resource/x-bb-externallink": "external_link",
    "resource/x-bb-video": "video",
}


async def bb_get_courses(include_inactive: bool = False) -> dict:
    """Every course the student is enrolled in, with term and favorite info."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        me = await bb_client.get_json(config, "/learn/api/v1/users/me")
        user_id = me.get("id")
        if not user_id:
            return {"success": False, "error": "could not resolve the current user"}

        memberships = await bb_client.get_all_results(
            config,
            f"/learn/api/v1/users/{user_id}/memberships",
            {
                "expand": "course.effectiveAvailability,course.permissions,courseRole",
                "includeCount": "true",
                "limit": 1000,
            },
        )
        favorites = await _load_favorite_course_ids(config)

        courses = []
        for item in memberships:
            course = item.get("course") or {}
            course_id = course.get("id")
            if not course_id:
                continue
            available = bool(course.get("isAvailable"))
            if not available and not include_inactive:
                continue

            term = course.get("term") or {}
            role_id = item.get("role") or ""
            courses.append(
                {
                    "course_id": course_id,
                    "course_code": course.get("courseId"),
                    "name": course.get("name"),
                    "term_name": term.get("name"),
                    "term_id": course.get("termId"),
                    "role": _ROLE_NAMES.get(role_id, role_id or None),
                    "is_available": available,
                    "is_favorite": course_id in favorites,
                }
            )

        return {"success": True, "courses": courses}
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_announcements(course_id: str | None = None, limit: int = 20) -> dict:
    """Announcements across one course, or every course the student is in."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        if course_id:
            course_ids = [course_id]
        else:
            courses_result = await bb_get_courses()
            if not courses_result.get("success"):
                return courses_result
            course_ids = [c["course_id"] for c in courses_result["courses"]]

        semaphore = asyncio.Semaphore(6)
        warnings: list[str] = []

        async def fetch_one(cid: str) -> list[dict]:
            async with semaphore:
                try:
                    # Fetched generously per course; the merged list is
                    # trimmed to `limit` only after every course is in, so a
                    # course processed later never loses out to one
                    # processed first (the draft applied `limit` per course
                    # *and* again to the merge, which is not the same thing).
                    per_course_cap = max(limit, 20)
                    raw = await bb_client.get_all_results(
                        config,
                        f"/learn/api/v1/courses/{cid}/announcements",
                        {"limit": per_course_cap},
                        max_items=per_course_cap,
                    )
                except bb_client.PermissionDeniedError:
                    return []  # this course just doesn't expose announcements to this role
                except bb_client.BlackboardError as exc:
                    warnings.append(f"{cid}: {exc}")
                    return []

                items = []
                for ann in raw:
                    items.append(
                        {
                            "id": ann.get("id"),
                            "title": ann.get("title"),
                            "body": _plain_text(ann.get("body")),
                            "course_id": cid,
                            "created": ann.get("createdDate"),
                        }
                    )
                return items

        per_course = await asyncio.gather(*(fetch_one(cid) for cid in course_ids))
        all_announcements = [a for group in per_course for a in group]
        all_announcements.sort(key=lambda a: a.get("created") or "", reverse=True)

        result = {"success": True, "announcements": all_announcements[:limit]}
        if warnings:
            result["warnings"] = warnings
        return result
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_calendar(days_ahead: int = 30, days_back: int = 0) -> dict:
    """Upcoming (and optionally recent) calendar events across every course."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        tz = _resolve_zoneinfo(config["timezone"])
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=max(0, days_back))
        until = now + timedelta(days=max(0, days_ahead))

        results = await _calendar_items(config, since, until, max_items=500)

        events = [
            {
                "id": item.get("itemSourceId"),
                "title": item.get("title"),
                "start": _render_dt(item.get("startDate"), tz),
                "end": _render_dt(item.get("endDate"), tz),
                "course_id": item.get("calendarId"),
                "type": item.get("itemSourceType"),
                "location": item.get("location"),
            }
            for item in results
        ]
        events.sort(key=lambda e: e.get("start") or "")
        return {"success": True, "timezone": config["timezone"], "events": events}
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_assignments(
    course_id: str | None = None,
    status: str = "all",
    days_back: int = 30,
    days_ahead: int = 60,
) -> dict:
    """Gradable items (assignments, tests, and similar) with due dates,
    inferred from the calendar. ``status`` is only ever 'pending' or
    'overdue' -- telling those apart from 'completed' needs a per-item
    submission check this bulk listing deliberately doesn't do; see
    bb_get_assignment_detail for that."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        tz = _resolve_zoneinfo(config["timezone"])
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=max(0, days_back))
        until = now + timedelta(days=max(0, days_ahead))

        results = await _calendar_items(config, since, until, max_items=1000)

        assignments = []
        for item in results:
            if item.get("itemSourceType") != _GRADABLE_ITEM_TYPE:
                continue
            cid = item.get("calendarId")
            if course_id and cid != course_id:
                continue

            due_raw = item.get("endDate") or item.get("startDate")
            item_status = "pending"
            if due_raw:
                try:
                    due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                    if due_dt < now:
                        item_status = "overdue"
                except ValueError:
                    pass

            if status != "all" and status != item_status:
                continue

            assignments.append(
                {
                    "id": item.get("itemSourceId"),
                    "title": item.get("title"),
                    "course_id": cid,
                    "due_date": _render_dt(due_raw, tz),
                    "status": item_status,
                }
            )

        assignments.sort(key=lambda a: a.get("due_date") or "")
        return {"success": True, "timezone": config["timezone"], "assignments": assignments}
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_course_content(course_id: str, content_id: str = "ROOT") -> dict:
    """Course materials (folders, files, assignments, links) under one
    content node -- ROOT for the top level of the course."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        base_url = config["base_url"]
        tz = _resolve_zoneinfo(config["timezone"])
        results = await bb_client.get_all_results(
            config,
            f"/learn/api/v1/courses/{course_id}/contents/{content_id}/children",
            {"@view": "Summary", "limit": 50},
            max_items=200,
        )
        items = [_summarize_content_item(item, base_url, tz) for item in results]
        return {
            "success": True,
            "course_id": course_id,
            "parent_id": content_id,
            "items": items,
        }
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_item(course_id: str, content_id: str) -> dict:
    """Full detail for one content item: its text, file/download info (if
    any), and due date/points possible (if it's a gradable item) -- more
    than bb_get_course_content's listing carries for any single item."""
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        base_url = config["base_url"]
        tz = _resolve_zoneinfo(config["timezone"])
        item = await bb_client.get_json(
            config, f"/learn/api/v1/courses/{course_id}/contents/{content_id}"
        )
        summary = _summarize_content_item(item, base_url, tz)
        # bb_get_course_content only surfaces `description`, which is
        # usually empty -- the item's actual text lives in `body.rawText`,
        # the same {"rawText": "<html>", ...} shape announcements use.
        summary["text"] = _plain_text(item.get("body")) or summary["description"]

        # A container -- a folder, or an Ultra "lesson" -- has no text of
        # its own: what it holds *is* its children. Returning only an empty
        # body for one reads as "the tool failed", when in fact there was
        # simply nothing there to read, so list the children instead.
        if summary["has_children"]:
            try:
                children = await bb_client.get_all_results(
                    config,
                    f"/learn/api/v1/courses/{course_id}/contents/{content_id}/children",
                    {"@view": "Summary", "limit": 50},
                    max_items=200,
                )
                summary["children"] = [
                    _summarize_content_item(child, base_url, tz) for child in children
                ]
            except bb_client.BlackboardError as exc:
                summary["children_error"] = str(exc)

        return {"success": True, "item": summary}
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_assignment_detail(course_id: str, item_id: str) -> dict:
    """Everything bb_get_item has, plus this student's own submission
    status, attempt date and score.

    ``item_id`` may be either kind of id a caller actually ends up holding,
    because the two other tools hand out different ones: bb_get_course_content
    returns *content* ids, while bb_get_assignments returns *gradebook
    column* ids (a calendar item's itemSourceId). They are not
    interchangeable -- handing a column id to the contents endpoint is a
    flat 403 -- so rather than making the caller know which it has, this
    resolves either.

    Blackboard's gradebook REST API has no field for instructor feedback
    text anywhere this package could find it (checked directly, live,
    against a real graded attempt) -- so it's not in the output. Claiming
    it and always returning nothing would be worse than just not offering
    it.
    """
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        base_url = config["base_url"]
        tz = _resolve_zoneinfo(config["timezone"])
        item, column = await _resolve_gradable(config, course_id, item_id)

        if item is not None:
            summary = _summarize_content_item(item, base_url, tz)
            summary["text"] = _plain_text(item.get("body")) or summary["description"]
            handler = item.get("contentHandler")
            detail = (item.get("contentDetail") or {}).get(handler or "", {}) or {}
            column = detail.get("gradingColumn") or column
        elif column is not None:
            # Only the gradebook column was reachable -- still worth
            # answering with, since its own fields cover most of what a
            # student asking "how did I do on this" wants.
            summary = {
                "id": column.get("contentId") or item_id,
                "title": column.get("columnName"),
                "type": "assignment",
                "description": "",
                "text": "",
                "due_date": _render_dt(column.get("dueDate"), tz),
                "points_possible": column.get("possible"),
            }
        else:
            return {
                "success": False,
                "error": (
                    f"'{item_id}' is neither a readable content item nor a "
                    f"gradebook column in course {course_id}"
                ),
            }

        column = column or {}
        summary["grades_released"] = column.get("gradesReleased")
        column_id = column.get("id") or (item_id if not item else None)
        if column_id:
            summary["submission"] = await _fetch_submission(
                config, course_id, column_id, tz
            )
        else:
            summary["submission"] = {"status": "not_gradable", "score": None}

        return {"success": True, "item": summary}
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


async def bb_get_grades(course_id: str, include_ungraded: bool = False) -> dict:
    """This student's grades for one course: every gradebook column with
    its score, out of how many points, and whether the instructor has
    released it. Only ever this student's own -- the same session that
    reads everything else here has no broader access.

    Blackboard's own gradebook access has turned out to be inconsistent in
    ways unrelated to login state -- one course's columns answered normally
    while another's refused with 403 despite nothing this package did
    differently -- so a course (or a single column within it) that can't be
    read comes back as a clear note rather than failing the whole call.
    """
    config = bb_config.load_config()
    if not bb_config.is_configured(config):
        return bb_config.not_configured_error()

    try:
        me = await bb_client.get_json(config, "/learn/api/v1/users/me")
        user_id = me.get("id")
        if not user_id:
            return {"success": False, "error": "could not resolve the current user"}

        try:
            columns = await bb_client.get_all_results(
                config,
                f"/learn/api/v1/courses/{course_id}/gradebook/columns",
                {"limit": 200},
                max_items=200,
            )
        except bb_client.PermissionDeniedError:
            return {
                "success": False,
                "error": (
                    "grades for this course aren't accessible through the API "
                    "right now -- Blackboard sometimes restricts gradebook "
                    "access per course independently of everything else"
                ),
            }

        semaphore = asyncio.Semaphore(4)
        warnings: list[str] = []

        async def fetch_one(column: dict) -> dict | None:
            async with semaphore:
                submission = await _fetch_submission(
                    config, course_id, column["id"], None, user_id=user_id, warnings=warnings
                )
                return {
                    "column_id": column["id"],
                    "name": column.get("effectiveColumnName") or column.get("columnName"),
                    "possible": column.get("possible"),
                    "grades_released": column.get("gradesReleased"),
                    "score": submission.get("score"),
                    "status": submission.get("status"),
                }

        grades = await asyncio.gather(*(fetch_one(c) for c in columns[:150]))
        grades = list(grades)
        if not include_ungraded:
            grades = [g for g in grades if g["score"] is not None]

        result = {"success": True, "course_id": course_id, "grades": grades}
        if warnings:
            result["warnings"] = warnings
        return result
    except bb_client.BlackboardError as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _calendar_items(
    config: dict, since: datetime, until: datetime, *, max_items: int = 1000
) -> list[dict]:
    """Calendar items that actually fall inside ``[since, until]``.

    This endpoint's own paging cannot be trusted to stay inside the window
    it was asked for: ``paging.nextPage`` carries only ``until`` and
    silently drops ``since``, so following it means asking for "everything
    up to this date" and walking back through years of history. Measured
    live, a 28-day window returned 15 items on page 1 and 307 on page 2,
    reaching back to 2022 -- and because page 2 re-covers page 1's range,
    every page-1 item also came back a second time.

    So the window is enforced here rather than trusted, and items are
    de-duplicated on (source id, start date) -- a key that still keeps two
    genuinely separate occurrences of a recurring item while dropping the
    paging echoes.
    """
    collected: dict[tuple, dict] = {}
    path = "/learn/api/v1/calendars/calendarItems"
    params: dict | None = {"since": _to_bb_iso(since), "until": _to_bb_iso(until)}
    seen_pages: set[str] = set()

    for _ in range(10):  # a hard stop; correctness never depends on reaching it
        data = await bb_client.get_json(config, path, params)
        params = None  # a nextPage is a full URL, query string included

        kept_any = False
        for item in data.get("results", []):
            start = _parse_bb_dt(item.get("startDate") or item.get("endDate"))
            if start is None or not (since <= start <= until):
                continue
            kept_any = True
            collected.setdefault((item.get("itemSourceId"), item.get("startDate")), item)

        next_page = (data.get("paging") or {}).get("nextPage") or None
        if not next_page or next_page in seen_pages or not kept_any:
            break
        if len(collected) >= max_items:
            break
        seen_pages.add(next_page)
        path = next_page

    return list(collected.values())[:max_items]


async def _resolve_gradable(
    config: dict, course_id: str, item_id: str
) -> tuple[dict | None, dict | None]:
    """Turn whichever id the caller has into (content item, gradebook column).

    Tries it as a content id first -- that's what bb_get_course_content
    hands out, and it carries the full detail. Failing that, looks it up as
    a gradebook column (what bb_get_assignments returns) and follows the
    column's own ``contentId`` back to the content item, which is how the
    two id spaces connect. Either half may come back None: a column whose
    content is unreadable still answers most of the question on its own.
    """
    try:
        item = await bb_client.get_json(
            config, f"/learn/api/v1/courses/{course_id}/contents/{item_id}"
        )
        return item, None
    except bb_client.BlackboardError:
        pass

    try:
        column = await bb_client.get_json(
            config, f"/learn/api/v1/courses/{course_id}/gradebook/columns/{item_id}"
        )
    except bb_client.BlackboardError:
        return None, None

    content_id = column.get("contentId")
    if content_id:
        try:
            item = await bb_client.get_json(
                config, f"/learn/api/v1/courses/{course_id}/contents/{content_id}"
            )
            return item, column
        except bb_client.BlackboardError:
            pass
    return None, column


async def _fetch_submission(
    config: dict,
    course_id: str,
    column_id: str,
    tz: ZoneInfo | None,
    *,
    user_id: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """This student's own attempt for one gradebook column, if any.

    The response is keyed by an opaque grade ID, not by user -- but a
    student's own session only ever sees their own attempts here (confirmed
    live: nothing in this package requests anyone else's), so every entry
    in `lookup` already belongs to this student. Matching on `user_id` when
    it's known is still done as a cheap safety check, not because it's
    ever actually expected to filter something out.
    """
    try:
        data = await bb_client.get_json(
            config,
            f"/learn/api/v1/courses/{course_id}/gradebook/columns/{column_id}/attempts",
            use_cache=False,
        )
    except bb_client.PermissionDeniedError:
        return {
            "status": "unavailable",
            "score": None,
            "note": "not accessible through the API for this item right now",
        }
    except bb_client.BlackboardError as exc:
        if warnings is not None:
            warnings.append(f"{column_id}: {exc}")
        return {"status": "unknown", "score": None}

    found = None
    for attempts in (data.get("lookup") or {}).values():
        for attempt in attempts:
            if user_id and attempt.get("userId") != user_id:
                continue
            found = attempt

    if not found:
        return {"status": "not_submitted", "score": None}

    display = found.get("displayGrade") or {}
    result = {"status": found.get("status", "unknown"), "score": display.get("score")}
    if tz is not None:
        result["submitted_at"] = _render_dt(found.get("attemptDate"), tz)
        result["graded_at"] = _render_dt(found.get("attemptLastGradedDate"), tz)
    return result


async def _load_favorite_course_ids(config: dict) -> set[str]:
    """The favorites endpoint doesn't return a {"results": [...]} list like
    everything else here -- it's {"value": "<json-encoded string>"}, a JSON
    object serialized as a string inside the JSON response. Treated as
    optional: a course list is still useful without favorite flags, so any
    failure here is swallowed rather than failing the whole tool call."""
    try:
        raw = await bb_client.get_json(
            config, "/learn/api/v1/users/me/preferences/favorite.courses"
        )
    except bb_client.BlackboardError:
        return set()
    value = raw.get("value")
    if not isinstance(value, str) or not value:
        return set()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, dict):
        return set()
    return {course_id for course_id, flagged in parsed.items() if flagged}


def _is_container(item: dict) -> bool:
    """Whether this item holds other items rather than content of its own.

    ``contentDetail[handler].isFolder`` is the API's own answer and covers
    folders and Ultra lessons alike; it is simply absent on leaf types like
    files and assignments. The handler-name check is only a fallback for
    anything that turns up without it.
    """
    handler = item.get("contentHandler")
    detail = (item.get("contentDetail") or {}).get(handler or "", {}) or {}
    if isinstance(detail, dict) and "isFolder" in detail:
        return bool(detail["isFolder"])
    return handler == "resource/x-bb-folder"


def _friendly_type(handler: str | None) -> str:
    if not handler:
        return "unknown"
    if handler in _HANDLER_LABELS:
        return _HANDLER_LABELS[handler]
    if handler.startswith("resource/x-bb-bltiplacement"):
        return "external_tool"
    return handler


def _summarize_content_item(item: dict, base_url: str, tz: ZoneInfo) -> dict:
    handler = item.get("contentHandler")
    detail = (item.get("contentDetail") or {}).get(handler or "", {}) or {}

    summary: dict = {
        "id": item.get("id"),
        "title": item.get("title"),
        "type": _friendly_type(handler),
        "content_handler": handler,
        # Blackboard marks every container -- a plain folder and an Ultra
        # "lesson" alike -- with isFolder inside its own contentDetail.
        # Matching on the handler name instead (as this did at first) misses
        # lessons, which is exactly why opening one looked empty: a lesson
        # has no body of its own, its content *is* its children.
        "has_children": _is_container(item),
        "description": _plain_text(item.get("description")),
    }

    file_info = detail.get("file")
    if isinstance(file_info, dict) and file_info.get("permanentUrl"):
        summary["file"] = {
            "name": file_info.get("fileName"),
            "size_bytes": file_info.get("fileSize"),
            "mime_type": file_info.get("mimeType"),
            "download_url": f"{base_url}{file_info['permanentUrl']}",
        }

    grading_column = detail.get("gradingColumn")
    if isinstance(grading_column, dict):
        summary["due_date"] = _render_dt(grading_column.get("dueDate"), tz)
        summary["points_possible"] = grading_column.get("possible")

    return summary


def _plain_text(body) -> str:
    """Blackboard represents a rich-text field two different ways depending
    on the endpoint: a plain HTML string on some, or a
    {"rawText": "<html>", "displayText": "<html>", ...} object on others
    (announcements fetched through /learn/api/v1/... are the latter).
    Handles either, and strips the HTML down to plain text either way."""
    if isinstance(body, dict):
        html = body.get("rawText") or body.get("displayText") or ""
    else:
        html = body or ""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()


def _resolve_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- a bad config value shouldn't break a read tool
        return ZoneInfo("UTC")


def _to_bb_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_bb_dt(value: str | None) -> datetime | None:
    """Blackboard's ISO timestamps, as an aware datetime. None when absent
    or unparseable -- callers treat that as "no date", never as epoch."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _render_dt(value: str | None, tz: ZoneInfo) -> str | None:
    parsed = _parse_bb_dt(value)
    if parsed is None:
        return value or None
    return parsed.astimezone(tz).isoformat()
