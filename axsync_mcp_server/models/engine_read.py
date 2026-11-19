# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import csv
import io
import json
import re

import odoo
from odoo import _, api, fields, models

from .engine import McpError, _jsonable

ORDER_RE = re.compile(r'^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?( (asc|desc))?( nulls (first|last))?$', re.I)
MEASURE_RE = re.compile(r'^([a-z_][a-z0-9_]*)(:(sum|avg|min|max|count_distinct))?$')
GROUP_RE = re.compile(r'^([a-z_][a-z0-9_]*)(:(day|week|month|quarter|year))?$')


class McpEngineRead(models.AbstractModel):
    _inherit = 'axsync.mcp.engine'

    @api.model
    def _limit(self, args, default=None):
        setting = self.env['axsync.mcp.setting'].get()
        cap = setting.max_records or 200
        try:
            value = int(args.get('limit') or default or min(cap, 50))
        except (TypeError, ValueError):
            raise McpError(_('limit must be a number.'))
        return max(1, min(value, cap))

    @api.model
    def _clean_order(self, model, order):
        if not order:
            return None
        parts = []
        for part in str(order).split(','):
            part = part.strip()
            if not ORDER_RE.match(part):
                raise McpError(_('Bad order clause %s.', part))
            fname = part.split()[0]
            self._check_path(model, fname)
            parts.append(part)
        return ', '.join(parts)

    @api.model
    def _tool_whoami(self, args, ctx):
        user = self.env.user
        setting = self.env['axsync.mcp.setting'].get()
        profiles = self._profiles(ctx)
        return {
            'user': {'id': user.id, 'name': user.name, 'login': user.login},
            'company': {'id': user.company_id.id, 'name': user.company_id.name},
            'access_levels': [{'name': p.name, 'level': p.level} for p in profiles] or [
                {'name': 'No restriction beyond Odoo access rights', 'level': 'full'}],
            'odoo_version': odoo.release.version,
            'limits': {'max_records_per_call': setting.max_records, 'calls_per_minute': setting.rate_limit,
                       'writes_per_day': setting.daily_write_quota},
            'default_write_action': setting.default_write_action,
            'can_approve': user.has_group('axsync_mcp_server.group_axsync_mcp_approver'),
        }

    @api.model
    def _tool_list_models(self, args, ctx):
        query = (args.get('query') or '').lower().strip()
        profiles = self._profiles(ctx)
        limit = self._limit(args, 80)
        out = []
        for rec in self.env['ir.model'].sudo().search([('transient', '=', False)], order='model'):
            if self._blocked_model(rec.model) or rec.model not in self.env:
                continue
            if query and query not in rec.model.lower() and query not in (rec.name or '').lower():
                continue
            Model = self.env[rec.model]
            if Model._abstract or (not getattr(Model, '_auto', True) and not getattr(Model, '_table_query', None)):
                continue
            if not self._has_access(Model, 'read'):
                continue
            if any(not p.allows(rec.model, 'read') for p in profiles):
                continue
            out.append({'model': rec.model, 'name': rec.name})
            if len(out) >= limit:
                break
        return {'models': out, 'count': len(out)}

    @api.model
    def _tool_describe_model(self, args, ctx):
        model = args.get('model')
        profiles = self._profiles(ctx)
        Model = self._model(model, 'read', profiles)
        masks = self._masks(model)
        fields_out = []
        for name, field in sorted(Model._fields.items()):
            if SECRET_HIDE(name, masks):
                continue
            entry = {'name': name, 'label': field.string, 'type': field.type, 'required': bool(field.required),
                     'readonly': bool(field.readonly), 'stored': bool(field.store)}
            if getattr(field, 'comodel_name', None):
                entry['relation'] = field.comodel_name
            if field.type == 'selection' and not callable(field.selection) and field.selection:
                entry['selection'] = [list(x) for x in field.selection]
            elif field.type == 'selection':
                try:
                    entry['selection'] = [list(x) for x in field._description_selection(self.env)]
                except Exception:  # pylint: disable=broad-except
                    pass
            if name in masks:
                entry['masked'] = masks[name]
            fields_out.append(entry)
        ops = {}
        for op in ('read', 'create', 'write', 'unlink'):
            ops[op] = self._has_access(Model, op) and all(p.allows(model, op) for p in profiles)
        ops['call'] = all(p.allows(model, 'call') for p in profiles)
        return {'model': model, 'name': Model._description, 'allowed_operations': ops,
                'has_active_flag': 'active' in Model._fields, 'fields': fields_out}

    @api.model
    def _read_names(self, model, args):
        names = args.get('fields')
        if names:
            if not isinstance(names, list):
                raise McpError(_('fields must be a list.'))
            return self._check_fields(model, names)
        return self._default_fields(model)

    @api.model
    def _tool_search_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        domain = self._clean_domain(model, args.get('domain'))
        names = self._read_names(model, args)
        limit = self._limit(args)
        offset = max(int(args.get('offset') or 0), 0)
        order = self._clean_order(model, args.get('order'))
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order=order)
        return {'total': total, 'returned': len(records), 'offset': offset,
                'records': self._serialize(records, names)}

    @api.model
    def _tool_count_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        return {'count': Model.search_count(self._clean_domain(model, args.get('domain')))}

    @api.model
    def _tool_read_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        ids = args.get('ids')
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise McpError(_('ids must be a list of integers.'))
        limit = self._limit({})
        records = Model.search([('id', 'in', ids[:limit])])
        return {'records': self._serialize(records, self._read_names(model, args))}

    @api.model
    def _tool_name_search(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        domain = self._clean_domain(model, args.get('domain'))
        found = Model.name_search(str(args.get('name') or ''), domain, limit=self._limit(args, 10))
        masks = self._masks(model)
        hit = [k for k in ('name', 'display_name', 'complete_name') if k in masks]
        if hit:
            raise McpError(_('The name of %s is masked, so it cannot be searched.', model), 'denied')
        return {'results': [{'id': i, 'name': n} for i, n in found]}

    @api.model
    def _tool_aggregate(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        domain = self._clean_domain(model, args.get('domain'))
        group_by = args.get('group_by') or []
        if not isinstance(group_by, list) or not group_by or len(group_by) > 3:
            raise McpError(_('group_by needs one to three fields.'))
        for g in group_by:
            m = GROUP_RE.match(str(g))
            if not m:
                raise McpError(_('Bad group_by %s.', g))
            self._check_path(model, m.group(1))
            if m.group(1) not in Model._fields:
                raise McpError(_('Unknown field %s.', m.group(1)))
        measures = args.get('measures') or []
        specs = []
        for item in measures:
            m = MEASURE_RE.match(str(item))
            if not m:
                raise McpError(_('Bad measure %s.', item))
            self._check_path(model, m.group(1))
            field = Model._fields.get(m.group(1))
            if not field or field.type not in ('integer', 'float', 'monetary') and m.group(3) not in ('count_distinct', 'min', 'max'):
                raise McpError(_('Field %s cannot be aggregated that way.', m.group(1)))
            specs.append('%s:%s' % (m.group(1), m.group(3) or 'sum'))
        limit = self._limit(args, 50)
        rows = self._group_rows(Model, domain, list(group_by), specs)
        order = args.get('order')
        if order:
            keys = set(group_by) | set(specs) | {'__count', 'count'}
            parts = []
            for part in str(order).split(','):
                bits = part.strip().split()
                if not bits or bits[0] not in keys or len(bits) > 2 or (len(bits) == 2 and bits[1].lower() not in ('asc', 'desc')):
                    raise McpError(_('Order by a group_by field, a measure or count.'))
                parts.append((('__count' if bits[0] == 'count' else bits[0]), len(bits) == 2 and bits[1].lower() == 'desc'))
            for key, desc in reversed(parts):
                rows.sort(key=lambda r, k=key: (r.get(k) is None, str(r.get(k)) if isinstance(r.get(k), (dict, list)) else r.get(k)), reverse=desc)
        out = []
        for row in rows[:limit]:
            entry = {'count': row['__count']}
            for g in group_by:
                entry[g] = row.get(g)
            for s_ in specs:
                entry[s_] = row.get(s_)
            out.append(entry)
        return {'groups': _jsonable(out), 'count': len(out), 'truncated': len(rows) > limit}

    @api.model
    def _group_rows(self, Model, domain, group_by, specs):
        """Version neutral grouped read. Returns dict rows with the group keys, the specs and __count."""
        out = []
        if odoo.release.version_info[0] >= 17:
            raw = Model._read_group(domain, groupby=list(group_by), aggregates=list(specs) + ['__count'], limit=2000)
            for row in raw:
                entry = {}
                for i, g in enumerate(group_by):
                    value = row[i]
                    if isinstance(value, models.BaseModel):
                        value = {'id': value.id, 'name': value.display_name} if value else None
                    entry[g] = value
                for j, spec in enumerate(specs):
                    entry[spec] = row[len(group_by) + j]
                entry['__count'] = row[-1]
                out.append(entry)
        else:
            raw = Model.read_group(domain, list(specs), list(group_by), limit=2000, lazy=False)
            for row in raw:
                entry = {}
                for g in group_by:
                    value = row.get(g)
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        value = {'id': value[0], 'name': value[1]}
                    entry[g] = value
                for spec in specs:
                    entry[spec] = row.get(spec.split(':')[0], row.get(spec))
                entry['__count'] = row.get('__count')
                out.append(entry)
        return out

    @api.model
    def _tool_export_records(self, args, ctx):
        model = args.get('model')
        Model = self._model(model, 'read', self._profiles(ctx))
        domain = self._clean_domain(model, args.get('domain'))
        names = self._check_fields(model, args.get('fields') or [])
        if not names:
            raise McpError(_('Give at least one field to export.'))
        limit = max(1, min(int(args.get('limit') or 10000), 10000))
        records = Model.search(domain, limit=limit)
        data = self._serialize(records, names)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(names)
        for row in data:
            writer.writerow([self._csv_cell(row.get(n)) for n in names])
        att = self.env['ir.attachment'].sudo().create({
            'name': '%s_export.csv' % model.replace('.', '_'),
            'raw': buf.getvalue().encode('utf-8-sig'),
            'mimetype': 'text/csv', 'res_model': 'axsync.mcp.setting', 'res_id': 0,
            'public': False,
        })
        token = att.generate_access_token()[0]
        return {'rows': len(data), 'file': att.name,
                'download_url': '%s/mcp/download/%d?access_token=%s' % (self.base_url(), att.id, token),
                'note': 'The link works for one hour for anyone who has it.'}

    @api.model
    def _csv_cell(self, value):
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
            value = value[1]
        elif isinstance(value, list):
            value = ','.join(str(v) for v in value)
        if value is None or value is False:
            return ''
        text = str(value)
        if text[:1] in ('=', '+', '-', '@', '\t', '\r'):
            text = "'" + text
        return text

    @api.model
    def _tool_list_changes(self, args, ctx):
        domain = [('user_id', '=', self.env.uid)]
        if args.get('model'):
            domain.append(('model', '=', str(args['model'])))
        if args.get('only_undoable'):
            domain += [('undoable', '=', True), ('undone', '=', False)]
        rows = self.env['axsync.mcp.journal'].sudo().search(domain, limit=self._limit(args, 30))
        return {'changes': [{
            'journal_id': r.id, 'batch': r.batch, 'model': r.model, 'res_id': r.res_id, 'record': r.record_name,
            'operation': r.operation, 'tool': r.tool, 'undone': r.undone, 'undoable': r.undoable,
            'when': fields.Datetime.to_string(r.create_date)} for r in rows]}

    @api.model
    def _tool_approval_status(self, args, ctx):
        rec = self.env['axsync.mcp.approval'].sudo().search([
            ('id', '=', args.get('approval_id') or 0), ('requester_id', '=', self.env.uid)], limit=1)
        if not rec:
            raise McpError(_('No such approval request.'))
        return {'approval_id': rec.id, 'reference': rec.name, 'state': rec.state, 'tool': rec.tool,
                'decided_by': rec.approver_id.name or None,
                'result': json.loads(rec.result) if rec.result else None}

    @api.model
    def _tool_list_approvals(self, args, ctx):
        domain = [('requester_id', '=', self.env.uid)]
        if args.get('state'):
            domain.append(('state', '=', str(args['state'])))
        rows = self.env['axsync.mcp.approval'].sudo().search(domain, limit=self._limit(args, 20))
        return {'approvals': [{'approval_id': r.id, 'reference': r.name, 'state': r.state, 'tool': r.tool,
                               'summary': r.summary} for r in rows]}

    @api.model
    def _tool_job_status(self, args, ctx):
        job = self.env['axsync.mcp.job'].sudo().search([
            ('id', '=', args.get('job_id') or 0), ('user_id', '=', self.env.uid)], limit=1)
        if not job:
            raise McpError(_('No such job.'))
        return {'job_id': job.id, 'state': job.state, 'total': job.total, 'done': job.done, 'failed': job.failed,
                'progress': round(job.progress, 1), 'errors': job.errors or ''}


def SECRET_HIDE(name, masks):
    return masks.get(name) == 'hidden'
