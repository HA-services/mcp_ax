# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class McpApproval(models.Model):
    _name = 'axsync.mcp.approval'
    _description = 'MCP Approval Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    requester_id = fields.Many2one('res.users', string='Requested by', required=True, readonly=True,
                                   default=lambda self: self.env.user, index=True)
    token_id = fields.Many2one('axsync.mcp.token', readonly=True, ondelete='set null')
    tool = fields.Char(required=True, readonly=True)
    model = fields.Char(readonly=True)
    operation = fields.Char(readonly=True)
    summary = fields.Text(readonly=True)
    arguments = fields.Text(readonly=True)
    record_count = fields.Integer(readonly=True)
    policy_id = fields.Many2one('axsync.mcp.policy', readonly=True, ondelete='set null')
    state = fields.Selection([
        ('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'),
        ('expired', 'Expired'), ('failed', 'Failed'),
    ], default='pending', required=True, tracking=True, index=True)
    approver_id = fields.Many2one('res.users', string='Decided by', readonly=True)
    decided_on = fields.Datetime(readonly=True)
    decision_note = fields.Text()
    result = fields.Text(readonly=True)
    expires_on = fields.Datetime(readonly=True)
    can_decide = fields.Boolean(compute='_compute_can_decide')

    @api.depends_context('uid')
    @api.depends('requester_id', 'state')
    def _compute_can_decide(self):
        user = self.env.user
        is_approver = user.has_group('axsync_mcp_server.group_axsync_mcp_approver')
        for rec in self:
            rec.can_decide = bool(is_approver and rec.state == 'pending' and rec.requester_id != user)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('axsync.mcp.approval') or 'New'
        return super().create(vals_list)

    @api.model
    def open_request(self, user, token, tool, model, operation, summary, arguments, count, policy):
        hours = self.env['axsync.mcp.setting'].get().approval_expiry_hours
        rec = self.sudo().create({
            'requester_id': user.id,
            'token_id': token.id if token else False,
            'tool': tool, 'model': model, 'operation': operation,
            'summary': summary,
            'arguments': json.dumps(arguments, indent=2, sort_keys=True, default=str),
            'record_count': count,
            'policy_id': policy.id if policy else False,
            'expires_on': fields.Datetime.now() + timedelta(hours=hours) if hours else False,
        })
        group = self.env.ref('axsync_mcp_server.group_axsync_mcp_approver')
        Users = self.env['res.users'].sudo()
        gfield = 'all_group_ids' if 'all_group_ids' in Users._fields else 'groups_id'
        approvers = Users.search([(gfield, 'in', group.ids), ('share', '=', False)]).filtered(
            lambda u: u != user)
        for approver in approvers[:10]:
            rec.activity_schedule(
                'mail.mail_activity_data_todo', user_id=approver.id,
                summary=_('MCP approval needed: %s', tool),
                note=_('%(user)s asked an AI agent to run %(tool)s. %(summary)s',
                       user=user.name, tool=tool, summary=summary or ''))
        rec.message_post(body=_('Approval requested by %s through an AI agent.', user.name))
        return rec

    def _check_decider(self):
        self.ensure_one()
        user = self.env.user
        if not user.has_group('axsync_mcp_server.group_axsync_mcp_approver'):
            raise AccessError(_('Only approvers can decide.'))
        if self.requester_id == user:
            raise AccessError(_('You cannot decide your own request.'))
        if self.state != 'pending':
            raise UserError(_('This request is already %s.', self.state))
        if self.expires_on and self.expires_on < fields.Datetime.now():
            raise UserError(_('This request has expired.'))

    def action_approve(self):
        for rec in self:
            rec._check_decider()
            rec.sudo().write({'state': 'approved', 'approver_id': self.env.user.id,
                              'decided_on': fields.Datetime.now()})
            rec.sudo().activity_ids.unlink()
            result = self.env['axsync.mcp.engine'].sudo().execute_approved(rec.sudo())
            ok = result.get('status') == 'ok'
            rec.sudo().write({
                'state': 'approved' if ok else 'failed',
                'result': json.dumps(result, default=str, indent=2)[:20000],
            })
            rec.message_post(body=_('Approved by %(u)s. Result: %(s)s', u=self.env.user.name,
                                    s=result.get('status')))
        return True

    def action_reject(self):
        for rec in self:
            rec._check_decider()
            rec.sudo().write({'state': 'rejected', 'approver_id': self.env.user.id,
                              'decided_on': fields.Datetime.now()})
            rec.sudo().activity_ids.unlink()
            rec.message_post(body=_('Rejected by %s.', self.env.user.name))
        return True

    @api.model
    def _cron_expire(self):
        now = fields.Datetime.now()
        stale = self.sudo().search([('state', '=', 'pending'), ('expires_on', '!=', False), ('expires_on', '<', now)])
        stale.write({'state': 'expired'})
        stale.activity_ids.unlink()
