"""Pure renderers: typed API models in, Markdown or an aligned table out.

Nothing here performs I/O, so every function is deterministic and trivially
testable by constructing the `atl.schemas` models from plain fixtures. Display
fallbacks for absent (`None`) values are applied here, not in the models.
"""

from dataclasses import dataclass

from markdownify import markdownify

from atl_cli.errors import AtlError
from atl_cli.models import Page
from atl_cli.schemas import (
    Changelog,
    Children,
    CommentPage,
    ConfluenceComments,
    ConfluencePage,
    Issue,
    IssueFields,
    IssueRef,
    Named,
    RemoteLink,
    RenderedFields,
    SearchIssue,
    SearchPage,
    User,
    WorklogPage,
)


# --------------------------------------------------------------------------- #
# Formatting helpers (apply display fallbacks for absent values)
# --------------------------------------------------------------------------- #
def html_to_md(html: str) -> str:
    """Convert an HTML fragment to Markdown."""
    return markdownify(html, heading_style="ATX").strip()


PLACEHOLDER = "-"


def text(value: str | None, default: str = PLACEHOLDER) -> str:
    return value if value is not None else default


def named(value: Named | None, default: str = PLACEHOLDER) -> str:
    return text(value.name, default) if value is not None else default


def user_name(user: User | None, default: str = PLACEHOLDER) -> str:
    return text(user.display_name, default) if user is not None else default


def fmt_dt(value: str | None) -> str:
    """Render an ISO timestamp as 'YYYY-MM-DD HH:MM'."""
    return value[:16].replace("T", " ") if value else ""


def fmt_date(value: str | None) -> str:
    """Render an ISO timestamp as its 'YYYY-MM-DD' date part."""
    return value[:10] if value else ""


def fmt_size(num_bytes: int | None) -> str:
    """Render a byte count as a human-readable size (e.g. '184.6 KB')."""
    if num_bytes is None:
        return "?"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def join_sections(sections: list[str]) -> str:
    return "\n\n---\n\n".join(s for s in sections if s)


def render_comments(entries: list[tuple[str, str, str]]) -> str:
    """Render a '## Comments (n)' section from (author, ISO date, body HTML)."""
    blocks = [f"## Comments ({len(entries)})"]
    blocks += [
        f"### {author} — {fmt_dt(date)}\n\n{html_to_md(body)}"
        for author, date, body in entries
    ]
    return "\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class SearchTable:
    """Neutral tabular data for a search result: a grid the console lays out.

    ``headers`` are the column titles and ``rows`` the string cells, one list per
    row aligned to ``headers``. ``id_col`` is the index of the identifier column,
    which the console highlights. ``more`` flags that the server had further
    results beyond this page.
    """

    headers: list[str]
    rows: list[list[str]]
    id_col: int
    more: bool


def search_summary(count: int, more: bool, link_pattern: str) -> str:
    """One-line result summary carrying the link pattern once.

    The search tables omit the per-row URL column because each row's link is
    derivable; this line supplies the pattern instead. `link_pattern` is a whole
    URL template (e.g. 'https://site/browse/<KEY>'): substitute <KEY>/<ID> to
    rebuild any row's link.
    """
    suffix = " (more available; raise --limit)" if more else ""
    return f"{count} result(s){suffix} · links: {link_pattern}"


# --------------------------------------------------------------------------- #
# Jira work items
# --------------------------------------------------------------------------- #
def render_jira_issue(
    issue: Issue,
    remote_links: list[RemoteLink],
    worklog: WorklogPage,
    comments: CommentPage,
    epic_children: Page[SearchIssue],
) -> str:
    return join_sections(
        [
            _jira_header(issue),
            _jira_description(issue.rendered_fields),
            _jira_references(issue.fields),
            _jira_epic_children(epic_children),
            _jira_status_history(issue.changelog),
            _jira_web_links(remote_links),
            _jira_worklogs(worklog),
            _jira_comments(comments),
        ]
    )


def _jira_header(issue: Issue) -> str:
    f = issue.fields
    lines = [
        f"# {text(issue.key)}: {text(f.summary, '')}",
        " | ".join(
            [
                f"**Type:** {named(f.issuetype)}",
                f"**Status:** {named(f.status)}",
                f"**Priority:** {named(f.priority)}",
                f"**Assignee:** {user_name(f.assignee, 'Unassigned')}",
                f"**Reporter:** {user_name(f.reporter)}",
            ]
        ),
    ]

    dates = [
        f"**Created:** {fmt_dt(f.created)}",
        f"**Updated:** {fmt_dt(f.updated)}",
    ]
    if f.resolution:
        dates.append(f"**Resolution:** {named(f.resolution)}")
    if f.resolution_date:
        dates.append(f"**Resolved:** {fmt_date(f.resolution_date)}")
    if f.due_date:
        dates.append(f"**Due:** {fmt_date(f.due_date)}")
    dates.append(f"**Watchers:** {f.watches.watch_count}")
    lines.append(" | ".join(dates))

    if f.parent:
        lines.append(
            f"**Parent:** {text(f.parent.key)} — {text(f.parent.fields.summary)}"
        )
    if f.components:
        lines.append("**Components:** " + ", ".join(named(c) for c in f.components))
    if f.fix_versions:
        lines.append("**Fix Versions:** " + ", ".join(named(v) for v in f.fix_versions))
    if f.labels:
        lines.append("**Labels:** " + ", ".join(f.labels))

    return "\n\n".join(lines)


def _jira_description(rendered: RenderedFields) -> str:
    body = rendered.description
    return "## Description\n\n" + (html_to_md(body) if body else "*No description.*")


