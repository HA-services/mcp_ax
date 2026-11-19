# MCP Server for Odoo

Odoo 16.0 | Category: Extra Tools | License: OPL-1 | Publisher: AXSync (www.axsync.com)

Let AI assistants such as Claude, ChatGPT, Claude Code and Cursor work with your Odoo through the Model Context Protocol (MCP), with controls that keep a person in charge: access levels, policies, approvals, field masks, undo and an audit log.

## Requirements

- Odoo 16.0 with the base, web and Discuss (`mail`) modules. No Python library is added.
- For hosted AI apps (Claude on the web, ChatGPT): an Odoo that is reachable over public HTTPS. For local tools (Claude Code, Cursor, Claude Desktop through mcp-remote) a private network address is enough.
- A reverse proxy that passes `/mcp`, `/.well-known/*` and `/mcp/*` to Odoo and sets `X-Forwarded-Proto`, if you run behind one.

## Installation

1. Copy the `axsync_mcp_server` folder into a directory on your Odoo `addons_path` (or add the Git repository branch `16.0` on Odoo.sh).
2. Restart Odoo and update the Apps list (developer mode, Apps, Update Apps List).
3. Install **MCP Server for Odoo**.
4. Open MCP Server > Configuration > Settings and check the endpoint and the default access level.

On a server with several databases, either use a database filter or add `?db=<name>` to the endpoint address.

## Quick start

1. MCP Server > Connections > Connect a client. Choose the user, the access level and the lifetime. Copy the token once; Odoo stores only a hash.
2. Paste the generated snippet into your client (Claude Code, Claude Desktop, Cursor, curl).
3. Ask the assistant to run `whoami`, then ask a real question about your data.

Hosted AI apps connect with OAuth instead: add the endpoint address as a custom connector, sign in to Odoo and press Allow. OAuth connections get the default access level, which is **Read only** after installation.

## Groups

- **MCP User**: may connect and see their own tokens, journal and approvals.
- **MCP Approver**: may approve or reject the requests of other people.
- **MCP Manager**: manages settings, access levels, policies, masks and sees all logs. Administration Settings includes it.

## Tools (23)

| Tool | Kind | What it does |
|---|---|---|
| `whoami` | read | Show the Odoo user, company, access level, limits and server version this connection uses. Call this first. |
| `list_models` | read | List the models the user can read, optionally filtered by a text query on name or technical name. |
| `describe_model` | read | Return the fields of a model with type, label, required flag, relation, selection values and whether the field is masked, plus the operations this connection may run on it. |
| `search_records` | read | Search and read records of a model. Returns at most the configured maximum per call; use offset to page. |
| `count_records` | read | Count the records matching a domain. |
| `read_records` | read | Read specific records by id. |
| `aggregate` | read | Group records and compute totals, for example group_by ["partner_id"] with measures ["amount_total:sum"]. Date fields accept a granularity: "date_order:month". |
| `name_search` | read | Find records by display name, the way an Odoo many2one field does. |
| `export_records` | read | Export up to 10000 records to a CSV file and return a download link. Masks apply. |
| `create_records` | write | Create one or more records. Every record is written to the change journal and can be undone. |
| `update_records` | write | Update records selected by ids or by a domain. Old and new values are journaled so the change can be undone. |
| `delete_records` | write | Delete records. Records with an active flag are archived (and can be restored with undo_change). Others are removed only when they are in a draft or cancelled state. Posted or confirmed documents are refused. |
| `call_method` | write | Call a public method on records, for example action_confirm on sale.order. Private methods and generic write, create and unlink are refused. Methods cannot be rolled back, so there is no dry run and the default policy asks for confirmation. |
| `undo_change` | write | Undo a journaled change by journal id or by batch id. Refused when the record changed since. |
| `list_changes` | read | List recent changes made through MCP by this user, newest first. |
| `approval_status` | read | Check the state of an approval request raised by a previous call. |
| `list_approvals` | read | List this user's approval requests. |
| `start_bulk_job` | write | Update or archive many records in batches in the background. The record set is fixed when the job starts. Use job_status to follow it. |
| `job_status` | read | Progress of a bulk job. |
| `publish_page` | write | Publish a read-only live page (table with optional bar chart) built from a query. Modes: private (owner only), login (any signed in user, shown with their own rights) and link (anyone with the link, always needs an approval). |
| `list_pages` | read | List the live pages published by this user. |
| `unpublish_page` | write | Take a live page offline. |
| `build_module` | admin | Generate a small installable Odoo module (models, fields, list and form views, menu, access rights) from a specification and return a ZIP download link. Nothing is installed on this server. Models must be named like x_something or vendor.thing. |

