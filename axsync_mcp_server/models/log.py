# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class McpLog(models.Model):
    _name = 'axsync.mcp.log'
    _description = 'MCP Audit Log'
    _order = 'id desc'
    _rec_name = 'tool'

    user_id = fields.Many2one('res.users', index=True, ondelete='set null')
    token_id = fields.Many2one('axsync.mcp.token', ondelete='set null')
    client_name = fields.Char(string='Client')
    method = fields.Char(string='MCP method', index=True)
    tool = fields.Char(index=True)
    model = fields.Char(index=True)
    operation = fields.Char()
    is_write = fields.Boolean(index=True)
    status = fields.Selection([
        ('ok', 'OK'), ('error', 'Error'), ('denied', 'Refused'),
        ('pending', 'Waiting'), ('dry_run', 'Dry run'),
    ], index=True)
    message = fields.Text()
    arguments = fields.Text()
    record_count = fields.Integer()
    duration_ms = fields.Integer(string='Duration (ms)')
    ip = fields.Char()
    day = fields.Date(index=True, default=fields.Date.context_today)

    @api.model
    def _read_group_count_by_tool(self, codes):
        self.env.cr.execute(
            'SELECT tool, COUNT(*) FROM axsync_mcp_log WHERE tool IN %s GROUP BY tool', [tuple(codes) or (None,)])
        return dict(self.env.cr.fetchall())

    def write(self, vals):
        if not self.env.context.get('axsync_log_internal'):
            raise UserError(_('The audit log cannot be edited.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('axsync_log_purge'):
            raise UserError(_('The audit log cannot be deleted. It is purged automatically after the retention period.'))
        return super().unlink()

    @api.model
    def _cron_purge(self):
        days = self.env['axsync.mcp.setting'].get().log_retention_days
        if days:
            cutoff = fields.Datetime.now() - timedelta(days=days)
            self.sudo().with_context(axsync_log_purge=True).search([('create_date', '<', cutoff)]).unlink()
