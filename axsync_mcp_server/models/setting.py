# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class McpSetting(models.Model):
    _name = 'axsync.mcp.setting'
    _description = 'MCP Server Settings'

    name = fields.Char(default='MCP Server Settings', readonly=True)
    enabled = fields.Boolean(
        string='Server enabled', default=True,
        help='Kill switch. When off, every MCP request is refused with HTTP 503 and no tool runs.')
    default_profile_id = fields.Many2one(
        'axsync.mcp.profile', string='Default access level',
        help='Used by tokens that have no access level of their own.')
    allow_all_internal = fields.Boolean(
        string='Any internal user may connect', default=False,
        help='When off, only members of the MCP User group (and managers) can create tokens or approve OAuth connections.')
    default_write_action = fields.Selection([
        ('allow', 'Run immediately (journaled, undoable)'),
        ('ask', 'Ask for confirmation first'),
        ('approve', 'Require an approval'),
    ], string='Creates and updates without a policy', default='allow', required=True)
    rate_limit = fields.Integer(
        string='Calls per minute and user', default=120,
        help='Tool calls per user per minute. Above this the server answers HTTP 429. 0 means no limit.')
    daily_write_quota = fields.Integer(
        string='Writes per day and user', default=1000,
        help='Number of successful creates, updates and deletes per user and day. 0 means no limit.')
    max_records = fields.Integer(string='Max records per call', default=200)
    max_text_chars = fields.Integer(string='Max response size (characters)', default=60000)
    log_retention_days = fields.Integer(string='Keep audit log for (days)', default=180,
                                        help='0 keeps the log forever.')
    journal_retention_days = fields.Integer(string='Keep change journal for (days)', default=365,
                                            help='0 keeps the journal forever.')
    token_default_days = fields.Integer(string='Default token lifetime (days)', default=90)
    approval_expiry_hours = fields.Integer(string='Approval expires after (hours)', default=72)
    oauth_enabled = fields.Boolean(string='OAuth for Claude and ChatGPT connectors', default=True)
    oauth_access_minutes = fields.Integer(string='OAuth access token lifetime (minutes)', default=60)
    mask_default_secrets = fields.Boolean(
        string='Hide secret-looking fields', default=True,
        help='Fields named like password, secret, token, api key or private key are never returned.')
    allowed_origins = fields.Char(
        string='Allowed browser origins', default='',
        help='Comma separated list of origins allowed to call the endpoint from a browser. Use * for any. '
             'Requests without an Origin header (desktop and CLI clients) are always accepted.')
    public_base_url = fields.Char(
        string='Public base URL',
        help='Optional. Overrides web.base.url in OAuth metadata and download links, for example when Odoo runs behind a proxy.')

    @api.model
    def get(self):
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    @api.model
    def action_open(self):
        rec = self.get()
        return {
            'type': 'ir.actions.act_window',
            'name': _('MCP Server Settings'),
            'res_model': self._name,
            'res_id': rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model_create_multi
    def create(self, vals_list):
        if self.sudo().search_count([]):
            raise UserError(_('There is only one settings record.'))
        return super().create(vals_list)

    def unlink(self):
        raise UserError(_('The settings record cannot be deleted.'))

    def write(self, vals):
        res = super().write(vals)
        self.env['axsync.mcp.engine'].clear_runtime_cache()
        return res