Resources: `odoo://help`, `odoo://models`, `odoo://schema/{model}`. Date macros in domains: `@today`, `@today-30d`, `@month_start`, `@year_start`, `@now`.

## How a request is checked

1. The server switch and the tool switch are on.
2. The access level of the connection allows the tool, the model and the operation.
3. The model is not on the blocked list (technical models such as `ir.*`, `base.*`, API keys, mail queue) and the fields are not masked as hidden.
4. Policies are evaluated. The strictest result wins: allow, ask, require approval or block.
5. Ask: the user confirms a dry-run preview. Approve: a second person with the Approver group decides in Odoo.
6. The call runs as the real Odoo user, so standard access rights and record rules still apply.
7. Writes are journaled with the old values (undo) and every call is written to the audit log.

## Access levels, policies and masks

- **Access levels**: Read only, Full (everything the Odoo user may do), or Custom with a list of models and the operations allowed on each, and a list of tools.
- **Policies**: filter by tool, model, operation, user, group, a record condition (domain) and a minimum number of records. Actions: allow, ask, require approval, block. Default policies ask before method calls and bulk jobs and require approval for deletes and module builds.
- **Field masks**: hidden (never returned, cannot be searched, grouped, ordered, exported or written), partial (only the ends of the value) and hash (stable short hash). Masks can exclude groups. Fields whose names look like secrets are masked by default.

## Approvals

A pending request shows the exact arguments. The requester cannot approve their own request. An approver in the right group presses Approve and run and the stored call executes as the requester. Requests expire (72 hours by default) and approvers get an activity.

## Undo

Every create, update and delete is journaled. Undo restores the old values, removes created records and un-archives archived ones. If the record changed after the journaled change, undo is refused unless forced. Deletes archive records that have an active flag; other records are removed only in a draft or cancelled state.

## Bulk jobs and live pages

Bulk jobs update or archive many records in batches of 200 in the background (a cron runs every minute). The set of ids is fixed when the job starts. Live pages are read-only tables with an optional bar chart. Modes: private (owner), login (any signed-in user, rows shown under their own rights) and link (anyone with the link, always needs an approval and an expiry).

## Module builder

`build_module` turns a specification (models, fields, views, menu, access rights) into a ZIP of a small installable Odoo module for this Odoo version. Nothing is installed on the server. The default policy requires an approval.

## Settings

Server switch (kill switch), default access level, whether any internal user may connect, default for writes without a policy, calls per minute (120), writes per day (1000), maximum records per call (200), maximum text size (60000 characters), retention for the audit log (180 days) and journal (365 days), default token lifetime (90 days), approval expiry (72 hours), OAuth switch and token lifetime, default secret masking, allowed browser origins and public base URL.

## Limits

- The transport is stateless request and response over HTTP POST. There is no server push (SSE); GET and DELETE on /mcp answer 405.
- Hosted AI apps need public HTTPS. Odoo Online (SaaS) is not supported because it does not allow custom modules.
- The rate limit counts calls in the audit log in the database. It protects the server from loops, not from a distributed attack; use your proxy for that.
- Method calls (`call_method`) cannot be rolled back and have no dry run. The default policy asks for confirmation.
- Bulk jobs update or archive; they do not create or delete.
- The module builder produces a ZIP; it never installs or runs generated code.
- OAuth with several databases needs a database filter so that the Odoo host name selects the database.
- Journal undo restores the fields that were journaled. Side effects done by Odoo logic (emails, stock moves) are not reversed.
- The assistant can only do what the connected Odoo user and the access level allow. Protect the token like a password.
- Texts are in English and can be translated with the standard Odoo tools.

