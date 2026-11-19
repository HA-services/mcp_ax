# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from html import escape
from urllib.parse import urlencode

from odoo import SUPERUSER_ID, api, fields, http
from odoo.http import Response, request
from odoo.modules.registry import Registry

from ..models.token import hash_secret
from .main import JSON_HEADERS, get_dbname, json_response, public_base


def _oauth_env(cr):
    return api.Environment(cr, SUPERUSER_ID, {})


def _page(title, body, status=200):
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title><style>'
        'body{font:15px/1.55 system-ui,sans-serif;background:#eef1f7;margin:0;color:#1c2536}'
        '.card{max-width:460px;margin:8vh auto;background:#fff;border-radius:12px;padding:32px;'
        'box-shadow:0 6px 24px rgba(20,30,60,.12)}h1{font-size:20px;margin:0 0 12px}'
        'ul{padding-left:20px;color:#44506a}.row{display:flex;gap:10px;margin-top:22px}'
        'button{flex:1;padding:11px;border-radius:8px;border:1px solid #c5cde0;background:#fff;font-size:15px;'
        'cursor:pointer}button.go{background:#1c2f57;color:#fff;border-color:#1c2f57}.muted{color:#6b7589;'
        'font-size:13px}</style></head><body><div class="card">%s</div></body></html>' % (escape(title), body))
    return Response(html, status=status, headers=[('Content-Type', 'text/html; charset=utf-8'),
                                                  ('Cache-Control', 'no-store'),
                                                  ('X-Frame-Options', 'DENY')])


