# atl

`atl` fetches Atlassian (Jira + Confluence) content from the terminal and
renders the HTML the REST APIs return as clean Markdown. It's built for AI
agents: `view` folds a work item or page and its context — comments, links,
sub-tasks, children, attachments — into a single Markdown document, and every
command has an `--output json` mode for scripting.

For anything the curated commands don't cover, `atl api` is a raw, authenticated
passthrough to the Atlassian REST API, modeled on `gh api`: type the real Jira
(`/rest/...`) or Confluence (`/wiki/...`) path against your configured host and
token — reads or writes.

## Install

```sh
uv tool install atl-cli
```

This installs the `atl` command.

## Authentication

Run `atl auth login` once. It prompts for your site URL, your Atlassian email,
and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens),
then verifies them before saving.

- Non-secret metadata (site URL and username) is written to
  `~/.config/atl/credentials.json` (mode 600; honors `XDG_CONFIG_HOME`).
- The API token is stored in your OS keyring; if no keyring backend is available
  it falls back into that same mode-600 file.

`atl auth status` shows the configured site and account and re-verifies the
token; `atl auth logout` removes both the file and the keyring entry.

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
atl auth       login | logout | status
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
