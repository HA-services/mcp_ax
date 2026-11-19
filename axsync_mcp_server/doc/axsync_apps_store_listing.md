# Odoo Apps Store listing: copy-ready text (17.0)

## Product title
MCP Server for Odoo
(Manifest name, 19 characters, the Apps Store limit is 25.)

## Short description (summary)
Connect Claude, ChatGPT, Cursor and any MCP client to Odoo with approvals, field masks, undo and an audit log.

## Full product description
MCP Server for Odoo turns your Odoo into a Model Context Protocol server, so AI assistants can search, analyse and update your data under rules you control. Connect developer tools with access tokens, or hosted AI apps with OAuth 2.1 and PKCE. Connections are read only by default. Access levels decide which models, operations and tools a connection may use. Policies allow, ask, require approval by a second person, or block. Field masks hide or shorten sensitive values. Every change is journaled and can be undone, and every call is written to an audit log.

Problem it solves: giving an AI assistant an admin login is unsafe, and simple connectors have no approvals, no masking and no record of what ran.

Who should use it: Odoo administrators, consultants and companies that want AI assistants on top of Odoo with control over what they see and change.

How it works: install the module, open MCP Server > Connections > Connect a client, choose the user and access level, copy the snippet into Claude Code, Claude Desktop, Cursor or curl. For Claude on the web or ChatGPT, add the endpoint as a custom connector and sign in with OAuth.

What makes it different: six server-enforced safety layers, a change journal with undo, approvals with exact arguments, dry-run previews, bulk jobs, live pages, CSV export, a module builder that returns a ZIP, rate limit, write quota and a kill switch.

## Feature list
- MCP over Streamable HTTP, JSON-RPC 2.0, 23 tools, resources and prompts
- Access tokens (hashed, expiry, IP allowlist) and OAuth 2.1 with PKCE
- Access levels: read only, full, custom per model and tool
- Policies: allow, ask, require approval, block
- Approvals by a second person, with exact arguments
- Confirmation with dry-run preview
- Field masks: hidden, partial, hash
- Change journal with undo
- Immutable audit log with graph and pivot
- Bulk jobs, live pages, CSV export
- Module builder (ZIP)
- Rate limit, daily write quota, kill switch
- Connection wizard for Claude Code, Claude Desktop, Cursor and curl

## Keywords
MCP, Model Context Protocol, AI, Claude, ChatGPT, Cursor, OAuth, API, audit log, approvals, automation

## Technical specifications
- Technical name: axsync_mcp_server
- Version: 17.0.1.0.0
- Category: Extra Tools
- License: OPL-1
- Depends: base, web, mail
- Implementation: HTTP controllers for /mcp and OAuth, an engine that runs calls as the real user, no changes to standard files, no external Python libraries
- Testing: automated engine and HTTP tests on Odoo 17.0, a full OAuth flow test, browser checks and 29 live MCP calls against a running server. Enterprise has not been tested.

## Supported versions
Odoo 17.0.

## Pricing
Price: 95.00 USD (one-time, per Odoo Apps Store terms).

## Publisher information
Published by AXSync. Website: https://www.axsync.com

## Support information
Email hunain@axsync.com, or use Contact the Author on the Apps Store page. See SUPPORT.md.
