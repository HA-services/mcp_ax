# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class McpJournal(models.Model):
    _name = 'axsync.mcp.journal'
    _description = 'MCP Change Journal'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', index=True, ondelete='set null')
    token_id = fields.Many2one('axsync.mcp.token', ondelete='set null')
    tool = fields.Char()
    model = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    record_name = fields.Char()
    operation = fields.Selection([
        ('create', 'Created'), ('write', 'Updated'), ('archive', 'Archived'), ('unlink', 'Deleted'),
    ], required=True)
    old_values = fields.Text(readonly=True)
    new_values = fields.Text(readonly=True)
    undoable = fields.Boolean(default=True)
    undone = fields.Boolean(default=False, index=True)
    undone_on = fields.Datetime()
    undone_by = fields.Many2one('res.users', ondelete='set null')
    batch = fields.Char(index=True, help='Groups the rows written by one tool call.')

    def write(self, vals):
        allowed = {'undone', 'undone_on', 'undone_by'}
        if set(vals) - allowed:
            raise UserError(_('The change journal cannot be edited.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('axsync_log_purge'):
            raise UserError(_('The change journal cannot be deleted. It is purged after the retention period.'))
        return super().unlink()

    def action_undo(self):
        engine = self.env['axsync.mcp.engine']
        for rec in self:
            engine.undo_entry(rec, self.env.user)
        return True

    @api.model
    def _cron_purge(self):
        days = self.env['axsync.mcp.setting'].get().journal_retention_days
        if days:
            cutoff = fields.Datetime.now() - timedelta(days=days)
            self.sudo().with_context(axsync_log_purge=True).search([('create_date', '<', cutoff)]).unlink()
