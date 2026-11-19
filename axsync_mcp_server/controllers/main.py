# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
import logging
from datetime import timedelta
from urllib.parse import urlparse

from odoo import SUPERUSER_ID, api, fields, http
from odoo.http import Response, request
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

JSON_HEADERS = [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')]
MAX_BODY = 2 * 1024 * 1024
MAX_BATCH = 20


def json_response(payload, status=200, headers=None):
    body = json.dumps(payload, default=str, ensure_ascii=False)
    return Response(body, status=status, headers=JSON_HEADERS + (headers or []))


def get_dbname():
    db = request.httprequest.args.get('db') or request.httprequest.headers.get('X-Odoo-Database')
    if db:
        return db
    try:
        return request.db
    except Exception:  # pylint: disable=broad-except
        return None


def public_base(env):
    return env['axsync.mcp.engine'].base_url() or request.httprequest.host_url.rstrip('/')


def cors_headers(env_setting_origins):
    origin = request.httprequest.headers.get('Origin')
    if not origin:
        return [], True
    allowed = [o.strip() for o in (env_setting_origins or '').split(',') if o.strip()]
    host = urlparse(request.httprequest.host_url).netloc
    same_origin = urlparse(origin).netloc == host
    if '*' in allowed or origin in allowed or same_origin:
        return [('Access-Control-Allow-Origin', '*' if '*' in allowed else origin),
                ('Vary', 'Origin'),
                ('Access-Control-Allow-Headers', 'Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id, X-Odoo-Database'),
                ('Access-Control-Allow-Methods', 'POST, GET, DELETE, OPTIONS'),
                ('Access-Control-Expose-Headers', 'WWW-Authenticate')], True
    return [], False


class McpController(http.Controller):

    def _unauthorized(self, env, message, db):
        base = public_base(env) if env is not None else request.httprequest.host_url.rstrip('/')
        suffix = '?db=%s' % db if db and not request.db else ''
        meta = '%s/.well-known/oauth-protected-resource%s' % (base, suffix)
        return json_response(
            {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32001, 'message': message}}, 401,
            [('WWW-Authenticate', 'Bearer realm="odoo", error="invalid_token", resource_metadata="%s"' % meta)])

    @http.route('/mcp', type='http', auth='none', methods=['POST', 'GET', 'DELETE', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def mcp(self, **kw):
        db = get_dbname()
        if not db:
            return json_response({'error': 'Database not specified. Add ?db=NAME to the URL.'}, 400)
        method = request.httprequest.method
        try:
            registry = Registry(db)
        except Exception:  # pylint: disable=broad-except
            return json_response({'error': 'Unknown database.'}, 404)
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            setting = env['axsync.mcp.setting'].get()
            cors, origin_ok = cors_headers(setting.allowed_origins)
            if not origin_ok:
                return json_response({'error': 'Origin not allowed.'}, 403)
            if method == 'OPTIONS':
                return Response('', status=204, headers=cors)
            if not setting.enabled:
                return json_response({'error': 'The MCP server is switched off.'}, 503, cors)
            if method in ('GET', 'DELETE'):
                return Response('', status=405, headers=cors + [('Allow', 'POST, OPTIONS')])
            auth = request.httprequest.headers.get('Authorization', '')
            plain = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
            token = env['axsync.mcp.token'].find_by_secret(plain)
            ip = request.httprequest.remote_addr
            fail = self._check_token(env, token, ip)
            if fail:
                resp = self._unauthorized(env, fail, db)
                for k, v in cors:
                    resp.headers[k] = v
                return resp
            user = token.user_id
            company = token.company_id or user.company_id
            uenv = api.Environment(cr, user.id, {'allowed_company_ids': [company.id], 'lang': user.lang,
                                                 'tz': user.tz})
            ctx = {'token_id': token.id, 'ip': ip, 'client_name': token.client_id.name or token.name}
            limited = self._rate_limited(uenv, setting)
            if limited:
                return json_response({'jsonrpc': '2.0', 'id': None, 'error': {
                    'code': -32000, 'message': 'Rate limit reached. Try again in a minute.'}}, 429,
                    cors + [('Retry-After', '30')])
            raw = request.httprequest.get_data(cache=False)
            if len(raw) > MAX_BODY:
                return json_response({'error': 'Request too large.'}, 413, cors)
            try:
                body = json.loads(raw.decode('utf-8'))
            except Exception:  # pylint: disable=broad-except
                return json_response({'jsonrpc': '2.0', 'id': None,
                                      'error': {'code': -32700, 'message': 'Parse error'}}, 400, cors)
            token.touch(ip)
            engine = uenv['axsync.mcp.engine']
            if isinstance(body, list):
                if not body or len(body) > MAX_BATCH:
                    return json_response({'jsonrpc': '2.0', 'id': None, 'error': {
                        'code': -32600, 'message': 'Batch must hold 1 to %d messages' % MAX_BATCH}}, 400, cors)
                results = [r for r in (engine.handle_message(m, dict(ctx)) for m in body) if r is not None]
                if not results:
                    return Response('', status=202, headers=cors)
                return json_response(results, 200, cors)
            result = engine.handle_message(body, ctx)
            if result is None:
                return Response('', status=202, headers=cors)
            return json_response(result, 200, cors)

    @staticmethod
    def _check_token(env, token, ip):
        if not token:
            return 'Missing or unknown token.'
        if not token.active:
            return 'The token was revoked.'
        if token.expires_on and token.expires_on < fields.Datetime.now():
            return 'The token expired.'
        user = token.user_id
        if not user.active or user.share:
            return 'The user is not active.'
        if not token.ip_allowed(ip):
            return 'This address is not allowed for the token.'
        setting = env['axsync.mcp.setting'].get()
        if not setting.allow_all_internal and not user.has_group('axsync_mcp_server.group_axsync_mcp_user') \
                and not user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
            return 'The user is not allowed to use MCP.'
        return False

    @staticmethod
    def _rate_limited(uenv, setting):
        limit = setting.rate_limit
        if not limit:
            return False
        since = fields.Datetime.now() - timedelta(seconds=60)
        uenv.cr.execute(
            "SELECT COUNT(*) FROM axsync_mcp_log WHERE user_id = %s AND create_date > %s AND method = 'tools/call'",
            [uenv.uid, since])
        return (uenv.cr.fetchone()[0] or 0) >= limit
