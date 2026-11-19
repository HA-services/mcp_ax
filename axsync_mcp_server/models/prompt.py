# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import re

from odoo import api, fields, models


class McpPrompt(models.Model):
    _name = 'axsync.mcp.prompt'
    _description = 'MCP Prompt Template'
    _order = 'sequence, id'

    name = fields.Char(string='Code', required=True, help='Short identifier, for example weekly_sales_review.')
    title = fields.Char(required=True)
    description = fields.Char()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    argument_names = fields.Char(
        string='Arguments', help='Comma separated names. Use {name} in the template to insert them.')
    template = fields.Text(required=True)

    _sql_constraints = [('name_uniq', 'unique(name)', 'Prompt codes are unique.')]

    def arguments(self):
        self.ensure_one()
        return [a.strip() for a in (self.argument_names or '').split(',') if a.strip()]

    def render(self, values):
        self.ensure_one()
        text = self.template or ''
        for arg in self.arguments():
            text = text.replace('{%s}' % arg, str((values or {}).get(arg, '')))
        return re.sub(r'\{[a-z_]+\}', '', text)
