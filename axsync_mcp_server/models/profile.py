# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class McpProfile(models.Model):
    _name = 'axsync.mcp.profile'
    _description = 'MCP Access Level'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    level = fields.Selection([
        ('full', 'Full: everything the user may do'),
        ('read_only', 'Read only'),
        ('custom', 'Custom: only the models listed'),
    ], required=True, default='read_only')
    description = fields.Text()
    line_ids = fields.One2many('axsync.mcp.profile.line', 'profile_id', string='Allowed models')
    tool_ids = fields.Many2many('axsync.mcp.tool', string='Allowed tools',
                                help='Leave empty to allow every enabled tool.')
    enforced = fields.Boolean(
        string='Always applies', default=False,
        help='When on, this level is applied on top of the token level to the users and groups below. '
             'When several levels apply, the strictest result wins.')
    user_ids = fields.Many2many('res.users', string='Users')
    group_ids = fields.Many2many('res.groups', string='Groups')

    def allows_tool(self, code):
        self.ensure_one()
        if self.tool_ids and code not in self.tool_ids.mapped('code'):
            return False
        if self.level == 'read_only' and self.env['axsync.mcp.tool'].sudo().search(
                [('code', '=', code)], limit=1).kind != 'read':
            return False
        return True

    def allows(self, model, operation):
        """operation: read, create, write, unlink, call."""
        self.ensure_one()
        if self.level == 'full':
            return True
        if self.level == 'read_only':
            return operation == 'read'
        for line in self.line_ids:
            if line.model_name == model:
                return bool(line['can_' + operation])
        return False

    @api.model
    def effective_for(self, user, token_profile, default_profile):
        profiles = token_profile or default_profile or self.browse()
        enforced = self.sudo().search([('enforced', '=', True)])
        for prof in enforced:
            if user in prof.user_ids or (prof.group_ids & user.groups_id):
                profiles |= prof
        return profiles


class McpProfileLine(models.Model):
    _name = 'axsync.mcp.profile.line'
    _description = 'MCP Access Level Line'

    profile_id = fields.Many2one('axsync.mcp.profile', required=True, ondelete='cascade')
    model_id = fields.Many2one('ir.model', string='Model', required=True, ondelete='cascade')
    model_name = fields.Char(string='Technical name', related='model_id.model', store=True, index=True)
    can_read = fields.Boolean(string='Read', default=True)
    can_create = fields.Boolean(string='Create')
    can_write = fields.Boolean(string='Update')
    can_unlink = fields.Boolean(string='Delete')
    can_call = fields.Boolean(string='Call methods')

    _sql_constraints = [('model_uniq', 'unique(profile_id, model_id)', 'A model can appear once per level.')]
