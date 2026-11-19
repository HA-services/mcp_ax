# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import secrets
from datetime import timedelta
from html import escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class McpPage(models.Model):
    _name = 'axsync.mcp.page'
    _description = 'MCP Live Page'
    _order = 'id desc'

    name = fields.Char(string='Title', required=True)
    slug = fields.Char(required=True, readonly=True, index=True, copy=False)
    owner_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user, ondelete='cascade')
    mode = fields.Selection([
        ('private', 'Owner only (signed in)'),
        ('login', 'Any signed in user, shown with the viewer\'s own rights'),
        ('link', 'Anyone with the link, shown with the owner\'s rights'),
    ], required=True, default='private')
    state = fields.Selection([
        ('published', 'Published'), ('unpublished', 'Unpublished'), ('expired', 'Expired'),
    ], default='published', required=True, index=True)
    model = fields.Char(required=True)
    domain = fields.Char(default='[]')
    field_names = fields.Char(string='Columns', help='Comma separated field names.')
    group_by = fields.Char()
    measure = fields.Char(help='For example amount_total:sum')
    chart = fields.Selection([('none', 'Table only'), ('bar', 'Table and bar chart')], default='bar')
    limit = fields.Integer(default=100)
    expires_on = fields.Datetime()
    view_count = fields.Integer(readonly=True)
    url = fields.Char(compute='_compute_url')

    @api.depends('slug')
    def _compute_url(self):
        base = self.env['axsync.mcp.engine'].base_url()
        for rec in self:
            rec.url = '%s/mcp/page/%s' % (base, rec.slug) if rec.slug else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault('slug', secrets.token_urlsafe(18))
        return super().create(vals_list)

    def action_unpublish(self):
        self.sudo().write({'state': 'unpublished'})
        return True

    def is_viewable(self):
        self.ensure_one()
        if self.state != 'published':
            return False
        if self.expires_on and self.expires_on < fields.Datetime.now():
            return False
        return True

    def render_html(self, viewer):
        """Return a complete HTML document. viewer is the res.users whose rights are used."""
        self.ensure_one()
        engine = self.env['axsync.mcp.engine'].with_user(viewer)
        try:
            data = engine.page_data(self)
        except Exception as exc:  # pylint: disable=broad-except
            data = {'error': str(exc)}
        title = escape(self.name or '')
        if data.get('error'):
            body = '<p class="err">%s</p>' % escape(data['error'])
        else:
            cols = data['columns']
            head = ''.join('<th>%s</th>' % escape(c) for c in cols)
            rows = ''.join(
                '<tr>%s</tr>' % ''.join('<td>%s</td>' % escape(str(v if v is not None else '')) for v in row)
                for row in data['rows'])
            body = '<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (head, rows)
            if data.get('bars'):
                body = self._svg_bars(data['bars']) + body
        stamp = escape(fields.Datetime.to_string(fields.Datetime.now()))
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">'
            '<title>%s</title><style>'
            'body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f4f6fa;color:#1c2536}'
            'main{max-width:960px;margin:0 auto;padding:32px 20px}'
            'h1{font-size:22px;margin:0 0 4px}.meta{color:#6b7589;font-size:12px;margin-bottom:20px}'
            'table{border-collapse:collapse;width:100%%;background:#fff;border-radius:8px;overflow:hidden;'
            'box-shadow:0 1px 3px rgba(20,30,60,.08)}'
            'th{background:#1c2f57;color:#fff;text-align:left;padding:9px 12px;font-weight:600}'
            'td{padding:8px 12px;border-top:1px solid #e6eaf2}tr:nth-child(even) td{background:#f9fafc}'
            '.err{color:#b3261e}svg{display:block;margin:0 0 20px;background:#fff;border-radius:8px;'
            'box-shadow:0 1px 3px rgba(20,30,60,.08)}</style></head><body><main>'
            '<h1>%s</h1><div class="meta">Live data from Odoo, generated %s UTC. Read only.</div>%s'
            '</main></body></html>' % (title, title, stamp, body))

    @staticmethod
    def _svg_bars(bars):
        bars = bars[:12]
        width, height, pad = 920, 260, 40
        top = max([b[1] for b in bars] + [1])
        step = (width - 2 * pad) / max(len(bars), 1)
        parts = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="Bar chart">' % (width, height)]
        for i, (label, value) in enumerate(bars):
            h = (height - 2 * pad) * (value / top) if top else 0
            x = pad + i * step + step * 0.15
            y = height - pad - h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="#2f5fd0"/>' % (
                x, y, step * 0.7, h))
            parts.append('<text x="%.1f" y="%.1f" font-size="11" text-anchor="middle" fill="#1c2536">%s</text>' % (
                x + step * 0.35, y - 5, escape('%g' % round(value, 2))))
            parts.append('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="#6b7589">%s</text>' % (
                x + step * 0.35, height - pad + 16, escape(str(label)[:16])))
        parts.append('</svg>')
        return ''.join(parts)

    @api.model
    def _cron_expire(self):
        now = fields.Datetime.now()
        self.sudo().search([('state', '=', 'published'), ('expires_on', '!=', False),
                            ('expires_on', '<', now)]).write({'state': 'expired'})
