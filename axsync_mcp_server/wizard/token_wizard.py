# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class McpTokenWizard(models.TransientModel):
    _name = 'axsync.mcp.token.wizard'
    _description = 'Create MCP Token'

    name = fields.Char(string='Label', required=True, default='My AI assistant')
    user_id = fields.Many2one('res.users', string='Acts as', required=True, default=lambda self: self.env.user)
    profile_id = fields.Many2one('axsync.mcp.profile', string='Access level',
                                 default=lambda self: self.env['axsync.mcp.setting'].get().default_profile_id)
    days = fields.Integer(string='Valid for (days)',
                          default=lambda self: self.env['axsync.mcp.setting'].get().token_default_days)
    ip_allowlist = fields.Text(string='Allowed IPs')
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    secret = fields.Char(string='Token', readonly=True)
    endpoint = fields.Char(readonly=True)
    snippets = fields.Text(readonly=True)

    @api.constrains('user_id')
    def _check_user(self):
        for rec in self:
            if rec.user_id != self.env.user and not self.env.user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
                raise AccessError(_('Only managers can create tokens for other users.'))

    def action_generate(self):
        self.ensure_one()
        if self.user_id != self.env.user and not self.env.user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
            raise AccessError(_('Only managers can create tokens for other users.'))
        setting = self.env['axsync.mcp.setting'].get()
        if not setting.allow_all_internal and not self.user_id.has_group('axsync_mcp_server.group_axsync_mcp_user') \
                and not self.user_id.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
            raise UserError(_('%s is not a member of the MCP User group.', self.user_id.name))
        token, plain = self.env['axsync.mcp.token'].issue(
            self.user_id, self.name, days=self.days or None, ips=self.ip_allowlist, profile=self.profile_id)
        base = self.env['axsync.mcp.engine'].base_url()
        db = self.env.cr.dbname
        url = '%s/mcp' % base
        multi = self._multi_db()
        if multi:
            url += '?db=%s' % db
        self.write({'state': 'done', 'secret': plain, 'endpoint': url, 'snippets': self._snippets(url, plain)})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}

    @api.model
    def _multi_db(self):
        from odoo.tools import config
        try:
            try:
                from odoo.service.db import list_dbs
            except ImportError:
                from odoo.modules.db import list_dbs
            return len(list_dbs(True)) > 1 and not config['dbfilter']
        except Exception:  # pylint: disable=broad-except
            return False

    @api.model
    def _snippets(self, url, token):
        desktop = json.dumps({'mcpServers': {'odoo': {
            'command': 'npx', 'args': ['-y', 'mcp-remote', url, '--header', 'Authorization: Bearer ' + token]}}}, indent=2)
        cursor = json.dumps({'mcpServers': {'odoo': {'url': url, 'headers': {'Authorization': 'Bearer ' + token}}}},
                            indent=2)
        return (
            'Claude Code (terminal):\nclaude mcp add --transport http odoo %s --header "Authorization: Bearer %s"\n\n'
            'Claude Desktop (claude_desktop_config.json):\n%s\n\n'
            'Cursor or VS Code (mcp.json):\n%s\n\n'
            'Any HTTP client:\ncurl -s %s -H "Authorization: Bearer %s" -H "Content-Type: application/json" '
            '-d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\'\n\n'
            'Claude and ChatGPT connectors: add %s as a custom connector and sign in with Odoo when asked. '
            'No token is needed.' % (url, token, desktop, cursor, url, token, url))
