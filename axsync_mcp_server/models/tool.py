# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class McpTool(models.Model):
    _name = 'axsync.mcp.tool'
    _description = 'MCP Tool'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=False)
    code = fields.Char(required=True, index=True, readonly=True)
    sequence = fields.Integer(default=10)
    kind = fields.Selection([
        ('read', 'Read'), ('write', 'Write'), ('admin', 'Admin'),
    ], required=True, default='read', readonly=True)
    enabled = fields.Boolean(default=True,
                             help='Disabled tools are hidden from tools/list and refuse to run.')
    description = fields.Text(readonly=True)
    call_count = fields.Integer(compute='_compute_call_count')

    _sql_constraints = [('code_uniq', 'unique(code)', 'Tool codes are unique.')]

    def _compute_call_count(self):
        counts = {}
        if self.ids:
            groups = self.env['axsync.mcp.log'].sudo()._read_group_count_by_tool(self.mapped('code'))
            counts = groups
        for rec in self:
            rec.call_count = counts.get(rec.code, 0)

    def write(self, vals):
        res = super().write(vals)
        self.env['axsync.mcp.engine'].clear_runtime_cache()
        return res
