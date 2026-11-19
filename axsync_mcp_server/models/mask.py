# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class McpMask(models.Model):
    _name = 'axsync.mcp.mask'
    _description = 'MCP Field Mask'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    model_id = fields.Many2one('ir.model', string='Model', ondelete='cascade',
                               help='Empty applies the mask to every model with a field of that name.')
    field_name = fields.Char(string='Field', required=True,
                             help='Technical field name, for example email, vat or standard_price.')
    mode = fields.Selection([
        ('hidden', 'Hidden: never returned, cannot be searched or written'),
        ('partial', 'Partial: show only the ends of the value'),
        ('hash', 'Hash: stable short hash instead of the value'),
    ], required=True, default='hidden')
    exempt_group_ids = fields.Many2many('res.groups', string='Not applied to groups',
                                        help='Users in these groups see the real value.')

    @api.model
    def masks_for(self, user, model):
        """Return {field_name: mode} that apply to this user on this model."""
        result = {}
        rank = {'partial': 1, 'hash': 2, 'hidden': 3}
        user_groups = user.all_group_ids
        for mask in self.sudo().search([]):
            if mask.model_id and mask.model_id.model != model:
                continue
            if mask.exempt_group_ids & user_groups:
                continue
            old = result.get(mask.field_name)
            if not old or rank[mask.mode] > rank[old]:
                result[mask.field_name] = mask.mode
        return result

    def write(self, vals):
        res = super().write(vals)
        self.env['axsync.mcp.engine'].clear_runtime_cache()
        return res
