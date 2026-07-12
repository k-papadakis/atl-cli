"""Pydantic models for the Atlassian API responses we consume.

The models are intentionally *lenient*: every value is optional and unknown
fields are ignored. Jira and Confluence responses are large, deeply nested and
configuration-dependent, so this keeps parsing best-effort -- a missing field
becomes `None` rather than raising, which suits a read-only renderer.

The models mirror the API faithfully: an absent value is `None`, never a
placeholder. Display fallbacks ("-", "Unassigned", ...) live in `atl.rendering`.
Structural wrapper objects (`fields`, `body`, ...) default to an empty instance
so traversal stays null-safe while their leaves remain honestly `None`.

Verifying against the OpenAPI specs
-----------------------------------
Atlassian publishes OpenAPI specs. Treat them as a *reference* for a field's
wire name, optionality or type -- not as a code source: generating models from
them yields ~23k lines that forbid unknown fields and type a Jira issue's
`fields` (where `summary`, `status`, custom fields, ... all live) as an untyped
`dict`, i.e. nothing for the part that matters. Fetch and query them with:

    # Jira Cloud platform v3
    curl -sSLO https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json
    # Confluence v1 (pages, comments, content search -- the version we use)
    curl -sSLO https://dac-static.atlassian.com/cloud/confluence/swagger.v3.json
    jq '.components.schemas.Worklog.properties.timeSpent' swagger-v3.v3.json

Every alias and type below was checked this way.
"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ApiModel(BaseModel):
    """Base for every response model: immutable, alias-aware, extra-tolerant."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="ignore", validate_by_name=True, validate_by_alias=True, frozen=True
    )


# --------------------------------------------------------------------------- #
# Shared leaf shapes
# --------------------------------------------------------------------------- #
class Named(ApiModel):
    name: str | None = None


class User(ApiModel):
    display_name: str | None = Field(default=None, alias="displayName")


class Watches(ApiModel):
    watch_count: int = Field(default=0, alias="watchCount")


# --------------------------------------------------------------------------- #
# Jira work item (rendered view)
# --------------------------------------------------------------------------- #
class IssueRefFields(ApiModel):
    summary: str | None = None
    status: Named | None = None


class IssueRef(ApiModel):
    """A referenced work item: a parent, link target or sub-task."""

    key: str | None = None
    fields: IssueRefFields = Field(default_factory=IssueRefFields)


class LinkType(ApiModel):
    inward: str | None = None
    outward: str | None = None


class IssueLink(ApiModel):
    type: LinkType = Field(default_factory=LinkType)
    inward_issue: IssueRef | None = Field(default=None, alias="inwardIssue")
    outward_issue: IssueRef | None = Field(default=None, alias="outwardIssue")


class Attachment(ApiModel):
    id: str | None = None
    filename: str | None = None
    size: int | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")


class ChangeItem(ApiModel):
    field: str | None = None
    from_string: str | None = Field(default=None, alias="fromString")
    to_string: str | None = Field(default=None, alias="toString")


class ChangeHistory(ApiModel):
    created: str | None = None
    author: User | None = None
    items: list[ChangeItem] = Field(default_factory=list)


class Changelog(ApiModel):
    histories: list[ChangeHistory] = Field(default_factory=list)


class IssueFields(ApiModel):
    # Standard fields only; per-instance `customfield_*` are dropped -- see `-o json`.
    summary: str | None = None
    issuetype: Named | None = None
    status: Named | None = None
    priority: Named | None = None
    assignee: User | None = None
    reporter: User | None = None
    created: str | None = None
    updated: str | None = None
    resolution: Named | None = None
    resolution_date: str | None = Field(default=None, alias="resolutiondate")
    due_date: str | None = Field(default=None, alias="duedate")
    watches: Watches = Field(default_factory=Watches)
    parent: IssueRef | None = None
    components: list[Named] = Field(default_factory=list)
    fix_versions: list[Named] = Field(default_factory=list, alias="fixVersions")
    labels: list[str] = Field(default_factory=list)
    issuelinks: list[IssueLink] = Field(default_factory=list)
    subtasks: list[IssueRef] = Field(default_factory=list)
    attachment: list[Attachment] = Field(default_factory=list)


class RenderedFields(ApiModel):
    description: str | None = None


class Issue(ApiModel):
    key: str | None = None
    fields: IssueFields = Field(default_factory=IssueFields)
    rendered_fields: RenderedFields = Field(
        default_factory=RenderedFields, alias="renderedFields"
    )
    changelog: Changelog = Field(default_factory=Changelog)


