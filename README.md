# atl

`atl` fetches Atlassian (Jira + Confluence) content from the terminal and
renders the HTML the REST APIs return as clean Markdown. It's built for AI
agents: `view` folds a work item or page and its context — comments, links,
sub-tasks, children, attachments — into a single Markdown document, and every
command has an `--output json` mode for `jq`.

It complements Atlassian's official
[`acli`](https://developer.atlassian.com/cloud/acli/guides/install-acli/):
`acli` owns writes, boards and sprints. `atl`'s curated commands stay read-only
and focus on rendering and attachment downloads, while `atl api` is a raw REST
escape hatch for anything they don't cover (including writes).

## Install

```sh
uv tool install git+https://github.com/k-papadakis/atl
```

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
(raw API JSON for `jq`), `--limit <n>` (cap search results; default: all). Run
`atl --help` or `atl <command> --help` for the rest.

First run `atl auth login` and provide the site URL, your Atlassian email, and
an [API token](https://id.atlassian.com/manage-profile/security/api-tokens).

## Raw API

`atl api` is a raw passthrough to the Atlassian REST API, modeled on `gh api`:
one command for the whole site (you type the real REST path — Jira `/rest/...`
or Confluence `/wiki/...` — against one host and token). It reaches any endpoint
the curated commands don't cover, read or write.

```sh
atl api /wiki/api/v2/pages -f limit=1               # GET query param
atl api /rest/api/3/issue/BST-1/comment -f body=hi  # body present -> POST
```

- `-X/--method` — HTTP method (default GET; POST once there's a body).
- `-f/--raw-field key=value` — string field; `-F/--field key=value` — typed
  (`true`/`false`/`null`/ints convert; `@file`/`@-` reads a value).
- `--input <file|->` — raw request body (fields then go to the query string).
- `-H/--header key:value` — add a request header.
