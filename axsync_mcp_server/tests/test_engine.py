# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import csv
import io
import json
import zipfile

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.axsync_mcp_server.models.engine import McpError

from .common import McpCase


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestProtocol(McpCase):

    def test_initialize_negotiates_version(self):
        r = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'clientInfo': {'name': 'tester'}})
        self.assertEqual(r['result']['protocolVersion'], '2025-03-26')
        self.assertIn('tools', r['result']['capabilities'])
        self.assertTrue(r['result']['instructions'])
        r = self.rpc('initialize', {'protocolVersion': '1999-01-01'})
        self.assertEqual(r['result']['protocolVersion'], '2025-06-18')

    def test_notifications_and_errors(self):
        eng = self.engine()
        self.assertIsNone(eng.handle_message({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, self.ctx()))
        self.assertEqual(self.rpc('ping')['result'], {})
        self.assertEqual(self.rpc('nope/nothing')['error']['code'], -32601)
        self.assertEqual(eng.handle_message({'id': 1}, self.ctx())['error']['code'], -32600)
        self.assertEqual(eng.handle_message([], self.ctx())['error']['code'], -32600)

    def test_tools_list_respects_access_level_and_switches(self):
        names = {t['name'] for t in self.rpc('tools/list')['result']['tools']}
        self.assertIn('update_records', names)
        self.assertIn('search_records', names)
        ro, _plain = self.make_token(self.user, profile=self.profile_ro)
        names_ro = {t['name'] for t in self.rpc('tools/list', token=ro)['result']['tools']}
        self.assertIn('search_records', names_ro)
        for write_tool in ('create_records', 'update_records', 'delete_records', 'call_method', 'build_module'):
            self.assertNotIn(write_tool, names_ro)
        tool = self.env.ref('axsync_mcp_server.tool_export_records')
        tool.enabled = False
        self.assertNotIn('export_records', {t['name'] for t in self.rpc('tools/list')['result']['tools']})
        res = self.call('export_records', {'model': 'res.partner', 'fields': ['name']})
        self.assertEqual(res['status'], 'denied')

    def test_every_tool_has_schema_and_record(self):
        listed = self.rpc('tools/list')['result']['tools']
        self.assertEqual(len(listed), 23)
        for tool in listed:
            self.assertEqual(tool['inputSchema']['type'], 'object')
            self.assertTrue(tool['description'])

    def test_resources_and_prompts(self):
        res = self.rpc('resources/read', {'uri': 'odoo://schema/res.partner'})
        schema = json.loads(res['result']['contents'][0]['text'])
        self.assertEqual(schema['model'], 'res.partner')
        self.assertTrue(any(f['name'] == 'email' for f in schema['fields']))
        self.assertIn('error', self.rpc('resources/read', {'uri': 'odoo://nothing'}))
        self.assertTrue(self.rpc('resources/list')['result']['resources'])
        self.assertTrue(self.rpc('resources/templates/list')['result']['resourceTemplates'])
        prompts = self.rpc('prompts/list')['result']['prompts']
        self.assertIn('weekly_sales_review', [p['name'] for p in prompts])
        got = self.rpc('prompts/get', {'name': 'weekly_sales_review', 'arguments': {'days': '14'}})
        self.assertIn('14 days', got['result']['messages'][0]['content']['text'])
        self.assertIn('error', self.rpc('prompts/get', {'name': 'missing'}))

    def test_unknown_tool_and_bad_args(self):
        self.assertEqual(self.call('nothing')['status'], 'error')
        self.assertEqual(self.call('search_records', {'model': 'does.not.exist'})['status'], 'error')


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestReading(McpCase):

    def test_whoami_and_models(self):
        who = self.call('whoami')
        self.assertEqual(who['user']['login'], 'mia_mcp')
        self.assertIn('odoo_version', who)
        listing = self.call('list_models', {'query': 'res.partner'})
        self.assertIn('res.partner', [m['model'] for m in listing['models']])
        allm = self.call('list_models', {'limit': 200})
        self.assertFalse([m for m in allm['models'] if m['model'].startswith(('ir.', 'axsync.mcp.'))])

    def test_search_count_read_name_search(self):
        dom = [['name', 'ilike', 'Acme Test']]
        res = self.call('search_records', {'model': 'res.partner', 'domain': dom, 'fields': ['name', 'email'],
                                           'order': 'name asc', 'limit': 3})
        self.assertEqual(res['total'], 5)
        self.assertEqual(res['returned'], 3)
        self.assertEqual(res['records'][0]['name'], 'Acme Test 1')
        self.assertEqual(self.call('count_records', {'model': 'res.partner', 'domain': dom})['count'], 5)
        ids = self.partners.ids[:2]
        rec = self.call('read_records', {'model': 'res.partner', 'ids': ids, 'fields': ['name']})
        self.assertEqual(len(rec['records']), 2)
        found = self.call('name_search', {'model': 'res.partner', 'name': 'Acme Test 3'})
        self.assertEqual(found['results'][0]['id'], self.partners[2].id)

    def test_domain_validation_and_macros(self):
        bad = self.call('search_records', {'model': 'res.partner', 'domain': [['name', 'any', []]]})
        self.assertEqual(bad['status'], 'error')
        self.assertEqual(self.call('search_records', {'model': 'res.partner', 'domain': [['nope', '=', 1]]})['status'], 'error')
        self.assertEqual(self.call('search_records', {'model': 'res.partner', 'domain': ['x']})['status'], 'error')
        self.assertEqual(self.call('search_records', {'model': 'res.partner', 'domain': 'name'})['status'], 'error')
        self.assertEqual(self.call('search_records', {'model': 'res.partner', 'order': 'name; drop'})['status'], 'error')
        macro = self.call('search_records', {'model': 'res.partner', 'fields': ['name'],
                                             'domain': [['create_date', '>=', '@today-1d'], ['name', 'ilike', 'Acme Test']]})
        self.assertEqual(macro['total'], 5)
        future = self.call('count_records', {'model': 'res.partner', 'domain': [['create_date', '>=', '@today+2d']]})
        self.assertEqual(future['count'], 0)

    def test_aggregate(self):
        res = self.call('aggregate', {'model': 'res.partner', 'domain': [['name', 'ilike', 'Acme Test']],
                                      'group_by': ['type'], 'measures': ['color:sum']})
        self.assertEqual(res['status'], 'ok', res)
        self.assertEqual(res['groups'][0]['count'], 5)
        self.assertEqual(res['groups'][0]['color:sum'], 15)
        self.assertEqual(self.call('aggregate', {'model': 'res.partner', 'group_by': ['name; x']})['status'], 'error')
        self.assertEqual(self.call('aggregate', {'model': 'res.partner', 'group_by': ['type'],
                                                 'measures': ['name:sum']})['status'], 'error')

    def test_blocked_models(self):
        for model in ('ir.config_parameter', 'axsync.mcp.token', 'res.users.apikeys', 'ir.model'):
            self.assertEqual(self.call('search_records', {'model': model})['status'], 'denied', model)
        for op in ('create_records', 'update_records'):
            args = {'model': 'res.users', 'records': [{'name': 'x', 'login': 'xx'}]} if op == 'create_records' else \
                {'model': 'res.users', 'ids': [self.user.id], 'values': {'name': 'Hacked'}}
            self.assertEqual(self.call(op, args)['status'], 'denied')
        self.assertEqual(self.user.name, 'Mia Mcp')

    def test_user_rights_still_apply(self):
        # Olga has no partner manager rights: creating a partner depends on Odoo ACLs, not on MCP
        tok, _plain = self.make_token(self.outsider)
        self.setting.allow_all_internal = True
        res = self.call('create_records', {'model': 'res.country', 'records': [{'name': 'Narnia', 'code': 'NZ'}]},
                        user=self.outsider, token=tok)
        self.assertEqual(res['status'], 'denied')
        self.assertFalse(self.env['res.country'].search([('name', '=', 'Narnia')]))

    def test_describe_model(self):
        info = self.call('describe_model', {'model': 'res.partner'})
        self.assertTrue(info['allowed_operations']['read'])
        names = {f['name'] for f in info['fields']}
        self.assertIn('email', names)
        self.assertTrue(info['has_active_flag'])
        sel = [f for f in info['fields'] if f['name'] == 'type'][0]
        self.assertTrue(sel['selection'])


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestMasks(McpCase):

    def _mask(self, fname, mode, **kw):
        return self.env['axsync.mcp.mask'].create(dict(
            {'name': 'm_' + fname, 'model_id': self.env['ir.model']._get('res.partner').id,
             'field_name': fname, 'mode': mode}, **kw))

    def test_hidden_mask(self):
        self._mask('email', 'hidden')
        res = self.call('search_records', {'model': 'res.partner', 'domain': [['name', 'ilike', 'Acme Test']]})
        self.assertNotIn('email', res['records'][0])
        explicit = self.call('search_records', {'model': 'res.partner', 'fields': ['name', 'email']})
        self.assertEqual(explicit['status'], 'denied')
        by_filter = self.call('search_records', {'model': 'res.partner', 'domain': [['email', 'ilike', 'acme']]})
        self.assertEqual(by_filter['status'], 'denied')
        by_group = self.call('aggregate', {'model': 'res.partner', 'group_by': ['email']})
        self.assertEqual(by_group['status'], 'denied')
        by_order = self.call('search_records', {'model': 'res.partner', 'order': 'email'})
        self.assertEqual(by_order['status'], 'denied')
        write = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id],
                                             'values': {'email': 'x@example.test'}})
        self.assertEqual(write['status'], 'denied')
        info = self.call('describe_model', {'model': 'res.partner'})
        self.assertNotIn('email', {f['name'] for f in info['fields']})
        export = self.call('export_records', {'model': 'res.partner', 'fields': ['name', 'email']})
        self.assertEqual(export['status'], 'denied')
        related = self.call('search_records', {'model': 'res.partner', 'domain': [['parent_id.email', '=', 'x']]})
        self.assertEqual(related['status'], 'denied')

    def test_partial_and_hash_masks(self):
        self._mask('phone', 'partial')
        self._mask('email', 'hash')
        dom = [['name', '=', 'Acme Test 1']]
        row = self.call('search_records', {'model': 'res.partner', 'domain': dom,
                                           'fields': ['name', 'phone', 'email']})['records'][0]
        self.assertIn('***', row['phone'])
        self.assertNotEqual(row['phone'], self.partners[0].phone)
        self.assertTrue(row['email'].startswith('h_'))
        again = self.call('search_records', {'model': 'res.partner', 'domain': dom,
                                             'fields': ['email']})['records'][0]
        self.assertEqual(again['email'], row['email'])
        by_filter = self.call('search_records', {'model': 'res.partner', 'domain': [['phone', 'ilike', '1555']]})
        self.assertEqual(by_filter['status'], 'denied')

    def test_exempt_group(self):
        group = self.env.ref('base.group_partner_manager')
        self._mask('phone', 'hidden', exempt_group_ids=[(6, 0, group.ids)])
        row = self.call('search_records', {'model': 'res.partner', 'domain': [['name', '=', 'Acme Test 1']],
                                           'fields': ['phone']})['records'][0]
        self.assertEqual(row['phone'], self.partners[0].phone)

    def test_global_mask_for_any_model(self):
        self.env['axsync.mcp.mask'].create({'name': 'all vat', 'field_name': 'vat', 'mode': 'hidden'})
        res = self.call('search_records', {'model': 'res.partner', 'fields': ['name', 'vat']})
        self.assertEqual(res['status'], 'denied')

    def test_secret_names_hidden_by_default(self):
        engine = self.engine()
        masks = engine._masks('res.users')
        self.assertEqual(masks.get('password'), 'hidden')
        self.setting.mask_default_secrets = False
        self.assertNotIn('password', engine._masks('res.users'))


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestWrites(McpCase):

    def test_create_and_undo(self):
        res = self.call('create_records', {'model': 'res.partner', 'records': [
            {'name': 'Zed Created', 'email': 'zed@example.test'}, {'name': 'Zoe Created'}]})
        self.assertEqual(res['status'], 'ok', res)
        ids = [r['id'] for r in res['created']]
        self.assertEqual(len(ids), 2)
        entries = self.env['axsync.mcp.journal'].search([('batch', '=', res['batch'])])
        self.assertEqual(set(entries.mapped('operation')), {'create'})
        undo = self.call('undo_change', {'batch': res['batch']})
        self.assertEqual(undo['undone'], 2)
        self.assertFalse(self.env['res.partner'].browse(ids).exists())
        again = self.call('undo_change', {'batch': res['batch']})
        self.assertEqual(again['status'], 'error')

    def test_update_journal_undo_and_conflict(self):
        p = self.partners[0]
        old_phone = p.phone
        res = self.call('update_records', {'model': 'res.partner', 'ids': [p.id], 'values': {'phone': '+1999'}})
        self.assertEqual(res['updated'], 1)
        self.assertEqual(p.phone, '+1999')
        entry = self.env['axsync.mcp.journal'].search([('batch', '=', res['batch'])])
        self.assertEqual(json.loads(entry.old_values)['phone'], old_phone)
        self.assertEqual(json.loads(entry.new_values)['phone'], '+1999')
        self.assertEqual(self.call('undo_change', {'journal_id': entry.id})['undone'], 1)
        self.assertEqual(p.phone, old_phone)
        # conflict
        res = self.call('update_records', {'model': 'res.partner', 'ids': [p.id], 'values': {'phone': '+1888'}})
        p.write({'phone': '+1777'})
        refused = self.call('undo_change', {'batch': res['batch']})
        self.assertEqual(refused['status'], 'error')
        self.assertIn('changed', refused['message'])
        self.assertEqual(p.phone, '+1777')
        forced = self.call('undo_change', {'batch': res['batch'], 'force': True})
        self.assertEqual(forced['status'], 'ok')
        self.assertEqual(p.phone, old_phone)

    def test_update_by_domain_and_limits(self):
        res = self.call('update_records', {'model': 'res.partner', 'domain': [['name', 'ilike', 'Acme Test']],
                                           'values': {'ref': 'BATCH'}})
        self.assertEqual(res['updated'], 5)
        self.setting.max_records = 3
        too_many = self.call('update_records', {'model': 'res.partner', 'domain': [['name', 'ilike', 'Acme Test']],
                                                'values': {'ref': 'X'}})
        self.assertEqual(too_many['status'], 'error')
        self.assertIn('start_bulk_job', too_many['message'])

    def test_unknown_and_unwritable_fields(self):
        for values in ({'nope': 1}, {'display_name': 'x'}, {'create_date': '2020-01-01'}, {}):
            res = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id], 'values': values})
            self.assertEqual(res['status'], 'error', values)

    def test_dry_run_changes_nothing(self):
        p = self.partners[1]
        before = self.env['axsync.mcp.journal'].search_count([])
        res = self.call('update_records', {'model': 'res.partner', 'ids': [p.id], 'values': {'phone': '+1000'},
                                           'dry_run': True})
        self.assertEqual(res['status'], 'dry_run')
        self.assertEqual(res['preview']['updated'], 1)
        self.assertNotEqual(p.phone, '+1000')
        self.assertEqual(self.env['axsync.mcp.journal'].search_count([]), before)
        created = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'Ghost Partner'}],
                                               'dry_run': True})
        self.assertEqual(created['status'], 'dry_run')
        self.assertFalse(self.env['res.partner'].search([('name', '=', 'Ghost Partner')]))

    def test_delete_archives_after_approval(self):
        p = self.partners[2]
        res = self.call('delete_records', {'model': 'res.partner', 'ids': [p.id]})
        self.assertEqual(res['status'], 'pending_approval')
        approval = self.env['axsync.mcp.approval'].browse(res['approval_id'])
        self.assertEqual(approval.state, 'pending')
        self.assertTrue(p.active)
        self.assertTrue(approval.activity_ids)
        with self.assertRaises(AccessError):
            approval.with_user(self.user).action_approve()
        approval.with_user(self.approver).action_approve()
        self.assertEqual(approval.state, 'approved')
        self.assertFalse(p.active)
        self.assertEqual(approval.approver_id, self.approver)
        status = self.call('approval_status', {'approval_id': approval.id})
        self.assertEqual(status['state'], 'approved')
        self.assertEqual(status['result']['archived'], [p.id])
        entry = self.env['axsync.mcp.journal'].search([('res_id', '=', p.id), ('operation', '=', 'archive')])
        self.assertTrue(entry)
        self.assertEqual(self.call('undo_change', {'journal_id': entry.id})['undone'], 1)
        self.assertTrue(p.active)
        with self.assertRaises(UserError):
            approval.with_user(self.approver).action_approve()

    def test_nobody_approves_own_request(self):
        tok, _plain = self.make_token(self.approver)
        res = self.call('delete_records', {'model': 'res.partner', 'ids': [self.partners[3].id]},
                        user=self.approver, token=tok)
        self.assertEqual(res['status'], 'pending_approval')
        approval = self.env['axsync.mcp.approval'].browse(res['approval_id'])
        self.assertFalse(approval.with_user(self.approver).can_decide)
        with self.assertRaises(AccessError):
            approval.with_user(self.approver).action_approve()
        approval.with_user(self.manager).action_reject()
        self.assertEqual(approval.state, 'rejected')
        self.assertTrue(self.partners[3].active)

    def test_approval_expiry(self):
        res = self.call('delete_records', {'model': 'res.partner', 'ids': [self.partners[4].id]})
        approval = self.env['axsync.mcp.approval'].browse(res['approval_id'])
        approval.expires_on = '2000-01-01 00:00:00'
        with self.assertRaises(UserError):
            approval.with_user(self.approver).action_approve()
        self.env['axsync.mcp.approval']._cron_expire()
        self.assertEqual(approval.state, 'expired')

    def test_hard_delete_only_for_draft_like_states(self):
        engine = self.engine()
        # a model without active flag and without state is refused
        res = self.call('delete_records', {'model': 'res.country.state', 'ids': [self.env['res.country.state'].search([], limit=1).id]})
        self.assertIn(res['status'], ('pending_approval', 'denied', 'error'))
        self.assertIsNotNone(engine)

    def test_call_method_confirmation_flow(self):
        p = self.partners[0]
        args = {'model': 'res.partner', 'method': 'message_post', 'ids': [p.id], 'kwargs': {'body': 'Hello from MCP'}}
        first = self.call('call_method', args)
        self.assertEqual(first['status'], 'confirmation_required')
        self.assertTrue(first['confirm'])
        self.assertFalse(p.message_ids.filtered(lambda m: 'Hello from MCP' in (m.body or '')))
        tampered = self.call('call_method', dict(args, kwargs={'body': 'Other'}, confirm=first['confirm']))
        self.assertEqual(tampered['status'], 'confirmation_required')
        tok, _plain = self.make_token(self.approver)
        stolen = self.call('call_method', dict(args, confirm=first['confirm']), user=self.approver, token=tok)
        self.assertEqual(stolen['status'], 'confirmation_required')
        done = self.call('call_method', dict(args, confirm=first['confirm']))
        self.assertEqual(done['status'], 'ok', done)
        self.assertTrue(p.message_ids.filtered(lambda m: 'Hello from MCP' in (m.body or '')))

    def test_call_method_blocklist(self):
        base = {'model': 'res.partner', 'ids': [self.partners[0].id], 'confirm': 'x'}
        for method in ('unlink', 'write', 'create', '_compute_display_name', 'sudo', 'with_user', 'nope_nothing', 'a b'):
            res = self.call('call_method', dict(base, method=method))
            self.assertEqual(res['status'], 'denied', method)

    def test_confirm_token_expiry(self):
        engine = self.engine()
        token = engine.make_confirm_token('call_method', {'a': 1})
        self.assertTrue(engine.check_confirm_token('call_method', {'a': 1}, token))
        self.assertFalse(engine.check_confirm_token('call_method', {'a': 2}, token))
        self.assertFalse(engine.check_confirm_token('other_tool', {'a': 1}, token))
        self.assertFalse(engine.check_confirm_token('call_method', {'a': 1}, '1.abc'))
        self.assertFalse(engine.check_confirm_token('call_method', {'a': 1}, None))
        self.assertFalse(engine.check_confirm_token('call_method', {'a': 1}, 'garbage'))

    def test_default_write_action_ask(self):
        self.setting.default_write_action = 'ask'
        p = self.partners[0]
        args = {'model': 'res.partner', 'ids': [p.id], 'values': {'ref': 'ASK'}}
        first = self.call('update_records', args)
        self.assertEqual(first['status'], 'confirmation_required')
        self.assertEqual(first['preview']['updated'], 1)
        self.assertNotEqual(p.ref, 'ASK')
        second = self.call('update_records', dict(args, confirm=first['confirm']))
        self.assertEqual(second['status'], 'ok')
        self.assertEqual(p.ref, 'ASK')
        self.setting.default_write_action = 'approve'
        third = self.call('update_records', {'model': 'res.partner', 'ids': [p.id], 'values': {'ref': 'NEXT'}})
        self.assertEqual(third['status'], 'pending_approval')

    def test_quota(self):
        self.setting.daily_write_quota = 2
        for i in range(2):
            self.assertEqual(self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'Q%d' % i}]})['status'], 'ok')
        blocked = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'Q3'}]})
        self.assertEqual(blocked['status'], 'denied')
        self.assertIn('quota', blocked['message'])

    def test_bulk_job(self):
        parts = self.env['res.partner'].create([{'name': 'Bulk %d' % i} for i in range(7)])
        args = {'model': 'res.partner', 'domain': [['name', 'ilike', 'Bulk ']], 'operation': 'write',
                'values': {'ref': 'BULK'}}
        first = self.call('start_bulk_job', args)
        self.assertEqual(first['status'], 'confirmation_required')
        self.assertEqual(first['preview']['would_process'], 7)
        started = self.call('start_bulk_job', dict(args, confirm=first['confirm']))
        self.assertEqual(started['status'], 'ok', started)
        self.assertEqual(started['total'], 7)
        self.env['axsync.mcp.job']._cron_run()
        status = self.call('job_status', {'job_id': started['job_id']})
        self.assertEqual(status['state'], 'done')
        self.assertEqual(status['done'], 7)
        self.assertEqual(set(parts.mapped('ref')), {'BULK'})
        self.assertEqual(self.env['axsync.mcp.journal'].search_count([('batch', '=', 'job%d' % started['job_id'])]), 7)
        # archive operation, cancelled before running
        arch = dict(args, operation='archive')
        arch.pop('values')
        pending = self.call('start_bulk_job', arch)
        job = self.env['axsync.mcp.job'].browse(self.call('start_bulk_job', dict(arch, confirm=pending['confirm']))['job_id'])
        job.action_cancel()
        self.env['axsync.mcp.job']._cron_run()
        self.assertTrue(all(parts.mapped('active')))


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestSafetyLayers(McpCase):

    def _policy(self, **vals):
        base = {'name': 'test policy', 'operation': 'write', 'action': 'block'}
        base.update(vals)
        return self.env['axsync.mcp.policy'].create(base)

    def test_block_policy_with_message(self):
        self._policy(model_ids=[(6, 0, self.env['ir.model']._get('res.partner').ids)], message='Frozen for audit')
        res = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id], 'values': {'ref': 'X'}})
        self.assertEqual(res['status'], 'denied')
        self.assertEqual(res['message'], 'Frozen for audit')
        self.assertTrue(self.call('count_records', {'model': 'res.partner'})['count'])

    def test_record_condition_and_min_records(self):
        self._policy(action='approve', domain="[('color', '>', 3)]")
        low = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id], 'values': {'ref': 'L'}})
        high = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[4].id], 'values': {'ref': 'H'}})
        self.assertEqual(low['status'], 'ok')
        self.assertEqual(high['status'], 'pending_approval')
        self._policy(name='bulk create', operation='create', action='ask', min_records=3)
        two = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'a1'}, {'name': 'a2'}]})
        three = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'b1'}, {'name': 'b2'}, {'name': 'b3'}]})
        self.assertEqual(two['status'], 'ok')
        self.assertEqual(three['status'], 'confirmation_required')

    def test_strictest_policy_wins_and_scoping(self):
        self._policy(name='soft', action='ask')
        self._policy(name='hard', action='block')
        self._policy(name='other user only', action='block', operation='create',
                     user_ids=[(6, 0, self.approver.ids)])
        res = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id], 'values': {'ref': 'X'}})
        self.assertEqual(res['status'], 'denied')
        ok = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'scoped ok'}]})
        self.assertEqual(ok['status'], 'ok')
        group_policy = self._policy(name='partner managers', action='block', operation='create',
                                    group_ids=[(6, 0, self.env.ref('base.group_partner_manager').ids)])
        self.assertEqual(self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'no'}]})['status'], 'denied')
        group_policy.active = False
        self.assertEqual(self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'yes'}]})['status'], 'ok')

    def test_custom_profile(self):
        prof = self.env['axsync.mcp.profile'].create({
            'name': 'Partners only', 'level': 'custom',
            'line_ids': [(0, 0, {'model_id': self.env['ir.model']._get('res.partner').id, 'can_read': True})]})
        tok, _plain = self.make_token(self.user, profile=prof)
        self.assertEqual(self.call('count_records', {'model': 'res.partner'}, token=tok)['status'], 'ok')
        self.assertEqual(self.call('count_records', {'model': 'res.country'}, token=tok)['status'], 'denied')
        denied = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id],
                                              'values': {'ref': 'X'}}, token=tok)
        self.assertEqual(denied['status'], 'denied')
        listed = self.call('list_models', token=tok)
        self.assertEqual([m['model'] for m in listed['models']], ['res.partner'])
        prof.line_ids.can_write = True
        prof.tool_ids = [(6, 0, [self.env.ref('axsync_mcp_server.tool_update_records').id])]
        self.assertEqual(self.call('count_records', {'model': 'res.partner'}, token=tok)['status'], 'denied')
        self.assertEqual(self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id],
                                                      'values': {'ref': 'Y'}}, token=tok)['status'], 'ok')

    def test_enforced_profile_restricts_full_token(self):
        self.env['axsync.mcp.profile'].create({
            'name': 'Mia is read only', 'level': 'read_only', 'enforced': True, 'user_ids': [(6, 0, self.user.ids)]})
        denied = self.call('update_records', {'model': 'res.partner', 'ids': [self.partners[0].id], 'values': {'ref': 'X'}})
        self.assertEqual(denied['status'], 'denied')
        self.assertEqual(self.call('count_records', {'model': 'res.partner'})['status'], 'ok')

    def test_tools_default_to_read_only_level(self):
        self.assertEqual(self.setting.default_profile_id, self.profile_ro)
        tok, _plain = self.env['axsync.mcp.token'].issue(self.user, 'default level')
        res = self.call('create_records', {'model': 'res.partner', 'records': [{'name': 'x'}]}, token=tok)
        self.assertEqual(res['status'], 'denied')

    def test_audit_log(self):
        self.call('count_records', {'model': 'res.partner'})
        self.call('update_records', {'model': 'ir.model', 'ids': [1], 'values': {'name': 'x'}})
        Log = self.env['axsync.mcp.log'].sudo()
        ok = Log.search([('tool', '=', 'count_records'), ('user_id', '=', self.user.id)], limit=1)
        self.assertEqual(ok.status, 'ok')
        self.assertEqual(ok.model, 'res.partner')
        refused = Log.search([('tool', '=', 'update_records'), ('user_id', '=', self.user.id)], limit=1)
        self.assertEqual(refused.status, 'denied')
        with self.assertRaises(UserError):
            ok.write({'message': 'tampered'})
        with self.assertRaises(UserError):
            ok.unlink()
        journal = self.env['axsync.mcp.journal'].sudo().search([], limit=1)
        if journal:
            with self.assertRaises(UserError):
                journal.write({'old_values': 'x'})
            with self.assertRaises(UserError):
                journal.unlink()
        self.setting.log_retention_days = 1
        self.env.cr.execute("UPDATE axsync_mcp_log SET create_date = '2000-01-01 00:00:00' WHERE id = %s", [ok.id])
        ok.invalidate_recordset()
        self.env['axsync.mcp.log']._cron_purge()
        self.assertFalse(ok.exists())

    def test_users_only_see_own_records(self):
        self.call('count_records', {'model': 'res.partner'})
        tok, _plain = self.make_token(self.approver)
        self.call('count_records', {'model': 'res.partner'}, user=self.approver, token=tok)
        mine = self.env['axsync.mcp.log'].with_user(self.user).search([])
        self.assertEqual(set(mine.mapped('user_id')), {self.user})
        self.assertIn(self.user, self.env['axsync.mcp.log'].with_user(self.manager).search([]).mapped('user_id'))
        self.assertIn(self.approver, self.env['axsync.mcp.log'].with_user(self.manager).search([]).mapped('user_id'))
        self.assertFalse(self.env['axsync.mcp.token'].with_user(self.approver).search([('user_id', '=', self.user.id)]))


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestPagesExportBuilder(McpCase):

    def test_pages(self):
        res = self.call('publish_page', {'title': 'Acme list', 'model': 'res.partner',
                                         'domain': [['name', 'ilike', 'Acme Test']], 'fields': ['name', 'email'],
                                         'mode': 'private'})
        self.assertEqual(res['status'], 'ok', res)
        page = self.env['axsync.mcp.page'].search([('slug', '=', res['slug'])])
        html = page.render_html(self.user)
        self.assertIn('Acme Test 1', html)
        self.assertIn('noindex', html)
        grouped = self.call('publish_page', {'title': 'By type', 'model': 'res.partner', 'group_by': 'type',
                                             'measure': 'color:sum', 'chart': 'bar',
                                             'domain': [['name', 'ilike', 'Acme Test']]})
        gp = self.env['axsync.mcp.page'].search([('slug', '=', grouped['slug'])])
        self.assertIn('<svg', gp.render_html(self.user))
        listed = self.call('list_pages')
        self.assertEqual(len(listed['pages']), 2)
        self.assertEqual(self.call('unpublish_page', {'slug': res['slug']})['status'], 'ok')
        self.assertFalse(page.is_viewable())
        self.assertEqual(self.call('unpublish_page', {'slug': 'nope'})['status'], 'error')

    def test_page_masks_follow_the_viewer(self):
        self.env['axsync.mcp.mask'].create({
            'name': 'mask email', 'model_id': self.env['ir.model']._get('res.partner').id,
            'field_name': 'email', 'mode': 'hidden'})
        res = self.call('publish_page', {'title': 'x', 'model': 'res.partner', 'fields': ['name', 'email'],
                                         'mode': 'login'})
        self.assertEqual(res['status'], 'denied')
        res = self.call('publish_page', {'title': 'x', 'model': 'res.partner', 'fields': ['name'], 'mode': 'login'})
        page = self.env['axsync.mcp.page'].search([('slug', '=', res['slug'])])
        self.assertNotIn('acme1@example.test', page.render_html(self.approver))

    def test_link_pages_need_approval(self):
        res = self.call('publish_page', {'title': 'Public', 'model': 'res.partner', 'fields': ['name'],
                                         'domain': [['name', 'ilike', 'Acme Test']], 'mode': 'link'})
        self.assertEqual(res['status'], 'pending_approval')
        self.assertFalse(self.env['axsync.mcp.page'].search([('name', '=', 'Public')]))
        approval = self.env['axsync.mcp.approval'].browse(res['approval_id'])
        approval.with_user(self.approver).action_approve()
        page = self.env['axsync.mcp.page'].search([('name', '=', 'Public')])
        self.assertEqual(page.mode, 'link')
        self.assertTrue(page.expires_on)
        self.assertTrue(page.is_viewable())
        self.assertIn('Acme Test 2', page.render_html(page.owner_id))

    def test_export_csv(self):
        self.env['res.partner'].create({'name': '=HYPERLINK("http://x")', 'email': 'evil@example.test'})
        res = self.call('export_records', {'model': 'res.partner', 'fields': ['name', 'email'],
                                           'domain': [['email', 'in', ['evil@example.test', 'acme1@example.test']]]})
        self.assertEqual(res['status'], 'ok', res)
        self.assertEqual(res['rows'], 2)
        att = self.env['ir.attachment'].sudo().search([('name', '=', 'res_partner_export.csv')], limit=1, order='id desc')
        self.assertIn(att.access_token, res['download_url'])
        rows = list(csv.reader(io.StringIO(att.raw.decode('utf-8-sig'))))
        self.assertEqual(rows[0], ['name', 'email'])
        self.assertTrue(any(r[0].startswith("'=") for r in rows[1:]))

    def _spec(self, **over):
        spec = {'technical_name': 'demo_tracker', 'title': 'Demo Tracker', 'summary': 'Tracks demos',
                'models': [{'name': 'demo.tracker', 'description': 'Demo item', 'fields': [
                    {'name': 'stage', 'type': 'selection', 'selection': ['new', 'done'], 'string': 'Stage'},
                    {'name': 'partner_id', 'type': 'many2one', 'relation': 'res.partner'},
                    {'name': 'planned', 'type': 'date'}, {'name': 'qty', 'type': 'float', 'required': True},
                    {'name': 'notes', 'type': 'text'}]}]}
        spec.update(over)
        return spec

    def test_builder_output_is_valid(self):
        res = self.call('build_module', self._spec())
        self.assertEqual(res['status'], 'pending_approval')
        approval = self.env['axsync.mcp.approval'].browse(res['approval_id'])
        approval.with_user(self.approver).action_approve()
        result = json.loads(approval.result)
        self.assertEqual(result['module'], 'demo_tracker')
        att = self.env['ir.attachment'].sudo().search([('name', '=', 'demo_tracker.zip')], limit=1)
        zf = zipfile.ZipFile(io.BytesIO(att.raw))
        names = zf.namelist()
        for expected in ('demo_tracker/__manifest__.py', 'demo_tracker/models/demo_tracker.py',
                         'demo_tracker/views/views.xml'):
            self.assertIn(expected, names)
        for name in names:
            data = zf.read(name).decode()
            if name.endswith('.py'):
                compile(data, name, 'exec')
            if name.endswith('.xml'):
                etree.fromstring(data.encode())
        manifest = eval(zf.read('demo_tracker/__manifest__.py').decode().split('\n', 1)[1])
        self.assertEqual(manifest['name'], 'Demo Tracker')
        self.assertIn(manifest['data'][0], ('security/ir.model.access.csv', 'security/ir.access.csv'))
        model_src = zf.read('demo_tracker/models/demo_tracker.py').decode()
        self.assertIn("_name = 'demo.tracker'", model_src)
        self.assertIn('fields.Selection', model_src)

    def test_builder_rejects_unsafe_input(self):
        eng = self.engine()
        bad = [
            self._spec(technical_name='Bad Name'),
            self._spec(technical_name='../evil'),
            self._spec(models=[{'name': 'res.partner', 'fields': []}]),
            self._spec(models=[{'name': "x.y'); import os #", 'fields': []}]),
            self._spec(models=[{'name': 'demo.thing', 'fields': [{'name': 'class', 'type': 'char'}]}]),
            self._spec(models=[{'name': 'demo.thing', 'fields': [{'name': 'write', 'type': 'char'}]}]),
            self._spec(models=[{'name': 'demo.thing', 'fields': [{'name': 'a', 'type': 'binary'}]}]),
            self._spec(models=[{'name': 'demo.thing', 'fields': [{'name': 'a', 'type': 'many2one', 'relation': 'ir.model'}]}]),
            self._spec(models=[{'name': 'demo.thing', 'fields': [{'name': 'a', 'type': 'selection', 'selection': ["x'];import os"]}]}]),
            self._spec(models=[]),
            self._spec(models=[{'name': 'demo.a_b', 'fields': []}, {'name': 'demo_a.b', 'fields': []}]),
        ]
        for spec in bad:
            with self.assertRaises(McpError):
                eng._tool_build_module(spec, {})
        evil_title = eng._tool_build_module(self._spec(title='A <b>&"\'', summary='x\'; import os #'), {})
        att = self.env['ir.attachment'].sudo().search([('name', '=', 'demo_tracker.zip')], limit=1, order='id desc')
        zf = zipfile.ZipFile(io.BytesIO(att.raw))
        compile(zf.read('demo_tracker/__manifest__.py').decode(), 'manifest', 'exec')
        self.assertTrue(evil_title['download_url'])


