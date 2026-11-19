# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models
from odoo.tools.safe_eval import safe_eval

ACTION_RANK = {'allow': 0, 'ask': 1, 'approve': 2, 'block': 3}


class McpPolicy(models.Model):
    _name = 'axsync.mcp.policy'
    _description = 'MCP Write Policy'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    action = fields.Selection([
        ('allow', 'Allow'),
        ('ask', 'Ask for confirmation'),
        ('approve', 'Require approval'),
        ('block', 'Block'),
    ], required=True, default='ask')
    operation = fields.Selection([
        ('any', 'Any operation'),
        ('read', 'Read'),
        ('create', 'Create'),
        ('write', 'Update'),
        ('unlink', 'Delete'),
        ('call', 'Method call'),
    ], required=True, default='any')
    tool_ids = fields.Many2many('axsync.mcp.tool', string='Tools',
                                help='Empty means every tool.')
    model_ids = fields.Many2many('ir.model', string='Models', help='Empty means every model.')
    user_ids = fields.Many2many('res.users', string='Users', help='Empty means every user.')
    group_ids = fields.Many2many('res.groups', string='Groups', help='Matches users in any of these groups.')
    domain = fields.Char(
        string='Record condition',
        help='Optional domain. The policy applies when at least one affected record matches it. '
             'Example: [("amount_total", ">", 10000)]')
    min_records = fields.Integer(string='From this many records', default=0,
                                 help='The policy applies only when the call touches at least this many records.')
    message = fields.Char(string='Message to the agent',
                          help='Shown to the AI agent when the policy stops or delays a call.')
    note = fields.Text()

    def matches(self, user, tool_code, model, operation, records, count):
        """records: recordset of affected records (may be empty for creates)."""
        self.ensure_one()
        if self.operation != 'any' and self.operation != operation:
            return False
        if self.tool_ids and tool_code not in self.tool_ids.mapped('code'):
            return False
        if self.model_ids and model not in self.model_ids.mapped('model'):
            return False
        if self.user_ids or self.group_ids:
            user_groups = user.groups_id
            if not (user in self.user_ids or (self.group_ids & user_groups)):
                return False
        if self.min_records and count < self.min_records:
            return False
        if self.domain and self.domain.strip() not in ('[]', ''):
            if not records:
                return False
            try:
                dom = safe_eval(self.domain)
            except Exception:
                return True  # a broken condition fails closed
            hit = records.sudo().filtered_domain(dom)
            if not hit:
                return False
        return True

    @api.model
    def evaluate(self, user, tool_code, model, operation, records, count):
        """Return (action, policy) with the strictest matching policy, or ('allow', empty)."""
        best, best_policy = 'allow', self.browse()
        for policy in self.sudo().search([]):
            if policy.matches(user, tool_code, model, operation, records, count):
                if ACTION_RANK[policy.action] > ACTION_RANK[best] or (not best_policy and policy.action == best):
                    best, best_policy = policy.action, policy
        return best, best_policy

    def write(self, vals):
        res = super().write(vals)
        self.env['axsync.mcp.engine'].clear_runtime_cache()
        return res
