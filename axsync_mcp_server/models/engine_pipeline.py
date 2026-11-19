# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
import logging
import time
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from .engine import McpError, _DryRunRollback, _jsonable
from .tool_defs import TOOL_BY_NAME

_logger = logging.getLogger(__name__)


class McpEnginePipeline(models.AbstractModel):
    _inherit = 'axsync.mcp.engine'

    # ------------------------------------------------------------------
    # targets
    # ------------------------------------------------------------------
    @api.model
    def _targets(self, meta, args, setting, profiles):
        """Return (model, operation, records, count) used by policies and quotas."""
        name = meta['name']
        if name == 'undo_change':
            entry = self._journal_entries(args)
            model = entry[:1].model if entry else False
            return model, 'write', self.env[model].browse() if model in self.env else None, len(entry)
        if name == 'publish_page':
            return args.get('model'), 'read', None, 0
        op = meta['op']
        if op == 'none' or not args.get('model'):
            return None, op, None, 0
        model = args.get('model')
        Model = self._model(model, op, profiles)
        if name == 'create_records':
            recs = args.get('records')
            if not isinstance(recs, list) or not recs:
                raise McpError(_('records must be a non empty list.'))
            return model, op, Model.browse(), len(recs)
        if name == 'start_bulk_job':
            dom = self._clean_domain(model, args.get('domain'))
            records = Model.search(dom, limit=50001)
            return model, 'write', records, len(records)
        if name == 'call_method':
            self._check_method(Model, args.get('method'))
        if name in ('update_records', 'delete_records', 'call_method'):
            records = self._resolve_ids_or_domain(Model, args, setting, name)
            return model, op, records, len(records)
        return model, op, None, 0

    @api.model
    def _resolve_ids_or_domain(self, Model, args, setting, name):
        ids = args.get('ids')
        domain = args.get('domain')
        limit = setting.max_records or 200
        if ids:
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise McpError(_('ids must be a list of integers.'))
            records = Model.search([('id', 'in', ids)])
            if len(records) != len(set(ids)):
                raise McpError(_('Some ids do not exist or are not accessible.'))
        elif domain and name == 'update_records':
            records = Model.search(self._clean_domain(Model._name, domain), limit=limit + 1)
        elif name == 'call_method':
            records = Model.browse()
        else:
            raise McpError(_('Provide ids%s.', _(' or a domain') if name == 'update_records' else ''))
        if len(records) > limit:
            raise McpError(_('This call touches more than %d records. Narrow it down or use start_bulk_job.', limit))
        return records

    @api.model
    def _forced_action(self, name, args):
        if name == 'publish_page' and args.get('mode') == 'link':
            return 'approve'
        return None

    # ------------------------------------------------------------------
    # pipeline
    # ------------------------------------------------------------------
    @api.model
    def call_tool(self, name, args, ctx):
        started = time.time()
        args = args if isinstance(args, dict) else {}
        info = {'tool': name, 'is_write': False, 'record_count': 0}
        try:
            result = self._pipeline(name, args, ctx, info)
        except McpError as exc:
            result = {'status': exc.status, 'message': str(exc)}
        except (AccessError, MissingError) as exc:
            result = {'status': 'denied', 'message': exc.args[0] if exc.args else 'Access denied'}
        except (UserError, ValidationError) as exc:
            result = {'status': 'error', 'message': exc.args[0] if exc.args else 'Error'}
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception('MCP tool %s failed', name)
            result = {'status': 'error', 'message': 'The tool failed unexpectedly: %s' % type(exc).__name__}
        status = result.get('status')
        log_status = {'ok': 'ok', 'dry_run': 'dry_run', 'denied': 'denied', 'error': 'error',
                      'confirmation_required': 'pending', 'pending_approval': 'pending'}.get(status, 'ok')
        self._log(
            ctx, method='tools/call', status=log_status, message=(result.get('message') or '')[:2000],
            arguments=self._log_args(args), duration_ms=int((time.time() - started) * 1000), **info)
        return result

    @api.model
    def _log_args(self, args):
        clean = dict(args)
        if 'confirm' in clean:
            clean['confirm'] = '***'
        return clean

    @api.model
    def _pipeline(self, name, args, ctx, info):
        meta = TOOL_BY_NAME.get(name)
        tool = self.env['axsync.mcp.tool'].sudo().search([('code', '=', name)], limit=1)
        if not meta or not tool:
            raise McpError(_('Unknown tool %s.', name))
        if not tool.enabled:
            raise McpError(_('The tool %s is disabled by the administrator.', name), 'denied')
        setting = self.env['axsync.mcp.setting'].get()
        profiles = self._profiles(ctx)
        for profile in profiles:
            if not profile.allows_tool(name):
                raise McpError(_('The access level "%(p)s" does not allow the tool %(t)s.', p=profile.name, t=name),
                               'denied')
        is_write = meta['kind'] in ('write', 'admin')
        info['is_write'] = is_write
        model, op, records, count = self._targets(meta, args, setting, profiles)
        info.update({'model': model, 'operation': op, 'record_count': count})
        approved = bool(ctx.get('approved'))

        action, policy = 'allow', self.env['axsync.mcp.policy']
        if is_write or op in ('read', 'unlink', 'call'):
            recs = records
            if recs is None:
                recs = self.env[model].browse() if model and model in self.env else self.env['res.users'].browse()
            action, policy = self.env['axsync.mcp.policy'].evaluate(
                self.env.user, name, model or '', op, recs, count)
        forced = self._forced_action(name, args)
        if forced and ACTION_RANK[forced] > ACTION_RANK[action]:
            action = forced
        if (not policy and action == 'allow' and is_write
                and op in ('create', 'write') and name != 'unlink'):
            action = setting.default_write_action
        message = policy.message if policy else False

        if action == 'block':
            raise McpError(message or _('Blocked by policy %s.', policy.name if policy else ''), 'denied')
        if is_write:
            self._check_quota(setting, count)
        if action == 'approve' and not approved:
            summary = self._summary(name, model, op, count, args)
            approval = self.env['axsync.mcp.approval'].open_request(
                self.env.user, self.env['axsync.mcp.token'].sudo().browse(ctx.get('token_id') or 0).exists(),
                name, model, op, summary, self._approval_args(args), count, policy)
            return {'status': 'pending_approval', 'approval_id': approval.id, 'reference': approval.name,
                    'message': message or _(
                        'This action needs approval from another person. Request %s was created. '
                        'Check it later with approval_status.', approval.name)}
        if action == 'ask' and not approved and not self.check_confirm_token(name, args, args.get('confirm')):
            preview = None
            if meta.get('dry'):
                preview = self._dry_run(meta, args, ctx, setting, profiles)
            return {'status': 'confirmation_required',
                    'message': message or _('Show this to the user. If they agree, call the tool again with the '
                                            'same arguments and the confirm value.'),
                    'summary': self._summary(name, model, op, count, args),
                    'preview': preview, 'confirm': self.make_confirm_token(name, args)}
        if args.get('dry_run') and meta.get('dry'):
            return {'status': 'dry_run', 'preview': self._dry_run(meta, args, ctx, setting, profiles),
                    'message': _('Nothing was changed.')}
        return self._execute(meta, args, ctx, setting, profiles)

    @api.model
    def _approval_args(self, args):
        return {k: v for k, v in args.items() if k not in ('confirm', 'dry_run')}

    @api.model
    def _summary(self, name, model, op, count, args):
        bits = [name]
        if model:
            bits.append('on %s' % model)
        if count:
            bits.append('(%d record%s)' % (count, '' if count == 1 else 's'))
        if args.get('values'):
            bits.append('values: %s' % json.dumps(_jsonable(args['values']), default=str)[:300])
        if args.get('method'):
            bits.append('method: %s' % args['method'])
        return ' '.join(bits)

    @api.model
    def _check_quota(self, setting, count):
        quota = setting.daily_write_quota
        if not quota:
            return
        self.env.cr.execute(
            "SELECT COALESCE(SUM(GREATEST(record_count, 1)), 0) FROM axsync_mcp_log "
            "WHERE user_id = %s AND is_write AND status = 'ok' AND day = %s",
            [self.env.uid, fields.Date.context_today(self)])
        used = self.env.cr.fetchone()[0] or 0
        if used + max(count, 1) > quota:
            raise McpError(_('The daily write quota of %d was reached.', quota), 'denied')

    @api.model
    def _dry_run(self, meta, args, ctx, setting, profiles):
        try:
            with self.env.cr.savepoint():
                result = self._run_tool(meta, dict(args, dry_run=False), ctx, setting, profiles, dry=True)
                raise _DryRunRollback(result)
        except _DryRunRollback as roll:
            self.env.invalidate_all()
            return roll.result
        return None

    @api.model
    def _execute(self, meta, args, ctx, setting, profiles):
        with self.env.cr.savepoint():
            return self._run_tool(meta, args, ctx, setting, profiles)

    @api.model
    def _run_tool(self, meta, args, ctx, setting, profiles, dry=False):
        handler = getattr(self, '_tool_' + meta['name'])
        ctx = dict(ctx, batch=uuid.uuid4().hex[:12], dry=dry, setting_id=setting.id)
        out = handler(args, ctx)
        if isinstance(out, dict) and 'status' not in out:
            out = dict(out, status='ok')
        return out

    @api.model
    def execute_approved(self, approval):
        """Run the stored call as the requester after an approver agreed."""
        requester = approval.requester_id
        engine = self.with_user(requester).sudo(False)
        engine = engine.with_context(allowed_company_ids=[requester.company_id.id])
        args = json.loads(approval.arguments or '{}')
        ctx = {'token_id': approval.token_id.id, 'ip': False, 'client_name': 'approval %s' % approval.name,
               'approved': True}
        return engine.call_tool(approval.tool, args, ctx)

    @api.model
    def run_job_batch(self, job):
        user = job.user_id
        engine = self.with_user(user).with_context(allowed_company_ids=[user.company_id.id])
        ids = json.loads(job.record_ids)
        start = job.done + job.failed
        chunk = ids[start:start + (job.batch_size or 200)]
        if not chunk:
            job.write({'state': 'done'})
            return
        Model = engine.env[job.model]
        values = json.loads(job.values or '{}')
        errors = []
        batch = 'job%d' % job.id
        for rid in chunk:
            rec = Model.search([('id', '=', rid)])
            if not rec:
                job.failed += 1
                errors.append('%d: not found or not accessible' % rid)
                continue
            try:
                with engine.env.cr.savepoint():
                    if job.operation == 'archive':
                        engine._apply_archive(rec, {'token_id': job.token_id.id, 'batch': batch, 'tool': 'start_bulk_job'})
                    else:
                        engine._apply_write(rec, values, {'token_id': job.token_id.id, 'batch': batch,
                                                          'tool': 'start_bulk_job'})
                job.done += 1
            except Exception as exc:  # pylint: disable=broad-except
                job.failed += 1
                errors.append('%d: %s' % (rid, exc.args[0] if exc.args else exc))
        if errors:
            job.errors = ((job.errors or '') + '\n'.join(errors) + '\n')[-4000:]
        if job.done + job.failed >= job.total:
            job.state = 'done'


ACTION_RANK = {'allow': 0, 'ask': 1, 'approve': 2, 'block': 3}