@tagged('post_install', '-at_install', 'axsync_mcp')
class TestTokens(McpCase):

    def test_token_is_hashed_and_findable(self):
        Token = self.env['axsync.mcp.token']
        self.assertNotIn(self.secret, (self.token.sudo().token_hash or ''))
        self.assertTrue(self.secret.startswith('axmcp_'))
        self.assertEqual(Token.find_by_secret(self.secret), self.token)
        self.assertFalse(Token.find_by_secret(self.secret + 'x'))
        self.assertFalse(Token.find_by_secret(''))
        self.assertEqual(self.token.state, 'active')

    def test_ip_allowlist(self):
        tok = self.token
        self.assertTrue(tok.ip_allowed('203.0.113.9'))
        tok.ip_allowlist = '10.0.0.0/24\n192.168.1.5\nnot-an-ip'
        self.assertTrue(tok.ip_allowed('10.0.0.77'))
        self.assertTrue(tok.ip_allowed('192.168.1.5'))
        self.assertFalse(tok.ip_allowed('10.0.1.1'))
        self.assertFalse(tok.ip_allowed('garbage'))
        self.assertFalse(tok.ip_allowed(None))

    def test_expiry_and_revoke(self):
        tok, _plain = self.make_token(self.user)
        tok.expires_on = '2000-01-01 00:00:00'
        self.assertEqual(tok.state, 'expired')
        tok2, _plain2 = self.make_token(self.user)
        with self.assertRaises(AccessError):
            tok2.with_user(self.outsider).action_revoke()
        tok2.with_user(self.user).action_revoke()
        self.assertEqual(tok2.state, 'revoked')
        self.assertFalse(self.env['axsync.mcp.token'].find_by_secret(_plain2))
        self.assertIn(tok, self.env['axsync.mcp.token'].search([('state', '=', 'expired')]))
        self.assertNotIn(tok, self.env['axsync.mcp.token'].search([('state', '!=', 'expired')]))

    def test_wizard_creates_token(self):
        wiz = self.env['axsync.mcp.token.wizard'].with_user(self.user).create({'name': 'Wizard token'})
        wiz.action_generate()
        self.assertEqual(wiz.state, 'done')
        self.assertTrue(wiz.secret.startswith('axmcp_'))
        self.assertIn('Authorization: Bearer', wiz.snippets)
        self.assertIn('/mcp', wiz.endpoint)
        found = self.env['axsync.mcp.token'].find_by_secret(wiz.secret)
        self.assertEqual(found.user_id, self.user)
        with self.assertRaises(AccessError):
            self.env['axsync.mcp.token.wizard'].with_user(self.user).create({'name': 'x', 'user_id': self.approver.id})
        self.assertFalse(self.setting.allow_all_internal)
        wiz2 = self.env['axsync.mcp.token.wizard'].with_user(self.manager).create({'name': 'for olga', 'user_id': self.outsider.id})
        with self.assertRaises(UserError):
            wiz2.action_generate()

    def test_settings_singleton(self):
        with self.assertRaises(UserError):
            self.env['axsync.mcp.setting'].sudo().create({})
        with self.assertRaises(UserError):
            self.setting.unlink()
        action = self.env['axsync.mcp.setting'].action_open()
        self.assertEqual(action['res_id'], self.setting.id)
