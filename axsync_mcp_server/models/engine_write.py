# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
import re
from datetime import date, datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .engine import BLOCKED_METHODS, McpError, _jsonable
from .engine_read import GROUP_RE, MEASURE_RE

DELETABLE_STATES = ('draft', 'cancel', 'cancelled', 'canceled')
METHOD_RE = re.compile(r'^[a-z][a-zA-Z0-9_]*$')


class McpEngineWrite(models.AbstractModel):
    _inherit = 'axsync.mcp.engine'

    # ------------------------------------------------------------------
    # snapshots and journal
    # ------------------------------------------------------------------
    @api.model
    def _snap_value(self, field, value):
        if field.type == 'many2one':
            return value.id if value else False
        if field.type in ('one2many', 'many2many'):
            return sorted(value.ids)
        if isinstance(value, datetime):
            return fields.Datetime.to_string(value)
        if isinstance(value, date):
            return fields.Date.to_string(value)
        if isinstance(value, bytes):
            return '<binary>'
        return value

    @api.model
    def _snapshot(self, records, names):
        out = {}
        for rec in records:
            out[rec.id] = {n: self._snap_value(rec._fields[n], rec[n]) for n in names}
        return out

    @api.model
    def _journal(self, ctx, operation, model, res_id, name, old, new, undoable=True):
        return self.env['axsync.mcp.journal'].sudo().create({
            'user_id': self.env.uid, 'token_id': ctx.get('token_id') or False, 'tool': ctx.get('tool'),
            'model': model, 'res_id': res_id, 'record_name': (name or '')[:200], 'operation': operation,
            'old_values': json.dumps(old, default=str) if old is not None else False,
            'new_values': json.dumps(new, default=str) if new is not None else False,
            'undoable': undoable, 'batch': ctx.get('batch'),
        })

    @api.model
    def _convert_values(self, Model, values):
        out = {}
        for name, value in values.items():
            field = Model._fields[name]
            if field.type in ('one2many', 'many2many') and isinstance(value, list):
                if all(isinstance(v, int) for v in value):
                    value = [(6, 0, value)]
                else:
                    value = [tuple(c) if isinstance(c, list) else c for c in value]
            out[name] = value
        return out

    @api.model
    def _check_values(self, model, values):
        if not isinstance(values, dict) or not values:
            raise McpError(_('values must be a non empty object.'))
        self._check_fields(model, list(values), for_write=True)
        return values

    @api.model
    def _apply_write(self, records, values, ctx):
        names = list(values)
        undoable = all(records._fields[n].type != 'binary' for n in names)
        before = self._snapshot(records, names)
        records.write(self._convert_values(records, values))
        records.flush_recordset()
        after = self._snapshot(records, names)
        for rec in records:
            self._journal(ctx, 'write', rec._name, rec.id, rec.display_name, before[rec.id], after[rec.id], undoable)

    @api.model
    def _apply_archive(self, records, ctx):
        for rec in records:
            self._journal(ctx, 'archive', rec._name, rec.id, rec.display_name, {'active': True}, {'active': False})
        records.write({'active': False})

    # ------------------------------------------------------------------
    # write tools
    # ------------------------------------------------------------------
    @api.model
    def _tool_create_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'create', self._profiles(ctx))
        recs = args.get('records')
        limit = self.env['axsync.mcp.setting'].get().max_records or 200
        if not isinstance(recs, list) or not recs or len(recs) > limit:
            raise McpError(_('records must be a list of 1 to %d objects.', limit))
        vals_list = []
        for vals in recs:
            self._check_values(model, vals)
            vals_list.append(self._convert_values(Model, vals))
        created = Model.create(vals_list)
        for rec, vals in zip(created, recs):
            self._journal(ctx, 'create', model, rec.id, rec.display_name, None,
                          self._snapshot(rec, [n for n in vals if n in rec._fields])[rec.id])
        return {'created': [{'id': r.id, 'name': r.display_name} for r in created], 'batch': ctx['batch'],
                'message': _('Created %d record(s). Use undo_change with batch %s to revert.', len(created), ctx['batch'])}

    @api.model
    def _tool_update_records(self, args, ctx):
        model = args.get('model')
        profiles = self._profiles(ctx)
        Model = self._model(model, 'write', profiles)
        values = self._check_values(model, args.get('values'))
        records = self._resolve_ids_or_domain(Model, args, self.env['axsync.mcp.setting'].get(), 'update_records')
        if not records:
            return {'updated': 0, 'message': _('No record matched.')}
        self._apply_write(records, values, ctx)
        return {'updated': len(records), 'ids': records.ids, 'batch': ctx['batch']}

    @api.model
    def _tool_delete_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'unlink', self._profiles(ctx))
        records = self._resolve_ids_or_domain(Model, args, self.env['axsync.mcp.setting'].get(), 'delete_records')
        archived, removed = [], []
        for rec in records:
            if 'active' in Model._fields:
                archived.append(rec.id)
            elif 'state' in Model._fields and rec.state in DELETABLE_STATES:
                removed.append(rec.id)
            else:
                raise McpError(_('%(m)s %(n)s cannot be deleted through MCP. It has no active flag and is not in a '
                                 'draft or cancelled state.', m=model, n=rec.display_name), 'denied')
        if archived:
            self._apply_archive(Model.browse(archived), ctx)
        for rid in removed:
            rec = Model.browse(rid)
            self._journal(ctx, 'unlink', model, rid, rec.display_name, None, None, undoable=False)
            rec.unlink()
        return {'archived': archived, 'deleted': removed, 'batch': ctx['batch']}

    @api.model
    def _check_method(self, Model, method):
        if not isinstance(method, str) or not METHOD_RE.match(method) or method in BLOCKED_METHODS:
            raise McpError(_('The method %s cannot be called through MCP.', method), 'denied')
        func = getattr(Model, method, None)
        if not callable(func) or getattr(func, '_api_private', False):
            raise McpError(_('The model %(m)s has no public method %(f)s.', m=Model._name, f=method), 'denied')
        return func

    @api.model
    def _tool_call_method(self, args, ctx):
        model = args.get('model')
        profiles = self._profiles(ctx)
        Model = self._model(model, 'call', profiles)
        if not self._has_access(Model, 'read'):
            raise McpError(_('You cannot read %s.', model), 'denied')
        method = args.get('method')
        self._check_method(Model, method)
        records = self._resolve_ids_or_domain(Model, args, self.env['axsync.mcp.setting'].get(), 'call_method')
        call_args = args.get('args') or []
        call_kwargs = args.get('kwargs') or {}
        if not isinstance(call_args, list) or not isinstance(call_kwargs, dict):
            raise McpError(_('args must be a list and kwargs an object.'))
        target = records if args.get('ids') else Model
        result = getattr(target, method)(*call_args, **call_kwargs)
        if isinstance(result, models.BaseModel):
            result = {'model': result._name, 'ids': result.ids}
        elif isinstance(result, dict) and result.get('type', '').startswith('ir.actions'):
            result = {k: v for k, v in result.items() if k in ('type', 'res_model', 'res_id', 'name', 'view_mode', 'target')}
        return {'result': _jsonable(result), 'records': len(records),
                'note': _('Method calls are not journaled and cannot be undone automatically.')}

    # ------------------------------------------------------------------
    # undo
    # ------------------------------------------------------------------
    @api.model
    def _journal_entries(self, args):
        domain = [('user_id', '=', self.env.uid), ('undone', '=', False), ('undoable', '=', True)]
        if args.get('journal_id'):
            domain.append(('id', '=', int(args['journal_id'])))
        elif args.get('batch'):
            domain.append(('batch', '=', str(args['batch'])))
        else:
            raise McpError(_('Give journal_id or batch.'))
        return self.env['axsync.mcp.journal'].sudo().search(domain, order='id desc')

    @api.model
    def undo_entry(self, entry, user, force=False):
        if entry.undone or not entry.undoable:
            raise UserError(_('This change cannot be undone.'))
        if entry.user_id != user and not user.has_group('axsync_mcp_server.group_axsync_mcp_manager'):
            raise AccessError(_('You can only undo your own changes.'))
        engine = self.with_user(entry.user_id if entry.user_id else user)
        Model = engine.env[entry.model]
        rec = Model.search([('id', '=', entry.res_id)]) if 'active' not in Model._fields else \
            Model.with_context(active_test=False).search([('id', '=', entry.res_id)])
        if entry.operation == 'create':
            if rec:
                rec.unlink()
        else:
            if not rec:
                raise UserError(_('The record no longer exists or is not accessible.'))
            old = json.loads(entry.old_values or '{}')
            new = json.loads(entry.new_values or '{}')
            current = engine._snapshot(rec, list(new))[rec.id]
            if current != new and not force:
                raise UserError(_('%(m)s %(n)s was changed after this entry. Undo refused.',
                                  m=entry.model, n=entry.record_name))
            rec.write(engine._convert_values(Model, old))
        entry.sudo().write({'undone': True, 'undone_on': fields.Datetime.now(), 'undone_by': user.id})
        return True

    @api.model
    def _tool_undo_change(self, args, ctx):
        entries = self._journal_entries(args)
        if not entries:
            raise McpError(_('Nothing to undo. The change may be undone already, or it is not yours.'))
        for entry in entries:
            self.undo_entry(entry, self.env.user, force=bool(args.get('force')))
        return {'undone': len(entries)}

    # ------------------------------------------------------------------
    # bulk jobs
    # ------------------------------------------------------------------
    @api.model
    def _tool_start_bulk_job(self, args, ctx):
        model = args.get('model')
        profiles = self._profiles(ctx)
        Model = self._model(model, 'write', profiles)
        operation = args.get('operation')
        if operation not in ('write', 'archive'):
            raise McpError(_('operation must be write or archive.'))
        values = None
        if operation == 'write':
            values = self._check_values(model, args.get('values'))
        elif 'active' not in Model._fields:
            raise McpError(_('%s has no active flag to archive.', model))
        records = Model.search(self._clean_domain(model, args.get('domain')), limit=50001)
        if len(records) > 50000:
            raise McpError(_('A bulk job is limited to 50000 records.'))
        if not records:
            return {'message': _('No record matched.'), 'total': 0}
        if ctx.get('dry'):
            return {'would_process': len(records), 'operation': operation,
                    'sample': [{'id': r.id, 'name': r.display_name} for r in records[:5]]}
        job = self.env['axsync.mcp.job'].sudo().create({
            'name': '%s %s' % (operation, model), 'user_id': self.env.uid,
            'token_id': ctx.get('token_id') or False, 'model': model, 'operation': operation,
            'record_ids': json.dumps(records.ids), 'values': json.dumps(values) if values else False,
            'total': len(records),
        })
        return {'job_id': job.id, 'total': job.total,
                'message': _('The job runs in the background in batches. Check it with job_status.')}

    # ------------------------------------------------------------------
    # live pages
    # ------------------------------------------------------------------
    @api.model
    def _tool_publish_page(self, args, ctx):
        model = args.get('model')
        self._model(model, 'read', self._profiles(ctx))
        domain = args.get('domain') or []
        self._clean_domain(model, domain)
        names = args.get('fields') or []
        if names:
            self._check_fields(model, names)
        group_by = args.get('group_by')
        if group_by:
            m = GROUP_RE.match(str(group_by))
            if not m:
                raise McpError(_('Bad group_by.'))
            self._check_path(model, m.group(1))
        measure = args.get('measure')
        if measure:
            m = MEASURE_RE.match(str(measure))
            if not m:
                raise McpError(_('Bad measure.'))
            self._check_path(model, m.group(1))
        mode = args.get('mode') or 'private'
        if mode not in ('private', 'login', 'link'):
            raise McpError(_('Bad mode.'))
        days = args.get('expires_in_days')
        if mode == 'link' and not days:
            days = 30
        vals = {
            'name': str(args.get('title') or '')[:120] or model, 'owner_id': self.env.uid, 'mode': mode,
            'model': model, 'domain': json.dumps(domain), 'field_names': ','.join(names) or False,
            'group_by': group_by or False, 'measure': measure or False,
            'chart': args.get('chart') if args.get('chart') in ('none', 'bar') else 'bar',
        }
        if days:
            vals['expires_on'] = fields.Datetime.add(fields.Datetime.now(), days=int(days))
        page = self.env['axsync.mcp.page'].sudo().create(vals)
        return {'slug': page.slug, 'url': page.url, 'mode': mode,
                'message': _('The page is live. Mode: %s.', mode)}

    @api.model
    def _tool_list_pages(self, args, ctx):
        pages = self.env['axsync.mcp.page'].sudo().search([('owner_id', '=', self.env.uid)], limit=100)
        return {'pages': [{'slug': p.slug, 'title': p.name, 'mode': p.mode, 'state': p.state, 'url': p.url,
                           'views': p.view_count} for p in pages]}

    @api.model
    def _tool_unpublish_page(self, args, ctx):
        page = self.env['axsync.mcp.page'].sudo().search([
            ('slug', '=', args.get('slug')), ('owner_id', '=', self.env.uid)], limit=1)
        if not page:
            raise McpError(_('No such page.'))
        page.action_unpublish()
        return {'unpublished': page.slug}

    @api.model
    def page_data(self, page):
        """Run the page query as the current env user."""
        model = page.model
        Model = self._model(model, 'read')
        domain = self._clean_domain(model, json.loads(page.domain or '[]'))
        limit = max(1, min(page.limit or 100, 500))
        if page.group_by:
            specs = []
            if page.measure:
                m = MEASURE_RE.match(page.measure)
                specs.append('%s:%s' % (m.group(1), m.group(3) or 'sum'))
            rows = self._group_rows(Model, domain, [page.group_by], specs)[:limit]
            key = page.group_by
            columns = [page.group_by] + ([page.measure] if page.measure else []) + ['count']
            data, bars = [], []
            for row in rows:
                label = row.get(key)
                if isinstance(label, dict):
                    label = label.get('name')
                value = row.get(specs[0]) if specs else row['__count']
                data.append([label] + ([value] if specs else []) + [row['__count']])
                bars.append((label if label is not None else '-', float(value or 0)))
            return {'columns': columns, 'rows': _jsonable(data), 'bars': bars if page.chart == 'bar' else []}
        names = [n for n in (page.field_names or '').split(',') if n] or self._default_fields(model)[:8]
        names = self._check_fields(model, names)
        records = Model.search(domain, limit=limit)
        data = self._serialize(records, names)
        rows = []
        for row in data:
            line = []
            for n in names:
                v = row.get(n)
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
                    v = v[1]
                line.append(v)
            rows.append(line)
        return {'columns': names, 'rows': rows, 'bars': []}
