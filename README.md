# atl

`atl` fetches Atlassian (Jira + Confluence) content from the terminal and
renders the HTML the REST APIs return as clean Markdown. It's built for AI
agents: `view` folds a work item or page and its context — comments, links,
sub-tasks, children, attachments — into a single Markdown document, and every
command has an `--output json` mode for scripting.

For anything the curated commands don't cover, `atl api` is a raw, authenticated
passthrough to the Atlassian REST API, modeled on `gh api`: type the real Jira
(`/rest/...`) or Confluence (`/wiki/...`) path against your configured host and
token — reads or writes (subject to your token's scopes). With one credential
configured it is used for any path; with both Jira and Confluence configured the
product is inferred from the path (override with `--product`). Absolute endpoints
are allowed only for your configured site — or, for a scoped token, its API
gateway — so the stored credentials cannot be sent to another origin.

## Install

```sh
uv tool install atl-cli
```

This installs the `atl` command (also installed as `atl-cli`).

## Authentication

Credentials are stored per Atlassian product. Run `atl auth login jira` and/or
`atl auth login confluence`; each prompts for your site URL, your Atlassian email,
and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens),
then verifies it before saving.

Two token kinds work, detected automatically at login:

- A **classic** ("full access") token talks to your site directly and covers
  both products.
- A **scoped** token ("Create API token with scopes") is locked to a single
  product and reaches the API through Atlassian's gateway. Log in a read-only
  scoped token per product to enforce read-only access — writes are then rejected
  by Atlassian itself, not by the tool.

To mint a read-only scoped token, use "Create API token with scopes", set a name
and expiry, pick the product, then choose **Scope Type: Classic** and select these
**Scope Actions** (or select all of the listed read scopes):

- **Jira** — `Read`.
- **Confluence** — `Read`, `Read Only`, **and `Search`** (the `search:confluence`
  scope is required for `atl confluence search`; without it search returns
  "scope does not match" while `view` still works).

- Non-secret metadata (site URL and username) is written to
  `~/.config/atl-cli/credentials.json` (mode 600; honors `XDG_CONFIG_HOME`).
- The API token is stored in your OS keyring (keyed per product); if no keyring
  backend is available it falls back into that same mode-600 file.

`atl auth status <product>` shows a product's account and re-verifies its token;
`atl auth status` (no product) shows every configured product. `atl auth logout
<product>` removes one product; `atl auth logout` (no product) removes everything.

## Usage

```text
atl jira       view <KEY>                work item + comments, links, sub-tasks,
                                         attachments (+ epic children, for epics)
atl jira       search <jql>              search work items with JQL
atl jira       download-attachment <id>  download a work-item attachment by id
atl confluence view <id>                 wiki page + child pages + attachments
atl confluence search <cql>              search pages with CQL
atl confluence download-attachment <id>  download a page attachment by id
atl            api <endpoint>            raw authenticated REST request to any
                                         Jira/Confluence endpoint (gh-style)
atl auth       login <product>           save/update a product's credentials
atl auth       logout|status [product]   remove/show creds (omit product = all)
```

Common options: `--web` (open in a browser instead of rendering), `--output json`
(raw API JSON for scripting), `--limit <n>` (cap search results; default: all). Run
`atl --help` or `atl <command> --help` for the rest.

### Examples

```sh
atl jira view PROJ-123                        # render a work item as Markdown
atl jira search 'assignee = currentUser() AND statusCategory != Done'
atl confluence view 123456                    # render a page (numeric id)
atl confluence search 'text ~ "onboarding"'
atl jira view PROJ-123 --web                  # open in a browser instead

atl api /rest/api/3/myself                    # raw REST call, pretty-printed JSON
atl api /wiki/api/v2/pages -f limit=5 -X GET  # a field defaults to POST; -X GET forces a query

# write with a raw body (add a Jira comment, in Atlassian Document Format):
echo '{"body":{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"hi"}]}]}}' |
  atl api /rest/api/3/issue/PROJ-123/comment --input -
```
