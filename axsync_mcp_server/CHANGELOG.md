# Changelog

## 17.0.1.0.0 (initial release)

- MCP endpoint at /mcp using Streamable HTTP (JSON-RPC 2.0), protocol versions 2025-06-18, 2025-03-26 and 2024-11-05.
- 23 tools for reading, searching, aggregating, exporting, creating, updating, deleting, calling methods, undoing changes, approvals, bulk jobs, live pages and module building.
- Resources (help, model list, model schema) and five default prompts.
- Access tokens with expiry, IP allowlist, per-token access level and revocation. Tokens are stored as hashes.
- OAuth 2.1 with PKCE (S256), dynamic client registration, consent screen and refresh token rotation, for hosted AI apps.
- Access levels (read only, full, custom per model and tool), policies (allow, ask, require approval, block), approvals with a second person, confirmation with dry-run preview.
- Field masks (hidden, partial, hash), default masking of secret-looking fields, blocked technical models.
- Change journal with undo and conflict detection, immutable audit log, usage graph and pivot.
- Rate limit, daily write quota, kill switch, allowed origins.
- Bulk jobs in batches, live pages (private, login, link), CSV export with formula-injection guard, module builder that returns a ZIP.
- Connection wizard with ready-to-paste snippets for Claude Code, Claude Desktop, Cursor and curl.