def _jira_status_history(changelog: Changelog) -> str:
    transitions: list[tuple[str, str, str, str]] = [
        (
            text(history.created, ""),
            user_name(history.author),
            text(item.from_string),
            text(item.to_string),
        )
        for history in changelog.histories
        for item in history.items
        if item.field == "status"
    ]
    if not transitions:
        return ""
    # Jira Cloud stamps these in a single uniform UTC offset, so a lexicographic
    # sort over the raw ISO strings is also chronological.
    transitions.sort(key=lambda t: t[0])
    lines = ["## Status history", ""]
    lines += [
        f"- {fmt_dt(when)}: {frm} → {to} ({who})" for when, who, frm, to in transitions
    ]
    return "\n".join(lines)


def _jira_references(fields: IssueFields) -> str:
    blocks: list[str] = []

    if fields.issuelinks:
        items: list[str] = []
        for link in fields.issuelinks:
            if link.inward_issue:
                rel, ref = text(link.type.inward), link.inward_issue
            else:
                rel, ref = text(link.type.outward), link.outward_issue or IssueRef()
            status = named(ref.fields.status)
            summary = text(ref.fields.summary)
            items.append(f"- {rel}: **{text(ref.key)}** [{status}] — {summary}")
        blocks.append("## Linked work items\n\n" + "\n".join(items))

    if fields.subtasks:
        items = [
            f"- **{text(sub.key)}** [{named(sub.fields.status)}] — "
            + text(sub.fields.summary)
            for sub in fields.subtasks
        ]
        blocks.append("## Sub-tasks\n\n" + "\n".join(items))

    if fields.attachment:
        # The id is what the `download-attachment` command takes.
        items = [
            f"- [{text(a.id)}] {text(a.filename)} ({fmt_size(a.size)})"
            for a in fields.attachment
        ]
        blocks.append("## Attachments\n\n" + "\n".join(items))

    return join_sections(blocks)


def _jira_epic_children(children: Page[SearchIssue]) -> str:
    if not children.items:
        return ""
    items = [
        f"- **{text(c.key)}** [{named(c.fields.status)}] — {text(c.fields.summary)}"
        for c in children.items
    ]
    return f"## Epic children ({len(children.items)})\n\n" + "\n".join(items)


def _jira_web_links(remote_links: list[RemoteLink]) -> str:
    if not remote_links:
        return ""
    items = [
        f"- [{text(link.object.title)}]({text(link.object.url)})"
        for link in remote_links
    ]
    return "## Web links\n\n" + "\n".join(items)


def _jira_worklogs(worklog: WorklogPage) -> str:
    if not worklog.worklogs:
        return ""
    items = [
        f"- {text(w.started)[:10]}: **{text(w.time_spent)}** — {user_name(w.author)}"
        for w in worklog.worklogs
    ]
    return "## Worklogs\n\n" + "\n".join(items)


def _jira_comments(comments: CommentPage) -> str:
    return render_comments(
        [
            (user_name(c.author), text(c.created, ""), text(c.rendered_body, ""))
            for c in comments.comments
        ]
    )


def jira_search_table(page: Page[SearchIssue]) -> SearchTable:
    # No URL column: a row's link is derivable from KEY, so the command layer
    # prints the '{base}/browse/<KEY>' pattern once instead of on every row.
    rows: list[list[str]] = [
        [
            text(issue.key),
            named(issue.fields.status),
            user_name(issue.fields.assignee, "Unassigned"),
            text(issue.fields.summary),
        ]
        for issue in page.items
    ]
    headers = ["KEY", "STATUS", "ASSIGNEE", "SUMMARY"]
    return SearchTable(headers=headers, rows=rows, id_col=0, more=page.more)


# --------------------------------------------------------------------------- #
# Confluence pages
# --------------------------------------------------------------------------- #
def _confluence_children(children: Children) -> str:
    pages = children.page.results
    if not pages:
        return ""
    items = [f"- [{text(p.id)}] {text(p.title)}" for p in pages]
    return f"## Child pages ({len(pages)})\n\n" + "\n".join(items)


def _confluence_attachments(children: Children) -> str:
    # The id is what the `download-attachment` command takes.
    atts = children.attachment.results
    if not atts:
        return ""
    items = [
        f"- [{text(a.id)}] {text(a.title)} ({fmt_size(a.extensions.file_size)})"
        for a in atts
    ]
    return f"## Attachments ({len(atts)})\n\n" + "\n".join(items)


def render_confluence_page(page: ConfluencePage, comments: ConfluenceComments) -> str:
    body = page.body.view.value
    if not body:
        raise AtlError(f"Page {text(page.id, '?')} has no content.")

    comments_md = render_comments(
        [
            (
                user_name(c.history.created_by),
                text(c.history.created_date, ""),
                text(c.body.view.value, ""),
            )
            for c in comments.results
        ]
    )

    page_md = f"# {text(page.title)}\n\n" + html_to_md(body)
    return join_sections(
        [
            page_md,
            _confluence_children(page.children),
            _confluence_attachments(page.children),
            comments_md,
        ]
    )


def confluence_search_table(page: Page[SearchPage]) -> SearchTable:
    # No URL column: a row's link is derivable from ID, so the command layer
    # prints the viewpage pattern once instead of on every row.
    rows: list[list[str]] = [
        [text(result.id), text(result.type), text(result.title)]
        for result in page.items
    ]
    headers = ["ID", "TYPE", "TITLE"]
    return SearchTable(headers=headers, rows=rows, id_col=0, more=page.more)
