# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json

from odoo.tests.common import TransactionCase


def groups_field(env):
    return 'group_ids' if 'group_ids' in env['res.users']._fields else 'groups_id'


class McpCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the audit log is written with its own cursor; share the test transaction
        if hasattr(cls, 'registry_enter_test_mode_cls'):
            if not getattr(cls, '_registry_patched', False):
                cls.registry_enter_test_mode_cls()
        elif hasattr(cls, 'registry_test_mode'):
            if not getattr(cls, '_registry_patched', False):
                cls.enterClassContext(cls.registry_test_mode())
        else:
            cls.registry.enter_test_mode(cls.cr)
            cls.addClassCleanup(cls.registry.leave_test_mode)
        env = cls.env
        gf = groups_field(env)
        ref = env.ref
        cls.profile_full = ref('axsync_mcp_server.profile_standard')
        cls.profile_ro = ref('axsync_mcp_server.profile_read_only')
        cls.user = env['res.users'].create({
            'name': 'Mia Mcp', 'login': 'mia_mcp', 'email': 'mia@example.test',
            gf: [(6, 0, [ref('base.group_user').id, ref('base.group_partner_manager').id,
                         ref('axsync_mcp_server.group_axsync_mcp_user').id])]})
        cls.approver = env['res.users'].create({
            'name': 'Ari Approver', 'login': 'ari_approver', 'email': 'ari@example.test',
            gf: [(6, 0, [ref('base.group_user').id, ref('base.group_partner_manager').id,
                         ref('axsync_mcp_server.group_axsync_mcp_approver').id])]})
        cls.manager = env['res.users'].create({
            'name': 'Max Manager', 'login': 'max_manager', 'email': 'max@example.test',
            gf: [(6, 0, [ref('base.group_user').id, ref('axsync_mcp_server.group_axsync_mcp_manager').id])]})
        cls.outsider = env['res.users'].create({
            'name': 'Olga Outsider', 'login': 'olga_out', 'email': 'olga@example.test',
            gf: [(6, 0, [ref('base.group_user').id])]})
        cls.setting = env['axsync.mcp.setting'].get()
        cls.token, cls.secret = env['axsync.mcp.token'].issue(cls.user, 'test', days=30, profile=cls.profile_full)
        cls.partners = env['res.partner'].create([
            {'name': 'Acme Test %d' % i, 'email': 'acme%d@example.test' % i, 'phone': '+1555000%04d' % i,
             'color': i} for i in range(1, 6)])

    def ctx(self, token=None, **extra):
        data = {'token_id': (token or self.token).id, 'ip': '127.0.0.1', 'client_name': 'tests'}
        data.update(extra)
        return data

    def engine(self, user=None):
        return self.env['axsync.mcp.engine'].with_user(user or self.user)

    def rpc(self, method, params=None, user=None, token=None, mid=1):
        msg = {'jsonrpc': '2.0', 'id': mid, 'method': method}
        if params is not None:
            msg['params'] = params
        return self.engine(user).handle_message(msg, self.ctx(token))

    def call(self, tool, args=None, user=None, token=None):
        resp = self.rpc('tools/call', {'name': tool, 'arguments': args or {}}, user=user, token=token)
        self.assertIn('result', resp, resp)
        data = json.loads(resp['result']['content'][0]['text'])
        data['_is_error'] = resp['result']['isError']
        return data

    def make_token(self, user, profile=None, **kw):
        return self.env['axsync.mcp.token'].issue(user, 'tok', days=5, profile=profile or self.profile_full, **kw)
