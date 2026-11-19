# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class McpJob(models.Model):
    _name = 'axsync.mcp.job'
    _description = 'MCP Bulk Job'
    _order = 'id desc'

    name = fields.Char(required=True)
    user_id = fields.Many2one('res.users', required=True, index=True, ondelete='cascade')
    token_id = fields.Many2one('axsync.mcp.token', ondelete='set null')
    model = fields.Char(required=True)
    operation = fields.Selection([('write', 'Update'), ('archive', 'Archive')], required=True)
    record_ids = fields.Text(required=True, help='JSON list of record ids, fixed when the job was started.')
    values = fields.Text()
    state = fields.Selection([
        ('running', 'Running'), ('done', 'Done'), ('failed', 'Failed'), ('cancelled', 'Cancelled'),
    ], default='running', required=True, index=True)
    total = fields.Integer(readonly=True)
    done = fields.Integer(readonly=True)
    failed = fields.Integer(readonly=True)
    batch_size = fields.Integer(default=200)
    errors = fields.Text(readonly=True)
    progress = fields.Float(compute='_compute_progress')

    @api.depends('total', 'done', 'failed')
    def _compute_progress(self):
        for rec in self:
            rec.progress = 100.0 * (rec.done + rec.failed) / rec.total if rec.total else 100.0

    def action_cancel(self):
        self.filtered(lambda j: j.state == 'running').sudo().write({'state': 'cancelled'})
        return True

    @api.model
    def _cron_run(self):
        jobs = self.sudo().search([('state', '=', 'running')], order='id', limit=5)
        for job in jobs:
            self.env.cr.execute('SAVEPOINT axsync_job')
            try:
                self.env['axsync.mcp.engine'].run_job_batch(job)
                self.env.cr.execute('RELEASE SAVEPOINT axsync_job')
            except Exception as exc:  # pylint: disable=broad-except
                self.env.cr.execute('ROLLBACK TO SAVEPOINT axsync_job')
                self.env.invalidate_all()
                job.write({'state': 'failed', 'errors': str(exc)[:2000]})