class McpOAuth(http.Controller):

    def _meta_suffix(self):
        db = request.httprequest.args.get('db')
        return '?db=%s' % db if db and not request.db else ''

    @http.route(['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'],
                type='http', auth='none', methods=['GET', 'OPTIONS'], csrf=False, save_session=False, readonly=False)
    def protected_resource(self, **kw):
        db = get_dbname()
        if not db:
            return json_response({'error': 'Database not specified.'}, 400)
        with Registry(db).cursor() as cr:
            env = _oauth_env(cr)
            base = public_base(env)
            sfx = '?db=%s' % db if not request.db else ''
            return json_response({
                'resource': '%s/mcp%s' % (base, sfx),
                'authorization_servers': [base],
                'bearer_methods_supported': ['header'],
                'scopes_supported': ['mcp'],
            }, headers=[('Access-Control-Allow-Origin', '*')])

    @http.route(['/.well-known/oauth-authorization-server', '/.well-known/oauth-authorization-server/mcp'],
                type='http', auth='none', methods=['GET', 'OPTIONS'], csrf=False, save_session=False, readonly=False)
    def authorization_server(self, **kw):
        db = get_dbname()
        if not db:
            return json_response({'error': 'Database not specified.'}, 400)
        with Registry(db).cursor() as cr:
            env = _oauth_env(cr)
            if not env['axsync.mcp.setting'].get().oauth_enabled:
                return json_response({'error': 'OAuth is switched off.'}, 404)
            base = public_base(env)
            sfx = '?db=%s' % db if not request.db else ''
            return json_response({
                'issuer': base,
                'authorization_endpoint': '%s/mcp/oauth/authorize%s' % (base, sfx),
                'token_endpoint': '%s/mcp/oauth/token%s' % (base, sfx),
                'registration_endpoint': '%s/mcp/oauth/register%s' % (base, sfx),
                'revocation_endpoint': '%s/mcp/oauth/revoke%s' % (base, sfx),
                'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code', 'refresh_token'],
                'code_challenge_methods_supported': ['S256'],
                'token_endpoint_auth_methods_supported': ['none'],
                'scopes_supported': ['mcp'],
            }, headers=[('Access-Control-Allow-Origin', '*')])

    @http.route('/mcp/oauth/register', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def register(self, **kw):
        cors = [('Access-Control-Allow-Origin', '*'), ('Access-Control-Allow-Headers', 'Content-Type')]
        if request.httprequest.method == 'OPTIONS':
            return Response('', status=204, headers=cors)
        db = get_dbname()
        if not db:
            return json_response({'error': 'invalid_request'}, 400, cors)
        try:
            body = json.loads(request.httprequest.get_data(cache=False).decode('utf-8') or '{}')
        except Exception:  # pylint: disable=broad-except
            return json_response({'error': 'invalid_client_metadata'}, 400, cors)
        with Registry(db).cursor() as cr:
            env = _oauth_env(cr)
            if not env['axsync.mcp.setting'].get().oauth_enabled:
                return json_response({'error': 'access_denied'}, 403, cors)
            since = fields.Datetime.now() - timedelta(days=1)
            if env['axsync.mcp.client'].with_context(active_test=False).search_count([('registered_on', '>', since)]) > 300:
                return json_response({'error': 'temporarily_unavailable'}, 429, cors)
            try:
                client = env['axsync.mcp.client'].register(body.get('client_name'), body.get('redirect_uris'))
            except Exception:  # pylint: disable=broad-except
                return json_response({'error': 'invalid_redirect_uri'}, 400, cors)
            return json_response({
                'client_id': client.client_id, 'client_name': client.name,
                'redirect_uris': client.redirect_list(), 'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code'], 'token_endpoint_auth_method': 'none',
            }, 201, cors)

    @http.route('/mcp/oauth/authorize', type='http', auth='user', methods=['GET', 'POST'], website=False, readonly=False)
    def authorize(self, **kw):
        params = request.httprequest.values
        env = request.env
        setting = env['axsync.mcp.setting'].sudo().get()
        if not setting.oauth_enabled:
            return _page('Not available', '<h1>OAuth is switched off</h1>', 403)
        client = env['axsync.mcp.client'].sudo().search([('client_id', '=', params.get('client_id', ''))], limit=1)
        redirect_uri = params.get('redirect_uri', '')
        if not client or redirect_uri not in client.redirect_list():
            return _page('Invalid request', '<h1>Invalid request</h1><p>Unknown client or redirect address.</p>', 400)
        state = params.get('state', '')

        def back(**extra):
            q = dict(extra)
            if state:
                q['state'] = state
            sep = '&' if '?' in redirect_uri else '?'
            return request.redirect(redirect_uri + sep + urlencode(q), local=False, code=302)

        if params.get('response_type') != 'code' or params.get('code_challenge_method') != 'S256' \
                or not params.get('code_challenge'):
            return back(error='invalid_request', error_description='PKCE with S256 is required')
        user = env.user
        if not setting.allow_all_internal and not user.has_group('axsync_mcp_server.group_axsync_mcp_user') \
                and not user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
            return _page('No access', '<h1>No access</h1><p>Your Odoo user is not allowed to use MCP. '
                                      'Ask an administrator to add you to the MCP User group.</p>', 403)
        if request.httprequest.method == 'POST':
            if params.get('decision') != 'allow':
                return back(error='access_denied')
            code = secrets.token_urlsafe(32)
            env['axsync.mcp.oauth.code'].sudo().create({
                'code_hash': hash_secret(code), 'client_id': client.id, 'user_id': user.id,
                'redirect_uri': redirect_uri, 'code_challenge': params['code_challenge'],
                'expires_on': fields.Datetime.now() + timedelta(minutes=10),
            })
            return back(code=code)
        profile = setting.default_profile_id
        level = escape(profile.name) if profile else 'Everything your Odoo rights allow'
        hidden = ''.join('<input type="hidden" name="%s" value="%s"/>' % (escape(k), escape(params.get(k, '')))
                         for k in ('client_id', 'redirect_uri', 'response_type', 'code_challenge',
                                   'code_challenge_method', 'state', 'scope'))
        body = (
            '<h1>Allow %s to use Odoo?</h1><p>You are signed in as <b>%s</b>. The application will work under '
            'your own Odoo access rights. Nothing is shared outside what you can already see and do.</p>'
            '<ul><li>Access level: %s</li><li>Writes are journaled and can be undone</li>'
            '<li>Sensitive actions can require an approval</li></ul>'
            '<form method="post"><input type="hidden" name="csrf_token" value="%s"/>%s'
            '<div class="row"><button name="decision" value="deny" type="submit">Deny</button>'
            '<button class="go" name="decision" value="allow" type="submit">Allow</button></div></form>'
            '<p class="muted">You can revoke the connection at any time in Odoo under MCP Server, Tokens.</p>' % (
                escape(client.name), escape(user.name), level, request.csrf_token(), hidden))
        return _page('Authorize', body)

    @http.route('/mcp/oauth/token', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def token(self, **kw):
        cors = [('Access-Control-Allow-Origin', '*'), ('Access-Control-Allow-Headers', 'Content-Type')]
        if request.httprequest.method == 'OPTIONS':
            return Response('', status=204, headers=cors)
        db = get_dbname()
        if not db:
            return json_response({'error': 'invalid_request'}, 400, cors)
        form = request.httprequest.form
        data = {k: form.get(k) for k in form}
        if not data:
            try:
                data = json.loads(request.httprequest.get_data(cache=False).decode('utf-8') or '{}')
            except Exception:  # pylint: disable=broad-except
                data = {}

        def err(code, status=400):
            return json_response({'error': code}, status, cors + [('Cache-Control', 'no-store')])

        with Registry(db).cursor() as cr:
            env = _oauth_env(cr)
            setting = env['axsync.mcp.setting'].get()
            if not setting.oauth_enabled:
                return err('unsupported_grant_type')
            Token = env['axsync.mcp.token']
            grant = data.get('grant_type')
            if grant == 'authorization_code':
                code = env['axsync.mcp.oauth.code'].search([
                    ('code_hash', '=', hash_secret(data.get('code') or '')), ('used', '=', False)], limit=1)
                if not code or code.expires_on < fields.Datetime.now():
                    return err('invalid_grant')
                code.used = True
                client = code.client_id
                if client.client_id != data.get('client_id') or code.redirect_uri != data.get('redirect_uri'):
                    return err('invalid_grant')
                verifier = data.get('code_verifier') or ''
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                if not verifier or digest != code.code_challenge:
                    return err('invalid_grant')
                rec, access = Token.issue(
                    code.user_id, '%s (OAuth)' % client.name, profile=setting.default_profile_id, kind='oauth',
                    client=client, minutes=setting.oauth_access_minutes or 60)
            elif grant == 'refresh_token':
                rec = Token.search([('refresh_hash', '=', hash_secret(data.get('refresh_token') or ''))], limit=1)
                if not rec or not rec.refresh_expires or rec.refresh_expires < fields.Datetime.now():
                    return err('invalid_grant')
                if data.get('client_id') and rec.client_id.client_id != data.get('client_id'):
                    return err('invalid_grant')
                access = 'axmcp_' + secrets.token_urlsafe(32)
                rec.write({'token_hash': hash_secret(access), 'prefix': access[:12],
                           'expires_on': fields.Datetime.now() + timedelta(minutes=setting.oauth_access_minutes or 60)})
            else:
                return err('unsupported_grant_type')
            refresh = secrets.token_urlsafe(40)
            rec.write({'refresh_hash': hash_secret(refresh),
                       'refresh_expires': fields.Datetime.now() + timedelta(days=30)})
            return json_response({
                'access_token': access, 'token_type': 'Bearer',
                'expires_in': (setting.oauth_access_minutes or 60) * 60,
                'refresh_token': refresh, 'scope': 'mcp',
            }, 200, cors + [('Cache-Control', 'no-store')])

    @http.route('/mcp/oauth/revoke', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def revoke(self, **kw):
        cors = [('Access-Control-Allow-Origin', '*'), ('Access-Control-Allow-Headers', 'Content-Type')]
        if request.httprequest.method == 'OPTIONS':
            return Response('', status=204, headers=cors)
        db = get_dbname()
        form = request.httprequest.form
        value = form.get('token') or ''
        if db and value:
            with Registry(db).cursor() as cr:
                env = _oauth_env(cr)
                rec = env['axsync.mcp.token'].search(['|', ('token_hash', '=', hash_secret(value)),
                                                       ('refresh_hash', '=', hash_secret(value))], limit=1)
                if rec:
                    rec.write({'active': False, 'revoked_on': fields.Datetime.now()})
        return Response('', status=200, headers=cors)