class RemoteLinkObject(ApiModel):
    title: str | None = None
    url: str | None = None


class RemoteLink(ApiModel):
    object: RemoteLinkObject = Field(default_factory=RemoteLinkObject)


class Worklog(ApiModel):
    started: str | None = None
    time_spent: str | None = Field(default=None, alias="timeSpent")
    author: User | None = None


class WorklogPage(ApiModel):
    worklogs: list[Worklog] = Field(default_factory=list)


class Comment(ApiModel):
    author: User | None = None
    created: str | None = None
    rendered_body: str | None = Field(default=None, alias="renderedBody")


class CommentPage(ApiModel):
    comments: list[Comment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Jira search
# --------------------------------------------------------------------------- #
class SearchIssueFields(ApiModel):
    summary: str | None = None
    status: Named | None = None
    assignee: User | None = None


class SearchIssue(ApiModel):
    key: str | None = None
    fields: SearchIssueFields = Field(default_factory=SearchIssueFields)


class JiraSearchPage(ApiModel):
    """One page of ``/search/jql``: raw issues plus the pagination flags.

    ``issues`` stays as raw ``JsonValue`` so the loop can accumulate the
    unmodified wire objects for ``--json``; the typed ``SearchIssue`` projection
    is built once, from the merged list, for the rendered table.
    """

    issues: list[JsonValue] = Field(default_factory=list)
    is_last: bool | None = Field(default=None, alias="isLast")
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


# --------------------------------------------------------------------------- #
# Confluence page
# --------------------------------------------------------------------------- #
class Body(ApiModel):
    value: str | None = None


class BodyContainer(ApiModel):
    view: Body = Field(default_factory=Body)


class ConfluenceChildPage(ApiModel):
    id: str | None = None
    title: str | None = None


class AttachmentExtensions(ApiModel):
    file_size: int | None = Field(default=None, alias="fileSize")
    media_type: str | None = Field(default=None, alias="mediaType")


class AttachmentLinks(ApiModel):
    # A relative path under the '/wiki' context, e.g.
    # '/rest/api/content/<page>/child/attachment/<att>/download'.
    download: str | None = None


class ConfluenceAttachment(ApiModel):
    id: str | None = None
    title: str | None = None
    extensions: AttachmentExtensions = Field(default_factory=AttachmentExtensions)
    links: AttachmentLinks = Field(default_factory=AttachmentLinks, alias="_links")


class ChildPages(ApiModel):
    results: list[ConfluenceChildPage] = Field(default_factory=list)


class ChildAttachments(ApiModel):
    results: list[ConfluenceAttachment] = Field(default_factory=list)


class Children(ApiModel):
    # Populated when the page is fetched with `expand=children.page,
    # children.attachment`; each collection is capped at the API page size.
    page: ChildPages = Field(default_factory=ChildPages)
    attachment: ChildAttachments = Field(default_factory=ChildAttachments)


class ConfluencePage(ApiModel):
    id: str | None = None
    title: str | None = None
    body: BodyContainer = Field(default_factory=BodyContainer)
    children: Children = Field(default_factory=Children)


class History(ApiModel):
    created_by: User | None = Field(default=None, alias="createdBy")
    created_date: str | None = Field(default=None, alias="createdDate")


class ConfluenceComment(ApiModel):
    history: History = Field(default_factory=History)
    body: BodyContainer = Field(default_factory=BodyContainer)


class ConfluenceComments(ApiModel):
    results: list[ConfluenceComment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Confluence search
# --------------------------------------------------------------------------- #
class SearchPage(ApiModel):
    id: str | None = None
    type: str | None = None
    title: str | None = None


class Links(ApiModel):
    next: str | None = None


class ConfluenceSearchPage(ApiModel):
    """One page of content search: raw results plus the next-page link.

    ``results`` stays raw for the same reason as ``JiraSearchPage.issues``.
    """

    results: list[JsonValue] = Field(default_factory=list)
    links: Links = Field(default_factory=Links, alias="_links")


# --------------------------------------------------------------------------- #
# Error responses
# --------------------------------------------------------------------------- #
class ApiError(ApiModel):
    error_messages: list[str] = Field(default_factory=list, alias="errorMessages")
    # Jira returns field errors as a {field: message} dict; Confluence (v1, the
    # only version we call) uses a single top-level message.
    errors: dict[str, str] = Field(default_factory=dict)
    message: str | None = None
