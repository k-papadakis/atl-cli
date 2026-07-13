"""Tests for the pure rendering core, driven by raw API-shaped fixtures."""

import pytest

from atl_cli.errors import AtlError
from atl_cli.models import Page
from atl_cli.rendering import (
    confluence_search_table,
    format_table,
    jira_search_table,
    render_confluence_page,
    render_jira_issue,
)
from atl_cli.schemas import (
    CommentPage,
    ConfluenceComments,
    ConfluencePage,
    Issue,
    RemoteLink,
    SearchIssue,
    SearchPage,
    WorklogPage,
)

NO_CHILDREN: Page[SearchIssue] = Page([], more=False)

ISSUE = {
    "key": "SYS-1",
    "fields": {
        "summary": "Do the thing",
        "issuetype": {"name": "Task"},
        "status": {"name": "In Progress"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Ada L."},
        "reporter": {"displayName": "Bob"},
        "created": "2026-01-02T09:30:00.000+0000",
        "updated": "2026-01-03T10:00:00.000+0000",
        "resolution": None,
        "watches": {"watchCount": 3},
        "parent": {"key": "SYS-0", "fields": {"summary": "Epic-ish"}},
        "labels": ["a", "b"],
        "issuelinks": [
            {
                "type": {"inward": "is blocked by"},
                "inwardIssue": {
                    "key": "SYS-9",
                    "fields": {"status": {"name": "Open"}, "summary": "Blocker"},
                },
            }
        ],
        "subtasks": [],
        "attachment": [
            {"id": "555", "filename": "log.txt", "size": 2048, "mimeType": "text/plain"}
        ],
    },
    "renderedFields": {"description": "<p>Hello <strong>world</strong></p>"},
    "changelog": {
        "histories": [
            {
                "created": "2026-01-02T09:35:00.000+0000",
                "author": {"displayName": "Ada L."},
                "items": [
                    {
                        "field": "status",
                        "fromString": "To Do",
                        "toString": "In Progress",
                    }
                ],
            }
        ]
    },
}


def test_format_table_aligns_columns_and_strips_trailing_space() -> None:
    """Each column widens to its longest cell; no line keeps trailing padding."""
    out = format_table(["ID", "NAME"], [["1", "Ada"], ["1000", "Bo"]])
    assert out.splitlines() == ["ID    NAME", "1     Ada", "1000  Bo"]
    assert all(not line.endswith(" ") for line in out.splitlines())


def test_format_table_with_no_rows_is_just_the_header() -> None:
    assert format_table(["ID", "NAME"], []) == "ID  NAME"


def test_render_jira_issue_full() -> None:
    out = render_jira_issue(
        Issue.model_validate(ISSUE),
        [
            RemoteLink.model_validate(
                {"object": {"title": "Doc", "url": "http://x/doc"}}
            )
        ],
        WorklogPage.model_validate(
            {
                "worklogs": [
                    {"started": "2026-01-02T00:00:00.000+0000", "timeSpent": "2h"}
                ]
            }
        ),
        CommentPage.model_validate(
            {
                "comments": [
                    {"author": {"displayName": "Bob"}, "renderedBody": "<p>Hi.</p>"}
                ]
            }
        ),
        NO_CHILDREN,
    )
    assert "# SYS-1: Do the thing" in out
    assert "**Assignee:** Ada L." in out
    assert "- is blocked by: **SYS-9** [Open] — Blocker" in out
    assert "- [555] log.txt (2.0 KB)" in out
    assert "Hello **world**" in out
    assert "2026-01-02 09:35: To Do → In Progress (Ada L.)" in out
    assert "[Doc](http://x/doc)" in out


def test_absent_fields_fall_back_to_placeholders() -> None:
    """A near-empty issue must render placeholders, not crash."""
    out = render_jira_issue(
        Issue.model_validate({"key": "SYS-2"}),
        [],
        WorklogPage(),
        CommentPage(),
        NO_CHILDREN,
    )
    assert "# SYS-2: " in out
    assert "**Assignee:** Unassigned" in out
    assert "**Reporter:** -" in out
    assert "*No description.*" in out
    assert "## Comments (0)" in out


def test_resolution_and_due_dates_render_as_date_only() -> None:
    """Resolved/Due show 'YYYY-MM-DD', never a full ISO timestamp."""
    issue = Issue.model_validate(
        {
            "key": "SYS-7",
            "fields": {
                "resolutiondate": "2026-02-03T14:00:00.000+0000",
                "duedate": "2026-02-10",
            },
        }
    )
    out = render_jira_issue(issue, [], WorklogPage(), CommentPage(), NO_CHILDREN)
    assert "**Resolved:** 2026-02-03" in out
    assert "**Due:** 2026-02-10" in out
    assert "T14:00" not in out


def test_status_history_is_sorted_and_filtered() -> None:
    """Only status changes are shown, oldest first, regardless of input order."""
    issue = Issue.model_validate(
        {
            "key": "SYS-4",
            "changelog": {
                "histories": [
                    {
                        "created": "2026-01-03T12:00:00.000+0000",
                        "author": {"displayName": "Ada"},
                        "items": [
                            {
                                "field": "status",
                                "fromString": "In Progress",
                                "toString": "Done",
                            }
                        ],
                    },
                    {
                        "created": "2026-01-02T09:00:00.000+0000",
                        "author": {"displayName": "Bob"},
                        "items": [
                            {
                                "field": "status",
                                "fromString": "To Do",
                                "toString": "In Progress",
                            },
                            {
                                "field": "assignee",
                                "fromString": "Bob",
                                "toString": "Ada",
                            },
                        ],
                    },
                ]
            },
        }
    )
    out = render_jira_issue(issue, [], WorklogPage(), CommentPage(), NO_CHILDREN)
    first = out.index("To Do → In Progress")
    second = out.index("In Progress → Done")
    assert first < second
    assert "Bob → Ada" not in out  # the assignee change is not a status transition


def test_jira_search_table() -> None:
    """Issues map to the right columns, with an Unassigned fallback and no URL."""
    page = Page(
        [
            SearchIssue.model_validate(
                {"key": "SYS-1", "fields": {"status": {"name": "Open"}, "summary": "A"}}
            ),
            SearchIssue.model_validate(
                {
                    "key": "SYS-2",
                    "fields": {
                        "status": {"name": "Done"},
                        "assignee": {"displayName": "Ada"},
                        "summary": "B",
                    },
                }
            ),
        ],
        more=False,
    )
    st = jira_search_table(page)
    # Exact table: four columns (no derivable URL), aligned, Unassigned fallback.
    assert st.table.splitlines() == [
        "KEY    STATUS  ASSIGNEE    SUMMARY",
        "SYS-1  Open    Unassigned  A",
        "SYS-2  Done    Ada         B",
    ]


def test_render_jira_epic_children() -> None:
    """An epic's children render as their own section; a plain issue has none."""
    children = Page(
        [
            SearchIssue.model_validate(
                {"key": "SYS-3", "fields": {"status": {"name": "Done"}, "summary": "C"}}
            )
        ],
        more=False,
    )
    out = render_jira_issue(
        Issue.model_validate({"key": "SYS-1"}),
        [],
        WorklogPage(),
        CommentPage(),
        children,
    )
    assert "## Epic children (1)" in out
    assert "- **SYS-3** [Done] — C" in out

    without = render_jira_issue(
        Issue.model_validate({"key": "SYS-1"}),
        [],
        WorklogPage(),
        CommentPage(),
        NO_CHILDREN,
    )
    assert "Epic children" not in without


def test_render_confluence_children_and_attachments() -> None:
    """Child pages and attachments (with ids and sizes) render as sections."""
    page = ConfluencePage.model_validate(
        {
            "id": "1",
            "title": "Parent",
            "body": {"view": {"value": "<p>body</p>"}},
            "children": {
                "page": {"results": [{"id": "2", "title": "Kid"}]},
                "attachment": {
                    "results": [
                        {
                            "id": "att9",
                            "title": "diagram.png",
                            "extensions": {
                                "fileSize": 666516,
                                "mediaType": "image/png",
                            },
                        }
                    ]
                },
            },
        }
    )
    out = render_confluence_page(page, ConfluenceComments())
    assert "## Child pages (1)" in out
    assert "- [2] Kid" in out
    assert "## Attachments (1)" in out
    assert "- [att9] diagram.png (650.9 KB)" in out


def test_render_confluence_page() -> None:
    page = ConfluencePage.model_validate(
        {
            "id": "42",
            "title": "My Page",
            "body": {"view": {"value": "<h2>S</h2><p>x</p>"}},
        }
    )
    comments = ConfluenceComments.model_validate(
        {
            "results": [
                {
                    "history": {"createdBy": {"displayName": "Zoe"}},
                    "body": {"view": {"value": "<p>Nice.</p>"}},
                }
            ]
        }
    )
    out = render_confluence_page(page, comments)
    assert out.startswith("# My Page")
    assert "## S" in out
    assert "### Zoe — " in out
    assert "Nice." in out


def test_confluence_page_without_body_raises() -> None:
    """A page with no readable body is an error, not an empty render."""
    page = ConfluencePage.model_validate({"id": "42", "title": "Empty"})
    with pytest.raises(AtlError):
        _ = render_confluence_page(page, ConfluenceComments())


def test_confluence_search_table() -> None:
    """Results map to id/type/title columns, with no derivable URL column."""
    page = Page(
        [SearchPage.model_validate({"id": "10", "type": "page", "title": "T"})],
        more=False,
    )
    st = confluence_search_table(page)
    # Exact table: three columns, no derivable viewpage URL.
    assert st.table.splitlines() == ["ID  TYPE  TITLE", "10  page  T"]
