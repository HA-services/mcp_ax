# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

from odoo.tests import HttpCase, tagged

from .common import groups_field


def pkce():
    verifier = base64.urlsafe_b64encode(b'axsync-test-verifier-0123456789abcdef').rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestMcpHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        gf = groups_field(env)
        cls.setting = env['axsync.mcp.setting'].get()
        cls.user = env['res.users'].create({
            'name': 'Hana Http', 'login': 'hana_http', 'password': 'hana-pass-123', 'email': 'hana@example.test',
            gf: [(6, 0, [env.ref('base.group_user').id, env.ref('base.group_partner_manager').id,
                         env.ref('axsync_mcp_server.group_axsync_mcp_user').id])]})
        cls.token, cls.secret = env['axsync.mcp.token'].issue(
            cls.user, 'http', days=5, profile=env.ref('axsync_mcp_server.profile_standard'))
        cls.partner = env['res.partner'].create({'name': 'Http Partner', 'email': 'http@example.test'})

    def mcp(self, body, secret=None, headers=None, method='post', path='/mcp'):
        hdr = {'Content-Type': 'application/json'}
        if secret is not False:
            hdr['Authorization'] = 'Bearer %s' % (secret or self.secret)
        hdr.update(headers or {})
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return self.opener.request(method.upper(), self.base_url() + path,
                                   data=data if method == 'post' else None, headers=hdr, timeout=30,
                                   allow_redirects=False)

    def rpc(self, method, params=None, **kw):
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': method}
        if params is not None:
            msg['params'] = params
        return self.mcp(msg, **kw)

    def test_auth_failures(self):
        res = self.rpc('ping', secret=False)
        self.assertEqual(res.status_code, 401)
        self.assertIn('resource_metadata', res.headers['WWW-Authenticate'])
        self.assertEqual(self.rpc('ping', secret='axmcp_nonsense').status_code, 401)
        self.token.ip_allowlist = '10.9.9.9'
        self.assertEqual(self.rpc('ping').status_code, 401)
        self.token.ip_allowlist = False
        self.token.expires_on = '2000-01-01 00:00:00'
        self.assertEqual(self.rpc('ping').status_code, 401)
        self.token.expires_on = False
        self.assertEqual(self.rpc('ping').status_code, 200)

    def test_group_membership_required(self):
        gf = groups_field(self.env)
        outsider = self.env['res.users'].create({
            'name': 'Out Side', 'login': 'out_side', 'email': 'o@example.test',
            gf: [(6, 0, [self.env.ref('base.group_user').id])]})
        _tok, plain = self.env['axsync.mcp.token'].issue(outsider, 'o')
        self.assertEqual(self.rpc('ping', secret=plain).status_code, 401)
        self.setting.allow_all_internal = True
        self.assertEqual(self.rpc('ping', secret=plain).status_code, 200)

    def test_methods_and_cors(self):
        self.assertEqual(self.mcp({}, method='get').status_code, 405)
        self.assertEqual(self.mcp({}, method='delete').status_code, 405)
        res = self.mcp({}, method='options', secret=False)
        self.assertEqual(res.status_code, 204)
        foreign = self.rpc('ping', headers={'Origin': 'https://evil.example'})
        self.assertEqual(foreign.status_code, 403)
        self.setting.allowed_origins = 'https://inspector.example'
        allowed = self.rpc('ping', headers={'Origin': 'https://inspector.example'})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers['Access-Control-Allow-Origin'], 'https://inspector.example')

    def test_kill_switch(self):
        self.setting.enabled = False
        self.assertEqual(self.rpc('ping').status_code, 503)
        self.setting.enabled = True
        self.assertEqual(self.rpc('ping').status_code, 200)

    def test_full_session(self):
        init = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'pytest'},
                                       'capabilities': {}}).json()
        self.assertEqual(init['result']['serverInfo']['name'], 'AXSync MCP Server for Odoo')
        note = self.mcp({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(note.status_code, 202)
        tools = self.rpc('tools/list').json()['result']['tools']
        self.assertIn('search_records', {t['name'] for t in tools})
        call = self.rpc('tools/call', {'name': 'search_records', 'arguments': {
            'model': 'res.partner', 'domain': [['name', '=', 'Http Partner']], 'fields': ['name', 'email']}}).json()
        self.assertFalse(call['result']['isError'])
        data = json.loads(call['result']['content'][0]['text'])
        self.assertEqual(data['records'][0]['email'], 'http@example.test')
        log = self.env['axsync.mcp.log'].search([('tool', '=', 'search_records'), ('user_id', '=', self.user.id)])
        self.assertTrue(log)
        self.assertTrue(self.token.sudo().last_used)

    def test_batch_and_errors(self):
        batch = self.mcp([{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                          {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                          {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]).json()
        self.assertEqual([r['id'] for r in batch], [1, 2])
        self.assertEqual(self.mcp([]).status_code, 400)
        self.assertEqual(self.mcp('{not json').status_code, 400)
        self.assertEqual(self.mcp([{'jsonrpc': '2.0', 'id': i, 'method': 'ping'} for i in range(25)]).status_code, 400)
        big = self.mcp(b'x' * (2 * 1024 * 1024 + 10))
        self.assertEqual(big.status_code, 413)

    def test_rate_limit(self):
        self.setting.rate_limit = 2
        for _ in range(2):
            self.assertEqual(self.rpc('tools/call', {'name': 'whoami', 'arguments': {}}).status_code, 200)
        limited = self.rpc('tools/call', {'name': 'whoami', 'arguments': {}})
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited.headers)

    def test_write_over_http_and_undo(self):
        res = self.rpc('tools/call', {'name': 'update_records', 'arguments': {
            'model': 'res.partner', 'ids': [self.partner.id], 'values': {'ref': 'HTTP'}}}).json()
        data = json.loads(res['result']['content'][0]['text'])
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(self.partner.ref, 'HTTP')
        undo = self.rpc('tools/call', {'name': 'undo_change', 'arguments': {'batch': data['batch']}}).json()
        self.assertEqual(json.loads(undo['result']['content'][0]['text'])['undone'], 1)
        self.assertFalse(self.partner.ref)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def test_oauth_metadata(self):
        prm = self.url_open('/.well-known/oauth-protected-resource').json()
        self.assertTrue(prm['resource'].endswith('/mcp'))
        self.assertTrue(prm['authorization_servers'])
        asm = self.url_open('/.well-known/oauth-authorization-server').json()
        self.assertIn('S256', asm['code_challenge_methods_supported'])
        self.assertTrue(asm['registration_endpoint'].endswith('/mcp/oauth/register'))
        self.assertEqual(asm['token_endpoint_auth_methods_supported'], ['none'])
        self.setting.oauth_enabled = False
        self.assertEqual(self.url_open('/.well-known/oauth-authorization-server').status_code, 404)

    def _register(self, uris=None):
        return self.url_open('/mcp/oauth/register', data=json.dumps({
            'client_name': 'Test Connector', 'redirect_uris': uris if uris is not None else ['https://client.example/callback']}),
            headers={'Content-Type': 'application/json'})

    def test_oauth_registration_validation(self):
        ok = self._register()
        self.assertEqual(ok.status_code, 201)
        self.assertTrue(ok.json()['client_id'].startswith('axc_'))
        for bad in (['javascript:alert(1)'], ['http://evil.example/cb'], [], ['data:text/html,x'], ['ftp']):
            self.assertEqual(self._register(bad).status_code, 400, bad)
        self.assertEqual(self._register(['http://localhost:7777/cb']).status_code, 201)
        self.assertEqual(self._register(['claude://callback']).status_code, 201)

    def test_oauth_full_flow(self):
        client = self._register().json()
        verifier, challenge = pkce()
        params = {'response_type': 'code', 'client_id': client['client_id'],
                  'redirect_uri': 'https://client.example/callback', 'code_challenge': challenge,
                  'code_challenge_method': 'S256', 'state': 'st8'}
        # not signed in: redirected to login
        res = self.url_open('/mcp/oauth/authorize', data=None, headers={}, allow_redirects=False) if False else None
        self.authenticate('hana_http', 'hana-pass-123')
        consent = self.url_open('/mcp/oauth/authorize?' + '&'.join('%s=%s' % (k, v) for k, v in params.items()))
        self.assertEqual(consent.status_code, 200)
        self.assertIn('Test Connector', consent.text)
        self.assertIn('Hana Http', consent.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', consent.text).group(1)
        deny = self.url_open('/mcp/oauth/authorize', data=dict(params, csrf_token=csrf, decision='deny'),
                             allow_redirects=False)
        self.assertEqual(deny.status_code, 302)
        self.assertIn('error=access_denied', deny.headers['Location'])
        allow = self.url_open('/mcp/oauth/authorize', data=dict(params, csrf_token=csrf, decision='allow'),
                              allow_redirects=False)
        self.assertEqual(allow.status_code, 302)
        loc = urlparse(allow.headers['Location'])
        self.assertEqual(loc.netloc, 'client.example')
        q = parse_qs(loc.query)
        self.assertEqual(q['state'], ['st8'])
        code = q['code'][0]
        self.logout()

        def exchange(**over):
            data = {'grant_type': 'authorization_code', 'code': code, 'client_id': client['client_id'],
                    'redirect_uri': 'https://client.example/callback', 'code_verifier': verifier}
            data.update(over)
            return self.url_open('/mcp/oauth/token', data=data)

        self.assertEqual(exchange(code_verifier='wrong-verifier').status_code, 400)
        # a failed attempt burns the code
        self.assertEqual(exchange().status_code, 400)

        # second round with a fresh code
        self.authenticate('hana_http', 'hana-pass-123')
        consent = self.url_open('/mcp/oauth/authorize?' + '&'.join('%s=%s' % (k, v) for k, v in params.items()))
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', consent.text).group(1)
        allow = self.url_open('/mcp/oauth/authorize', data=dict(params, csrf_token=csrf, decision='allow'),
                              allow_redirects=False)
        code = parse_qs(urlparse(allow.headers['Location']).query)['code'][0]
        self.logout()
        self.assertEqual(exchange(redirect_uri='https://other.example/cb').status_code, 400)
        self.authenticate('hana_http', 'hana-pass-123')
        consent = self.url_open('/mcp/oauth/authorize?' + '&'.join('%s=%s' % (k, v) for k, v in params.items()))
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', consent.text).group(1)
        allow = self.url_open('/mcp/oauth/authorize', data=dict(params, csrf_token=csrf, decision='allow'),
                              allow_redirects=False)
        code = parse_qs(urlparse(allow.headers['Location']).query)['code'][0]
        self.logout()
        tok = exchange()
        self.assertEqual(tok.status_code, 200, tok.text)
        body = tok.json()
        self.assertEqual(body['token_type'], 'Bearer')
        self.assertTrue(body['refresh_token'])
        self.assertEqual(exchange().status_code, 400)  # code is single use
        who = self.rpc('tools/call', {'name': 'whoami', 'arguments': {}}, secret=body['access_token']).json()
        self.assertEqual(json.loads(who['result']['content'][0]['text'])['user']['login'], 'hana_http')
        # OAuth connections start at the default (read only) access level
        deny = self.rpc('tools/call', {'name': 'create_records', 'arguments': {
            'model': 'res.partner', 'records': [{'name': 'Nope'}]}}, secret=body['access_token']).json()
        self.assertTrue(deny['result']['isError'])
        refreshed = self.url_open('/mcp/oauth/token', data={
            'grant_type': 'refresh_token', 'refresh_token': body['refresh_token'], 'client_id': client['client_id']})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        new = refreshed.json()
        self.assertNotEqual(new['access_token'], body['access_token'])
        self.assertEqual(self.rpc('ping', secret=body['access_token']).status_code, 401)
        self.assertEqual(self.rpc('ping', secret=new['access_token']).status_code, 200)
        self.assertEqual(self.url_open('/mcp/oauth/token', data={
            'grant_type': 'refresh_token', 'refresh_token': body['refresh_token']}).status_code, 400)
        self.url_open('/mcp/oauth/revoke', data={'token': new['access_token']})
        self.assertEqual(self.rpc('ping', secret=new['access_token']).status_code, 401)

    def test_oauth_rejects_bad_authorize_requests(self):
        client = self._register().json()
        _verifier, challenge = pkce()
        self.authenticate('hana_http', 'hana-pass-123')
        base = '/mcp/oauth/authorize?response_type=code&client_id=%s&code_challenge=%s&code_challenge_method=S256&redirect_uri=' % (
            client['client_id'], challenge)
        self.assertEqual(self.url_open(base + 'https://evil.example/cb').status_code, 400)
        self.assertEqual(self.url_open('/mcp/oauth/authorize?client_id=axc_nope&redirect_uri=https://client.example/callback').status_code, 400)
        plain = self.url_open(base.replace('S256', 'plain') + 'https://client.example/callback', allow_redirects=False)
        self.assertEqual(plain.status_code, 302)
        self.assertIn('error=invalid_request', plain.headers['Location'])
        self.authenticate('admin', 'admin')
        self.logout()
        res = self.url_open(base + 'https://client.example/callback', allow_redirects=False)
        self.assertEqual(res.status_code, 303 if res.status_code == 303 else res.status_code)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn('/web/login', res.headers['Location'])

    # ------------------------------------------------------------------
    # pages and downloads
    # ------------------------------------------------------------------
    def test_pages_and_download(self):
        env = self.env
        owner = self.user
        link = env['axsync.mcp.page'].sudo().create({
            'name': 'Open page', 'owner_id': owner.id, 'mode': 'link', 'model': 'res.partner',
            'domain': json.dumps([['name', '=', 'Http Partner']]), 'field_names': 'name'})
        private = env['axsync.mcp.page'].sudo().create({
            'name': 'Private page', 'owner_id': owner.id, 'mode': 'private', 'model': 'res.partner',
            'domain': json.dumps([['name', '=', 'Http Partner']]), 'field_names': 'name'})
        res = self.url_open('/mcp/page/%s' % link.slug)
        self.assertEqual(res.status_code, 200)
        self.assertIn('Http Partner', res.text)
        self.assertIn('Content-Security-Policy', res.headers)
        self.assertEqual(self.url_open('/mcp/page/%s' % 'nope').status_code, 404)
        anon = self.url_open('/mcp/page/%s' % private.slug, allow_redirects=False)
        self.assertIn(anon.status_code, (302, 303))
        self.authenticate('hana_http', 'hana-pass-123')
        self.assertIn('Http Partner', self.url_open('/mcp/page/%s' % private.slug).text)
        self.logout()
        link.state = 'unpublished'
        self.assertEqual(self.url_open('/mcp/page/%s' % link.slug).status_code, 404)

        exp = json.loads(self.rpc('tools/call', {'name': 'export_records', 'arguments': {
            'model': 'res.partner', 'fields': ['name'], 'domain': [['name', '=', 'Http Partner']]}}).json()
            ['result']['content'][0]['text'])
        url = exp['download_url']
        path = url[url.index('/mcp/download'):]
        got = self.url_open(path)
        self.assertEqual(got.status_code, 200)
        self.assertIn('Http Partner', got.text)
        self.assertIn('attachment', got.headers['Content-Disposition'])
        self.assertEqual(self.url_open(path.split('?')[0] + '?access_token=wrong').status_code, 404)
        self.assertEqual(self.url_open(path.split('?')[0]).status_code, 404)
