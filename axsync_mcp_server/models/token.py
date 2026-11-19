# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import hashlib
import ipaddress
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

TOKEN_PREFIX = 'axmcp_'


def hash_secret(value):
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()


class McpToken(models.Model):
    _name = 'axsync.mcp.token'
    _description = 'MCP Access Token'
    _order = 'id desc'

    name = fields.Char(string='Label', required=True)
    user_id = fields.Many2one('res.users', string='Acts as', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', string='Company')
    kind = fields.Selection([('pat', 'Personal token'), ('oauth', 'OAuth connection')],
                            default='pat', required=True)
    prefix = fields.Char(readonly=True)
    token_hash = fields.Char(readonly=True, index=True, copy=False, groups='base.group_system')
    refresh_hash = fields.Char(readonly=True, index=True, copy=False, groups='base.group_system')
    refresh_expires = fields.Datetime(readonly=True)
    profile_id = fields.Many2one('axsync.mcp.profile', string='Access level')
    client_id = fields.Many2one('axsync.mcp.client', string='OAuth client', ondelete='set null')
    expires_on = fields.Datetime(string='Expires')
    ip_allowlist = fields.Text(
        string='Allowed IPs', help='One address or CIDR range per line, for example 203.0.113.7 or 10.0.0.0/24. '
                                   'Empty means any address.')
    active = fields.Boolean(default=True)
    revoked_on = fields.Datetime(readonly=True)
    last_used = fields.Datetime(readonly=True)
    last_ip = fields.Char(readonly=True)
    use_count = fields.Integer(readonly=True)
    state = fields.Selection([
        ('active', 'Active'), ('expired', 'Expired'), ('revoked', 'Revoked')],
        compute='_compute_state', search='_search_state')

    @api.depends('active', 'expires_on')
    def _compute_state(self):
        now = fields.Datetime.now()
        for rec in self:
            if not rec.active:
                rec.state = 'revoked'
            elif rec.expires_on and rec.expires_on < now:
                rec.state = 'expired'
            else:
                rec.state = 'active'

    def _search_state(self, operator, value):
        now = fields.Datetime.now()
        if operator not in ('=', '!='):
            raise UserError(_('Unsupported search.'))
        mapping = {
            'active': [('active', '=', True), '|', ('expires_on', '=', False), ('expires_on', '>=', now)],
            'expired': [('active', '=', True), ('expires_on', '<', now)],
            'revoked': [('active', '=', False)],
        }
        dom = mapping.get(value, [('id', '=', 0)])
        ids = self.with_context(active_test=False).search(dom).ids
        return [('id', 'in' if operator == '=' else 'not in', ids)]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not self.env.su and not vals.get('token_hash') and not self.env.user.has_group('base.group_system'):
                raise AccessError(_('Tokens are created with the token wizard.'))
        return super().create(vals_list)

    @api.model
    def issue(self, user, name, days=None, ips=None, profile=None, company=None,
              kind='pat', client=None, minutes=None):
        """Create a token and return (record, plain secret). The plain value is never stored."""
        user = user.sudo()
        plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires = False
        if minutes:
            expires = fields.Datetime.now() + timedelta(minutes=minutes)
        elif days:
            expires = fields.Datetime.now() + timedelta(days=days)
        vals = {
            'name': name, 'user_id': user.id, 'kind': kind,
            'prefix': plain[:12], 'token_hash': hash_secret(plain),
            'expires_on': expires, 'ip_allowlist': ips or False,
            'profile_id': profile.id if profile else False,
            'company_id': company.id if company else False,
            'client_id': client.id if client else False,
        }
        rec = self.sudo().create(vals)
        return rec, plain

    def action_revoke(self):
        for rec in self:
            if rec.user_id != self.env.user and not self.env.user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
                raise AccessError(_('You can only revoke your own tokens.'))
        self.sudo().write({'active': False, 'revoked_on': fields.Datetime.now()})
        return True

    def ip_allowed(self, ip):
        self.ensure_one()
        lines = [x.strip() for x in (self.ip_allowlist or '').splitlines() if x.strip()]
        if not lines:
            return True
        try:
            addr = ipaddress.ip_address((ip or '').strip())
        except ValueError:
            return False
        for line in lines:
            try:
                if '/' in line:
                    if addr in ipaddress.ip_network(line, strict=False):
                        return True
                elif addr == ipaddress.ip_address(line):
                    return True
            except ValueError:
                continue
        return False

    @api.model
    def find_by_secret(self, plain):
        if not plain:
            return self.browse()
        return self.sudo().search([('token_hash', '=', hash_secret(plain))], limit=1)

    def touch(self, ip):
        self.ensure_one()
        now = fields.Datetime.now()
        if not self.last_used or (now - self.last_used).total_seconds() > 30 or self.last_ip != ip:
            self.sudo().write({'last_used': now, 'last_ip': ip})
        self.env.cr.execute('UPDATE axsync_mcp_token SET use_count = COALESCE(use_count, 0) + 1 WHERE id = %s', [self.id])
        self.invalidate_recordset(['use_count'])

    @api.model
    def _cron_cleanup(self):
        cutoff = fields.Datetime.now() - timedelta(days=30)
        old = self.sudo().with_context(active_test=False).search([
            '|', ('expires_on', '<', cutoff), '&', ('active', '=', False), ('revoked_on', '<', cutoff)])
        old.unlink()
        self.env['axsync.mcp.oauth.code'].sudo().search([('expires_on', '<', fields.Datetime.now())]).unlink()
        stale = fields.Datetime.now() - timedelta(days=7)
        clients = self.env['axsync.mcp.client'].sudo().with_context(active_test=False).search([
            ('registered_on', '<', stale)])
        clients.filtered(lambda c: not c.token_ids).unlink()


class McpClient(models.Model):
    _name = 'axsync.mcp.client'
    _description = 'MCP OAuth Client'
    _order = 'id desc'

    name = fields.Char(required=True)
    client_id = fields.Char(required=True, readonly=True, index=True, copy=False)
    redirect_uris = fields.Text(required=True)
    active = fields.Boolean(default=True)
    registered_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
    token_ids = fields.One2many('axsync.mcp.token', 'client_id', string='Connections')

    _sql_constraints = [('client_uniq', 'unique(client_id)', 'Client ids are unique.')]

    def redirect_list(self):
        self.ensure_one()
        return [x.strip() for x in (self.redirect_uris or '').splitlines() if x.strip()]

    @api.model
    def valid_redirect_uri(self, uri):
        from urllib.parse import urlparse
        try:
            p = urlparse(uri)
        except ValueError:
            return False
        if not p.scheme or p.scheme.lower() in ('javascript', 'data', 'file', 'vbscript', 'about'):
            return False
        if p.scheme == 'http':
            return p.hostname in ('localhost', '127.0.0.1', '::1')
        if p.scheme == 'https':
            return bool(p.hostname)
        return bool(p.scheme)  # custom schemes used by desktop apps

    @api.model
    def register(self, name, redirect_uris):
        uris = [u for u in (redirect_uris or []) if isinstance(u, str)]
        if not uris or not all(self.valid_redirect_uri(u) for u in uris):
            raise UserError(_('Invalid redirect URI.'))
        return self.sudo().create({
            'name': (name or 'MCP client')[:80],
            'client_id': 'axc_' + secrets.token_urlsafe(18),
            'redirect_uris': '\n'.join(uris),
        })


class McpOauthCode(models.Model):
    _name = 'axsync.mcp.oauth.code'
    _description = 'MCP OAuth Authorization Code'

    code_hash = fields.Char(required=True, index=True)
    client_id = fields.Many2one('axsync.mcp.client', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    redirect_uri = fields.Char(required=True)
    code_challenge = fields.Char(required=True)
    expires_on = fields.Datetime(required=True)
    used = fields.Boolean(default=False)
